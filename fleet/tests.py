import datetime as dt
from datetime import date, timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from fleet.forms import ServiceRecordDescriptionForm, VehicleForm
from fleet.models import ServiceAssignment, ServiceRecord, TimelineEvent, TimelineImmutableError, Vehicle
from fleet.services import (
    ALLOWED_TRANSITIONS,
    InvalidTransition,
    InvalidTransitionInput,
    _check_transition,
    book_service,
    complete_service,
    ensure_due_record,
    start_service,
)


def make_vehicle(**kwargs):
    defaults = {
        "registration_number": "REG-001",
        "make": "Ford",
        "model": "Transit",
        "current_odometer": 10_000,
        "service_interval_days": 90,
        "service_interval_km": 8_000,
    }
    defaults.update(kwargs)
    return Vehicle.objects.create(**defaults)


def make_user(email, role):
    return User.objects.create_user(email=email, password="irrelevant", role=role)


class TimelineEventImmutabilityTests(TestCase):
    def setUp(self):
        self.manager = make_user("manager@example.com", User.Role.FLEET_MANAGER)
        self.vehicle = make_vehicle()
        self.record = ServiceRecord.objects.create(
            vehicle=self.vehicle,
            description="Oil change",
            status=ServiceRecord.Status.DUE,
            created_by=self.manager,
        )
        self.event = TimelineEvent.objects.create(
            service_record=self.record,
            event_type=TimelineEvent.EventType.CREATED,
            actor=self.manager,
        )

    def test_create_succeeds(self):
        self.assertIsNotNone(self.event.pk)

    def test_save_on_existing_instance_raises(self):
        self.event.note = "trying to edit history"
        with self.assertRaises(TimelineImmutableError):
            self.event.save()

    def test_instance_delete_raises(self):
        with self.assertRaises(TimelineImmutableError):
            self.event.delete()

    def test_queryset_delete_raises(self):
        with self.assertRaises(TimelineImmutableError):
            TimelineEvent.objects.filter(pk=self.event.pk).delete()

    def test_row_survives_failed_delete_attempts(self):
        with self.assertRaises(TimelineImmutableError):
            self.event.delete()
        self.assertTrue(TimelineEvent.objects.filter(pk=self.event.pk).exists())


class VehicleManagerTests(TestCase):
    def setUp(self):
        self.active = make_vehicle(registration_number="ACTIVE-1")
        self.archived = make_vehicle(registration_number="ARCHIVED-1", is_archived=True)

    def test_default_manager_excludes_archived(self):
        self.assertQuerySetEqual(Vehicle.objects.all(), [self.active])

    def test_all_objects_includes_archived(self):
        self.assertEqual(Vehicle.all_objects.count(), 2)

    def test_archived_vehicle_not_reachable_via_default_manager(self):
        self.assertFalse(Vehicle.objects.filter(pk=self.archived.pk).exists())
        self.assertTrue(Vehicle.all_objects.filter(pk=self.archived.pk).exists())


class ServiceRecordCompletedFieldsTests(TestCase):
    def setUp(self):
        self.manager = make_user("manager2@example.com", User.Role.FLEET_MANAGER)
        self.vehicle = make_vehicle(registration_number="REG-002")

    def _record(self, **kwargs):
        defaults = {
            "vehicle": self.vehicle,
            "description": "Brake check",
            "status": ServiceRecord.Status.COMPLETED,
            "created_by": self.manager,
        }
        defaults.update(kwargs)
        return ServiceRecord(**defaults)

    def test_both_null_is_valid(self):
        record = self._record(status=ServiceRecord.Status.DUE)
        record.full_clean()  # should not raise

    def test_both_set_is_valid(self):
        from django.utils import timezone

        record = self._record(completed_at=timezone.now(), completed_odometer=12_000)
        record.full_clean()  # should not raise

    def test_only_completed_at_set_is_invalid(self):
        from django.utils import timezone

        record = self._record(completed_at=timezone.now())
        with self.assertRaises(ValidationError):
            record.full_clean()

    def test_only_completed_odometer_set_is_invalid(self):
        record = self._record(completed_odometer=12_000)
        with self.assertRaises(ValidationError):
            record.full_clean()

    def test_db_check_constraint_rejects_mismatched_pair_even_without_full_clean(self):
        # clean() is a form/admin convenience; the CheckConstraint is what
        # actually guarantees the invariant for code that skips full_clean()
        # and calls .save() directly.
        record = self._record(completed_odometer=12_000)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                record.save()


class VehiclePermissionTests(TestCase):
    """Goal 1, evidenced: manager-only vehicle actions actually 403 a
    technician server-side, and a rejected write leaves no trace."""

    def setUp(self):
        self.manager = make_user("vperm-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("vperm-tech@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="VPERM-1")

    def test_technician_get_create_is_403(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-create"))
        self.assertEqual(response.status_code, 403)

    def test_technician_get_update_is_403(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-update", args=[self.vehicle.pk]))
        self.assertEqual(response.status_code, 403)

    def test_technician_get_archived_list_is_403(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-archived-list"))
        self.assertEqual(response.status_code, 403)

    def test_technician_post_archive_is_403_and_vehicle_unchanged(self):
        self.client.force_login(self.technician)
        response = self.client.post(reverse("vehicle-archive", args=[self.vehicle.pk]))
        self.assertEqual(response.status_code, 403)
        self.vehicle.refresh_from_db()
        self.assertFalse(self.vehicle.is_archived)

    def test_manager_can_create_vehicle(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("vehicle-create"),
            {
                "registration_number": "VPERM-NEW",
                "make": "Toyota",
                "model": "Hilux",
                "current_odometer": 500,
                "service_interval_days": 90,
                "service_interval_km": 5_000,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Vehicle.objects.filter(registration_number="VPERM-NEW").exists())

    def test_manager_can_edit_vehicle(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("vehicle-update", args=[self.vehicle.pk]),
            {
                "registration_number": self.vehicle.registration_number,
                "make": "Changed",
                "model": self.vehicle.model,
                "current_odometer": self.vehicle.current_odometer,
                "service_interval_days": self.vehicle.service_interval_days,
                "service_interval_km": self.vehicle.service_interval_km,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.make, "Changed")

    def test_manager_can_archive_and_restore_vehicle(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("vehicle-archive", args=[self.vehicle.pk]))
        self.vehicle.refresh_from_db()
        self.assertTrue(self.vehicle.is_archived)

        self.client.post(reverse("vehicle-restore", args=[self.vehicle.pk]))
        self.vehicle.refresh_from_db()
        self.assertFalse(self.vehicle.is_archived)


class ArchivedVehicleVisibilityTests(TestCase):
    """Goal 2: archiving hides a vehicle from the default list without
    touching its service history."""

    def setUp(self):
        self.manager = make_user("varch-mgr@example.com", User.Role.FLEET_MANAGER)
        self.vehicle = make_vehicle(registration_number="VARCH-1")
        ServiceRecord.objects.create(
            vehicle=self.vehicle,
            description="Pre-archive record",
            status=ServiceRecord.Status.DUE,
            created_by=self.manager,
        )
        self.vehicle.is_archived = True
        self.vehicle.save(update_fields=["is_archived"])

    def test_archived_vehicle_absent_from_default_list(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("vehicle-list"))
        self.assertNotIn(self.vehicle, response.context["vehicles"])

    def test_archived_vehicle_present_in_archived_list(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("vehicle-archived-list"))
        self.assertIn(self.vehicle, response.context["vehicles"])

    def test_archived_vehicle_detail_still_renders(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("vehicle-detail", args=[self.vehicle.pk]))
        self.assertEqual(response.status_code, 200)

    def test_archived_vehicle_keeps_its_service_records(self):
        self.assertEqual(self.vehicle.service_records.count(), 1)


class ServiceRecordPermissionTests(TestCase):
    """Goal 3, evidenced: only the assigned technician (or a manager) can
    touch a record, and only its description."""

    def setUp(self):
        self.manager = make_user("srperm-mgr@example.com", User.Role.FLEET_MANAGER)
        self.assigned = make_user("srperm-assigned@example.com", User.Role.TECHNICIAN)
        self.unassigned = make_user("srperm-unassigned@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="SRPERM-1")
        self.record = ServiceRecord.objects.create(
            vehicle=self.vehicle,
            description="Original description",
            status=ServiceRecord.Status.DUE,
            due_since=timezone.now(),
            created_by=self.manager,
        )
        ServiceAssignment.objects.create(
            service_record=self.record, technician=self.assigned, assigned_by=self.manager
        )

    def test_technician_cannot_create_service_record(self):
        self.client.force_login(self.assigned)
        response = self.client.get(reverse("service-record-create", args=[self.vehicle.pk]))
        self.assertEqual(response.status_code, 403)

    def test_manager_can_view_record(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("service-record-detail", args=[self.record.pk]))
        self.assertEqual(response.status_code, 200)

    def test_assigned_technician_can_view_record(self):
        self.client.force_login(self.assigned)
        response = self.client.get(reverse("service-record-detail", args=[self.record.pk]))
        self.assertEqual(response.status_code, 200)

    def test_assigned_technician_can_update_description(self):
        self.client.force_login(self.assigned)
        response = self.client.post(
            reverse("service-record-update", args=[self.record.pk]),
            {"description": "Updated by assigned technician"},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.description, "Updated by assigned technician")

    def test_unassigned_technician_cannot_view_record(self):
        self.client.force_login(self.unassigned)
        response = self.client.get(reverse("service-record-detail", args=[self.record.pk]))
        self.assertEqual(response.status_code, 403)

    def test_unassigned_technician_cannot_update_record(self):
        self.client.force_login(self.unassigned)
        response = self.client.post(
            reverse("service-record-update", args=[self.record.pk]),
            {"description": "Should never apply"},
        )
        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertEqual(self.record.description, "Original description")


class FormFieldExposureTests(TestCase):
    """Goal 3's form fields, confirmed at the form-declaration level rather
    than by trying every disallowed POST param -- either role submitting
    status/due_since/technicians would fail simply because those fields
    don't exist on the form at all."""

    def test_service_record_form_only_exposes_description(self):
        self.assertEqual(list(ServiceRecordDescriptionForm.base_fields), ["description"])

    def test_vehicle_form_does_not_expose_derived_or_archive_fields(self):
        fields = set(VehicleForm.base_fields)
        self.assertNotIn("next_due_date", fields)
        self.assertNotIn("next_due_odometer", fields)
        self.assertNotIn("is_archived", fields)


def make_record(vehicle, created_by, status=ServiceRecord.Status.DUE, **kwargs):
    defaults = {
        "vehicle": vehicle,
        "description": "Routine check",
        "status": status,
        "created_by": created_by,
    }
    if status == ServiceRecord.Status.DUE:
        defaults["due_since"] = timezone.now()
    defaults.update(kwargs)
    return ServiceRecord.objects.create(**defaults)


class LegalTransitionTests(TestCase):
    """Goal 4, happy path: the full lifecycle actually runs end to end."""

    def test_full_lifecycle_succeeds(self):
        manager = make_user("legal-mgr@example.com", User.Role.FLEET_MANAGER)
        technician = make_user("legal-tech@example.com", User.Role.TECHNICIAN)
        vehicle = make_vehicle(registration_number="LEGAL-1")
        record = make_record(vehicle, manager)

        book_service(record, scheduled_date=date.today(), technician=technician, actor=manager)
        self.assertEqual(record.status, ServiceRecord.Status.BOOKED)

        start_service(record, actor=technician)
        self.assertEqual(record.status, ServiceRecord.Status.IN_SERVICE)

        complete_service(record, completed_odometer=vehicle.current_odometer + 1_000, actor=technician)
        self.assertEqual(record.status, ServiceRecord.Status.COMPLETED)


class IllegalTransitionTests(TestCase):
    """Goal 4: illegal moves raise, with a message naming current and
    attempted status. The four cases the brief calls out explicitly."""

    def setUp(self):
        self.manager = make_user("illegal-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("illegal-tech@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="ILLEGAL-1")

    def test_due_to_completed_is_rejected(self):
        record = make_record(self.vehicle, self.manager, status=ServiceRecord.Status.DUE)
        with self.assertRaises(InvalidTransition) as ctx:
            complete_service(record, completed_odometer=self.vehicle.current_odometer, actor=self.manager)
        message = str(ctx.exception)
        self.assertIn("Due", message)
        self.assertIn("Completed", message)

    def test_due_to_in_service_is_rejected(self):
        record = make_record(self.vehicle, self.manager, status=ServiceRecord.Status.DUE)
        with self.assertRaises(InvalidTransition) as ctx:
            start_service(record, actor=self.manager)
        message = str(ctx.exception)
        self.assertIn("Due", message)
        self.assertIn("In service", message)

    def test_completed_to_anything_is_rejected(self):
        record = make_record(
            self.vehicle,
            self.manager,
            status=ServiceRecord.Status.COMPLETED,
            completed_at=timezone.now(),
            completed_odometer=self.vehicle.current_odometer,
        )
        with self.assertRaises(InvalidTransition) as ctx:
            book_service(record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        self.assertIn("terminal", str(ctx.exception).lower())

    def test_booked_to_due_is_not_a_legal_move(self):
        # No function performs this move at all -- book_service only ever
        # goes DUE -> BOOKED, so the illegality is asserted against the
        # table and the guard function itself rather than a nonexistent
        # "unbook" call.
        record = make_record(self.vehicle, self.manager, status=ServiceRecord.Status.BOOKED, scheduled_date=date.today())
        self.assertNotIn(ServiceRecord.Status.DUE, ALLOWED_TRANSITIONS[ServiceRecord.Status.BOOKED])
        with self.assertRaises(InvalidTransition):
            _check_transition(record, ServiceRecord.Status.DUE)


class BookServiceInputTests(TestCase):
    """Goal 4: book_service requires both a technician and a scheduled
    date, and rejects either being missing without mutating the record."""

    def setUp(self):
        self.manager = make_user("book-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("book-tech@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="BOOK-1")
        self.record = make_record(self.vehicle, self.manager)

    def test_missing_technician_is_rejected(self):
        with self.assertRaises(InvalidTransitionInput):
            book_service(self.record, scheduled_date=date.today(), technician=None, actor=self.manager)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)

    def test_missing_scheduled_date_is_rejected(self):
        with self.assertRaises(InvalidTransitionInput):
            book_service(self.record, scheduled_date=None, technician=self.technician, actor=self.manager)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)
        self.assertFalse(ServiceAssignment.objects.filter(service_record=self.record).exists())


class CompleteServiceDueResetTests(TestCase):
    """Goal 4: completion resets both counters from the completion's OWN
    date and odometer, not from today or the vehicle's prior reading, and
    rejects a backwards odometer."""

    def setUp(self):
        self.manager = make_user("complete-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("complete-tech@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(
            registration_number="COMPLETE-1",
            current_odometer=10_000,
            service_interval_days=90,
            service_interval_km=8_000,
        )
        self.record = make_record(self.vehicle, self.manager, status=ServiceRecord.Status.IN_SERVICE)

    def test_resets_both_counters_from_completion_date_and_odometer(self):
        fixed_now = timezone.make_aware(dt.datetime(2026, 1, 10, 12, 0, 0))
        with patch("fleet.services.timezone.now", return_value=fixed_now):
            complete_service(self.record, completed_odometer=15_000, actor=self.technician)

        self.vehicle.refresh_from_db()
        self.record.refresh_from_db()
        self.assertEqual(self.vehicle.next_due_date, dt.date(2026, 1, 10) + timedelta(days=90))
        self.assertEqual(self.vehicle.next_due_odometer, 15_000 + 8_000)
        self.assertEqual(self.vehicle.current_odometer, 15_000)
        self.assertEqual(self.record.completed_at, fixed_now)
        self.assertEqual(self.record.completed_odometer, 15_000)

    def test_current_odometer_untouched_if_completion_reading_is_not_higher(self):
        complete_service(self.record, completed_odometer=self.vehicle.current_odometer, actor=self.technician)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.current_odometer, 10_000)

    def test_backwards_odometer_is_rejected(self):
        with self.assertRaises(InvalidTransitionInput):
            complete_service(self.record, completed_odometer=self.vehicle.current_odometer - 1, actor=self.technician)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.IN_SERVICE)
        self.assertIsNone(self.record.completed_at)


class TransitionTimelineTests(TestCase):
    """Goal 9: every transition writes exactly one timeline event, with
    the right actor, old_value and new_value."""

    def setUp(self):
        self.manager = make_user("timeline-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("timeline-tech@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="TIMELINE-1")
        self.record = make_record(self.vehicle, self.manager)

    def _event_count(self):
        return TimelineEvent.objects.filter(service_record=self.record).count()

    def test_book_service_writes_exactly_one_event(self):
        before = self._event_count()
        book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        self.assertEqual(self._event_count() - before, 1)
        event = TimelineEvent.objects.filter(service_record=self.record).latest("created_at")
        self.assertEqual(event.event_type, TimelineEvent.EventType.STATUS_CHANGED)
        self.assertEqual(event.actor, self.manager)
        self.assertEqual(event.old_value, ServiceRecord.Status.DUE)
        self.assertEqual(event.new_value, ServiceRecord.Status.BOOKED)

    def test_start_service_writes_exactly_one_event(self):
        book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        before = self._event_count()
        start_service(self.record, actor=self.technician)
        self.assertEqual(self._event_count() - before, 1)
        event = TimelineEvent.objects.filter(service_record=self.record).latest("created_at")
        self.assertEqual(event.actor, self.technician)
        self.assertEqual(event.old_value, ServiceRecord.Status.BOOKED)
        self.assertEqual(event.new_value, ServiceRecord.Status.IN_SERVICE)

    def test_complete_service_writes_exactly_one_event(self):
        book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        start_service(self.record, actor=self.technician)
        before = self._event_count()
        complete_service(self.record, completed_odometer=self.vehicle.current_odometer + 500, actor=self.technician)
        self.assertEqual(self._event_count() - before, 1)
        event = TimelineEvent.objects.filter(service_record=self.record).latest("created_at")
        self.assertEqual(event.actor, self.technician)
        self.assertEqual(event.old_value, ServiceRecord.Status.IN_SERVICE)
        self.assertEqual(event.new_value, ServiceRecord.Status.COMPLETED)

    def test_timeline_write_failure_rolls_back_the_whole_transition(self):
        # Forces the failure goal 9 cares about: if the TimelineEvent write
        # fails, everything else in the same transaction.atomic() block --
        # here, the status change and the ServiceAssignment row -- must
        # roll back with it rather than leaving a transition half-applied.
        with patch("fleet.services.TimelineEvent.objects.create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)
        self.assertIsNone(self.record.scheduled_date)
        self.assertFalse(ServiceAssignment.objects.filter(service_record=self.record).exists())


class EnsureDueRecordTests(TestCase):
    """Goal 4: due-record generation fires on either threshold, never
    duplicates, and treats a never-serviced vehicle as not-yet-due."""

    def setUp(self):
        self.manager = make_user("due-mgr@example.com", User.Role.FLEET_MANAGER)

    def test_fires_on_date_threshold_alone(self):
        vehicle = make_vehicle(
            registration_number="DUE-DATE-1",
            current_odometer=1_000,
            next_due_odometer=999_999,
            next_due_date=timezone.localdate() - timedelta(days=1),
        )
        record = ensure_due_record(vehicle)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, ServiceRecord.Status.DUE)
        self.assertIsNone(record.created_by)

    def test_fires_on_odometer_threshold_alone(self):
        vehicle = make_vehicle(
            registration_number="DUE-ODO-1",
            current_odometer=20_000,
            next_due_odometer=15_000,
            next_due_date=timezone.localdate() + timedelta(days=365),
        )
        record = ensure_due_record(vehicle)
        self.assertIsNotNone(record)

    def test_does_not_duplicate_with_an_open_record(self):
        vehicle = make_vehicle(
            registration_number="DUE-DUP-1",
            current_odometer=20_000,
            next_due_odometer=15_000,
            next_due_date=timezone.localdate() + timedelta(days=365),
        )
        make_record(vehicle, self.manager, status=ServiceRecord.Status.DUE)
        record = ensure_due_record(vehicle)
        self.assertIsNone(record)
        self.assertEqual(ServiceRecord.objects.filter(vehicle=vehicle).count(), 1)

    def test_returns_none_for_archived_vehicle(self):
        vehicle = make_vehicle(
            registration_number="DUE-ARCH-1",
            current_odometer=20_000,
            next_due_odometer=15_000,
            is_archived=True,
        )
        self.assertIsNone(ensure_due_record(vehicle))

    def test_returns_none_when_never_serviced(self):
        vehicle = make_vehicle(registration_number="DUE-NEW-1", current_odometer=20_000)
        self.assertIsNone(ensure_due_record(vehicle))

    def test_writes_a_created_timeline_event(self):
        vehicle = make_vehicle(
            registration_number="DUE-EVENT-1",
            current_odometer=20_000,
            next_due_odometer=15_000,
            next_due_date=timezone.localdate() + timedelta(days=365),
        )
        record = ensure_due_record(vehicle)
        event = TimelineEvent.objects.get(service_record=record)
        self.assertEqual(event.event_type, TimelineEvent.EventType.CREATED)
        self.assertIsNone(event.actor)


class OverdueParityTests(TestCase):
    """The queryset filter and the model property must agree on the same
    data -- goals 8/10 rely on the queryset, templates on the property."""

    def test_queryset_matches_property(self):
        manager = make_user("overdue-mgr@example.com", User.Role.FLEET_MANAGER)
        vehicle = make_vehicle(registration_number="OVERDUE-1")
        old_due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        overdue_record = make_record(vehicle, manager, status=ServiceRecord.Status.DUE, due_since=old_due_since)
        fresh_record = make_record(vehicle, manager, status=ServiceRecord.Status.DUE, due_since=timezone.now())
        completed_record = make_record(
            vehicle,
            manager,
            status=ServiceRecord.Status.COMPLETED,
            due_since=old_due_since,
            completed_at=timezone.now(),
            completed_odometer=vehicle.current_odometer,
        )

        overdue_qs = set(ServiceRecord.objects.overdue().values_list("pk", flat=True))
        overdue_by_property = {r.pk for r in ServiceRecord.objects.all() if r.is_overdue}

        self.assertEqual(overdue_qs, overdue_by_property)
        self.assertIn(overdue_record.pk, overdue_qs)
        self.assertNotIn(fresh_record.pk, overdue_qs)
        self.assertNotIn(completed_record.pk, overdue_qs)


class TransitionViewPermissionTests(TestCase):
    """Goal 1/4: a technician not assigned to a record cannot transition
    it, enforced server-side as a 403."""

    def setUp(self):
        self.manager = make_user("tview-mgr@example.com", User.Role.FLEET_MANAGER)
        self.assigned = make_user("tview-assigned@example.com", User.Role.TECHNICIAN)
        self.unassigned = make_user("tview-unassigned@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="TVIEW-1")
        self.record = make_record(self.vehicle, self.manager)
        ServiceAssignment.objects.create(
            service_record=self.record, technician=self.assigned, assigned_by=self.manager
        )

    def test_unassigned_technician_cannot_book(self):
        self.client.force_login(self.unassigned)
        response = self.client.post(
            reverse("service-record-book", args=[self.record.pk]),
            {"scheduled_date": date.today(), "technician": self.assigned.pk},
        )
        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)

    def test_assigned_technician_can_book_and_start(self):
        self.client.force_login(self.assigned)
        response = self.client.post(
            reverse("service-record-book", args=[self.record.pk]),
            {"scheduled_date": date.today(), "technician": self.assigned.pk},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("service-record-start", args=[self.record.pk]))
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.IN_SERVICE)

    def test_illegal_transition_via_view_returns_400_not_500(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("service-record-complete", args=[self.record.pk]),
            {"completed_odometer": self.vehicle.current_odometer},
        )
        self.assertEqual(response.status_code, 400)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)
