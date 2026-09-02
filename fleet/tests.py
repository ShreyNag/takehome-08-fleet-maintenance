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
    assign_technician,
    book_service,
    complete_service,
    ensure_due_record,
    start_service,
    unassign_technician,
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

    def test_book_service_writes_a_status_and_an_assignment_event(self):
        # Booking assigns a technician (goal 4) AND is a first-class
        # assignment (goal 5) -- goal 9 wants every technician assignment
        # on the timeline, so this is two events, not one: the assignment
        # itself, and the status change. (Previously this test asserted
        # exactly one event, from before assignment was a first-class
        # action with its own audit event -- see docs/decisions.md.)
        before = self._event_count()
        book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        self.assertEqual(self._event_count() - before, 2)
        events = list(TimelineEvent.objects.filter(service_record=self.record).order_by("pk"))
        assigned_event, status_event = events[-2], events[-1]

        self.assertEqual(assigned_event.event_type, TimelineEvent.EventType.TECHNICIAN_ASSIGNED)
        self.assertEqual(assigned_event.actor, self.manager)
        self.assertEqual(assigned_event.new_value, str(self.technician))

        self.assertEqual(status_event.event_type, TimelineEvent.EventType.STATUS_CHANGED)
        self.assertEqual(status_event.actor, self.manager)
        self.assertEqual(status_event.old_value, ServiceRecord.Status.DUE)
        self.assertEqual(status_event.new_value, ServiceRecord.Status.BOOKED)

    def test_start_service_writes_exactly_one_event(self):
        book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        before = self._event_count()
        start_service(self.record, actor=self.technician)
        self.assertEqual(self._event_count() - before, 1)
        event = TimelineEvent.objects.filter(service_record=self.record).latest("pk")
        self.assertEqual(event.actor, self.technician)
        self.assertEqual(event.old_value, ServiceRecord.Status.BOOKED)
        self.assertEqual(event.new_value, ServiceRecord.Status.IN_SERVICE)

    def test_complete_service_writes_exactly_one_event(self):
        book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        start_service(self.record, actor=self.technician)
        before = self._event_count()
        complete_service(self.record, completed_odometer=self.vehicle.current_odometer + 500, actor=self.technician)
        self.assertEqual(self._event_count() - before, 1)
        event = TimelineEvent.objects.filter(service_record=self.record).latest("pk")
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


class AssignTechnicianServiceTests(TestCase):
    """Goal 5, service layer: assign_technician / unassign_technician are
    idempotent and write exactly one timeline event per actual change."""

    def setUp(self):
        self.manager = make_user("assign-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("assign-tech@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="ASSIGN-1")
        self.record = make_record(self.vehicle, self.manager)

    def _event_count(self):
        return TimelineEvent.objects.filter(service_record=self.record).count()

    def test_assign_creates_row_and_one_event(self):
        before = self._event_count()
        assignment, created = assign_technician(self.record, self.technician, actor=self.manager)
        self.assertTrue(created)
        self.assertEqual(assignment.technician, self.technician)
        self.assertEqual(assignment.assigned_by, self.manager)
        self.assertEqual(self._event_count() - before, 1)
        event = TimelineEvent.objects.filter(service_record=self.record).latest("pk")
        self.assertEqual(event.event_type, TimelineEvent.EventType.TECHNICIAN_ASSIGNED)
        self.assertEqual(event.actor, self.manager)
        self.assertEqual(event.new_value, str(self.technician))

    def test_assigning_an_already_assigned_technician_is_a_no_op(self):
        assign_technician(self.record, self.technician, actor=self.manager)
        before = self._event_count()
        before_count = ServiceAssignment.objects.filter(service_record=self.record).count()

        assignment, created = assign_technician(self.record, self.technician, actor=self.manager)

        self.assertFalse(created)
        self.assertEqual(ServiceAssignment.objects.filter(service_record=self.record).count(), before_count)
        self.assertEqual(self._event_count(), before)

    def test_unassign_removes_row_and_writes_one_event(self):
        assign_technician(self.record, self.technician, actor=self.manager)
        before = self._event_count()

        removed = unassign_technician(self.record, self.technician, actor=self.manager)

        self.assertTrue(removed)
        self.assertFalse(ServiceAssignment.objects.filter(service_record=self.record, technician=self.technician).exists())
        self.assertEqual(self._event_count() - before, 1)
        event = TimelineEvent.objects.filter(service_record=self.record).latest("pk")
        self.assertEqual(event.event_type, TimelineEvent.EventType.TECHNICIAN_UNASSIGNED)
        self.assertEqual(event.actor, self.manager)
        self.assertEqual(event.old_value, str(self.technician))

    def test_unassigning_a_technician_who_is_not_assigned_is_a_no_op(self):
        before = self._event_count()
        removed = unassign_technician(self.record, self.technician, actor=self.manager)
        self.assertFalse(removed)
        self.assertEqual(self._event_count(), before)


class AssignTechnicianViewTests(TestCase):
    """Goal 5, view layer: manager-only, including against a technician
    already assigned to the record -- FleetManagerRequiredMixin, not the
    manager-or-assignee mixin the rest of the detail page uses."""

    def setUp(self):
        self.manager = make_user("assignview-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("assignview-tech@example.com", User.Role.TECHNICIAN)
        self.other_technician = make_user("assignview-other@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="ASSIGNVIEW-1")
        self.record = make_record(self.vehicle, self.manager)

    def test_manager_can_assign(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("service-record-assign", args=[self.record.pk]),
            {"technician": self.technician.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ServiceAssignment.objects.filter(service_record=self.record, technician=self.technician).exists()
        )

    def test_manager_can_unassign(self):
        ServiceAssignment.objects.create(service_record=self.record, technician=self.technician, assigned_by=self.manager)
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("service-record-unassign", args=[self.record.pk, self.technician.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            ServiceAssignment.objects.filter(service_record=self.record, technician=self.technician).exists()
        )

    def test_technician_gets_403_on_assign(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("service-record-assign", args=[self.record.pk]),
            {"technician": self.technician.pk},
        )
        self.assertEqual(response.status_code, 403)

    def test_already_assigned_technician_still_gets_403_on_assign(self):
        # Goal 5 is explicit: even a technician already on this record
        # cannot add another -- assignment is manager-only, full stop.
        ServiceAssignment.objects.create(service_record=self.record, technician=self.technician, assigned_by=self.manager)
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("service-record-assign", args=[self.record.pk]),
            {"technician": self.other_technician.pk},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            ServiceAssignment.objects.filter(service_record=self.record, technician=self.other_technician).exists()
        )

    def test_technician_gets_403_on_unassign(self):
        ServiceAssignment.objects.create(service_record=self.record, technician=self.technician, assigned_by=self.manager)
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("service-record-unassign", args=[self.record.pk, self.technician.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            ServiceAssignment.objects.filter(service_record=self.record, technician=self.technician).exists()
        )

    def test_already_assigned_technician_still_gets_403_on_unassign(self):
        ServiceAssignment.objects.create(service_record=self.record, technician=self.technician, assigned_by=self.manager)
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("service-record-unassign", args=[self.record.pk, self.technician.pk])
        )
        self.assertEqual(response.status_code, 403)


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


class VehicleTechnicianScopingTests(TestCase):
    """A technician sees only vehicles they have at least one
    ServiceAssignment against, any status, including completed -- and gets
    403 (not 404, not an empty page) on a vehicle they have none on."""

    def setUp(self):
        self.manager = make_user("vscope-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("vscope-tech@example.com", User.Role.TECHNICIAN)
        self.other_technician = make_user("vscope-other@example.com", User.Role.TECHNICIAN)

        self.assigned_vehicle = make_vehicle(registration_number="VSCOPE-ASSIGNED")
        record = make_record(self.assigned_vehicle, self.manager, status=ServiceRecord.Status.DUE)
        ServiceAssignment.objects.create(
            service_record=record, technician=self.technician, assigned_by=self.manager
        )

        self.completed_vehicle = make_vehicle(registration_number="VSCOPE-COMPLETED")
        completed_record = make_record(
            self.completed_vehicle,
            self.manager,
            status=ServiceRecord.Status.COMPLETED,
            completed_at=timezone.now(),
            completed_odometer=self.completed_vehicle.current_odometer,
        )
        ServiceAssignment.objects.create(
            service_record=completed_record, technician=self.technician, assigned_by=self.manager
        )

        self.unassigned_vehicle = make_vehicle(registration_number="VSCOPE-UNASSIGNED")

    def test_technician_sees_only_assigned_vehicles(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-list"))
        registrations = {v.registration_number for v in response.context["vehicles"]}
        self.assertEqual(
            registrations, {"VSCOPE-ASSIGNED", "VSCOPE-COMPLETED"}
        )

    def test_technician_with_completed_assignment_still_sees_vehicle(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-detail", args=[self.completed_vehicle.pk]))
        self.assertEqual(response.status_code, 200)

    def test_technician_gets_403_on_unassigned_vehicle_detail(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-detail", args=[self.unassigned_vehicle.pk]))
        self.assertEqual(response.status_code, 403)

    def test_unrelated_technician_sees_no_vehicles(self):
        self.client.force_login(self.other_technician)
        response = self.client.get(reverse("vehicle-list"))
        self.assertEqual(list(response.context["vehicles"]), [])

    def test_manager_still_sees_everything(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("vehicle-list"))
        registrations = {v.registration_number for v in response.context["vehicles"]}
        self.assertEqual(
            registrations, {"VSCOPE-ASSIGNED", "VSCOPE-COMPLETED", "VSCOPE-UNASSIGNED"}
        )

    def test_manager_archived_list_unaffected(self):
        # ArchivedVehicleListView is FleetManagerRequiredMixin-only, so
        # technician scoping never runs there at all -- confirms it stays
        # that way rather than accidentally picking up the new mixin.
        self.unassigned_vehicle.is_archived = True
        self.unassigned_vehicle.save(update_fields=["is_archived"])
        self.client.force_login(self.manager)
        response = self.client.get(reverse("vehicle-archived-list"))
        self.assertIn(self.unassigned_vehicle, response.context["vehicles"])

    def test_technician_gets_403_not_redirect_on_archived_list(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-archived-list"))
        self.assertEqual(response.status_code, 403)


class VehicleServiceStatusAnnotationTests(TestCase):
    """The four service_status states, and that the queryset stays a
    single query regardless of vehicle count (no N+1 from the
    annotation)."""

    def setUp(self):
        self.manager = make_user("vstatus-mgr@example.com", User.Role.FLEET_MANAGER)

    def test_not_yet_serviced_when_both_next_due_fields_are_null(self):
        vehicle = make_vehicle(registration_number="VSTATUS-NEW")
        status = Vehicle.objects.with_service_status().get(pk=vehicle.pk).service_status
        self.assertEqual(status, Vehicle.ServiceStatus.NOT_YET_SERVICED.label)

    def test_ok_when_no_open_record_and_next_due_is_in_the_future(self):
        vehicle = make_vehicle(
            registration_number="VSTATUS-OK",
            next_due_date=timezone.localdate() + timedelta(days=30),
            next_due_odometer=99_999,
        )
        status = Vehicle.objects.with_service_status().get(pk=vehicle.pk).service_status
        self.assertEqual(status, Vehicle.ServiceStatus.OK.label)

    def test_due_when_an_open_non_overdue_record_exists(self):
        vehicle = make_vehicle(
            registration_number="VSTATUS-DUE",
            next_due_date=timezone.localdate(),
            next_due_odometer=99_999,
        )
        make_record(vehicle, self.manager, status=ServiceRecord.Status.DUE, due_since=timezone.now())
        status = Vehicle.objects.with_service_status().get(pk=vehicle.pk).service_status
        self.assertEqual(status, Vehicle.ServiceStatus.DUE.label)

    def test_overdue_when_the_open_due_record_is_past_grace(self):
        vehicle = make_vehicle(
            registration_number="VSTATUS-OVERDUE",
            next_due_date=timezone.localdate() - timedelta(days=30),
            next_due_odometer=99_999,
        )
        old_due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        make_record(vehicle, self.manager, status=ServiceRecord.Status.DUE, due_since=old_due_since)
        status = Vehicle.objects.with_service_status().get(pk=vehicle.pk).service_status
        self.assertEqual(status, Vehicle.ServiceStatus.OVERDUE.label)

    def test_overdue_takes_priority_over_a_second_open_record(self):
        # Not a case the app is expected to create on its own (ensure_due_
        # record refuses to duplicate an open record), but the annotation
        # itself shouldn't silently pick the wrong one if it ever happens.
        vehicle = make_vehicle(
            registration_number="VSTATUS-MIXED",
            next_due_date=timezone.localdate() - timedelta(days=30),
            next_due_odometer=99_999,
        )
        old_due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        make_record(vehicle, self.manager, status=ServiceRecord.Status.DUE, due_since=old_due_since)
        make_record(vehicle, self.manager, status=ServiceRecord.Status.BOOKED, scheduled_date=date.today())
        status = Vehicle.objects.with_service_status().get(pk=vehicle.pk).service_status
        self.assertEqual(status, Vehicle.ServiceStatus.OVERDUE.label)

    def test_completed_only_history_does_not_count_as_open(self):
        vehicle = make_vehicle(
            registration_number="VSTATUS-COMPLETED",
            next_due_date=timezone.localdate() + timedelta(days=30),
            next_due_odometer=99_999,
        )
        make_record(
            vehicle,
            self.manager,
            status=ServiceRecord.Status.COMPLETED,
            completed_at=timezone.now(),
            completed_odometer=vehicle.current_odometer,
        )
        status = Vehicle.objects.with_service_status().get(pk=vehicle.pk).service_status
        self.assertEqual(status, Vehicle.ServiceStatus.OK.label)

    def test_query_count_does_not_grow_with_vehicle_count(self):
        for i in range(3):
            make_vehicle(registration_number=f"VSTATUS-N-{i}")
        self.client.force_login(self.manager)
        with self.assertNumQueries(3):
            self.client.get(reverse("vehicle-list"))

        for i in range(7):
            make_vehicle(registration_number=f"VSTATUS-N2-{i}")
        with self.assertNumQueries(3):
            self.client.get(reverse("vehicle-list"))


class TechnicianAssignedViaBookingVisibilityTests(TestCase):
    """Regression coverage from an earlier bug report: a technician
    assigned via booking could not see the vehicle or the service record
    afterward. Diagnosis found no bug in the code as it stood, but this
    exact flow -- manager books a record with a technician, then that
    technician requests both detail pages -- wasn't directly covered
    anywhere, and this session reworks book_service's assignment path, so
    it's worth locking in now."""

    def setUp(self):
        self.manager = make_user("bug-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("bug-tech@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="BUG-1")
        self.record = make_record(self.vehicle, self.manager, status=ServiceRecord.Status.DUE)
        book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)

    def test_assignment_row_exists_with_assigned_by_set(self):
        assignment = ServiceAssignment.objects.get(service_record=self.record, technician=self.technician)
        self.assertEqual(assignment.assigned_by, self.manager)

    def test_technician_can_view_vehicle_detail(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-detail", args=[self.vehicle.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.record, response.context["service_records"])

    def test_technician_can_view_service_record_detail(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("service-record-detail", args=[self.record.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["service_record"], self.record)

    def test_technician_sees_the_vehicle_on_the_vehicle_list(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("vehicle-list"))
        self.assertIn(self.vehicle, response.context["vehicles"])


class ServiceRecordListViewTests(TestCase):
    """Goal 6: search, filters (combined), sanitised sort in both
    directions, role scoping, pagination with a correct total count that
    survives onto later pages, and no N+1. Goal 5's technician landing page
    reuses this same view/URL -- covered here too rather than duplicated."""

    def setUp(self):
        self.manager = make_user("list-mgr@example.com", User.Role.FLEET_MANAGER)
        self.tech_a = make_user("list-tech-a@example.com", User.Role.TECHNICIAN)
        self.tech_b = make_user("list-tech-b@example.com", User.Role.TECHNICIAN)

        self.vehicle_1 = make_vehicle(registration_number="LIST-1")
        self.vehicle_2 = make_vehicle(registration_number="LIST-2")

        self.record_1 = make_record(
            self.vehicle_1, self.manager, status=ServiceRecord.Status.DUE, description="Replace brake pads"
        )
        ServiceAssignment.objects.create(service_record=self.record_1, technician=self.tech_a, assigned_by=self.manager)

        self.record_2 = make_record(
            self.vehicle_2,
            self.manager,
            status=ServiceRecord.Status.BOOKED,
            description="Oil and filter change",
            scheduled_date=date(2026, 3, 1),
        )
        ServiceAssignment.objects.create(service_record=self.record_2, technician=self.tech_b, assigned_by=self.manager)

        self.record_3 = make_record(
            self.vehicle_1,
            self.manager,
            status=ServiceRecord.Status.BOOKED,
            description="Annual inspection",
            scheduled_date=date(2026, 1, 15),
        )
        # No technician on record_3 -- neither tech_a nor tech_b should see it.

    def _get(self, **params):
        return self.client.get(reverse("service-record-list"), params)

    def test_manager_sees_every_record(self):
        self.client.force_login(self.manager)
        response = self._get()
        self.assertEqual(set(response.context["service_records"]), {self.record_1, self.record_2, self.record_3})

    def test_technician_sees_only_their_own_records_across_vehicles(self):
        self.client.force_login(self.tech_a)
        response = self._get()
        self.assertEqual(list(response.context["service_records"]), [self.record_1])

    def test_technician_with_records_on_multiple_vehicles_sees_all_of_them(self):
        ServiceAssignment.objects.create(service_record=self.record_2, technician=self.tech_a, assigned_by=self.manager)
        self.client.force_login(self.tech_a)
        response = self._get()
        self.assertEqual(set(response.context["service_records"]), {self.record_1, self.record_2})

    def test_search_matches_description(self):
        self.client.force_login(self.manager)
        response = self._get(q="brake")
        self.assertEqual(list(response.context["service_records"]), [self.record_1])

    def test_filter_by_vehicle(self):
        self.client.force_login(self.manager)
        response = self._get(vehicle=self.vehicle_2.pk)
        self.assertEqual(list(response.context["service_records"]), [self.record_2])

    def test_filter_by_status(self):
        self.client.force_login(self.manager)
        response = self._get(status=ServiceRecord.Status.DUE)
        self.assertEqual(list(response.context["service_records"]), [self.record_1])

    def test_filter_by_technician(self):
        self.client.force_login(self.manager)
        response = self._get(technician=self.tech_b.pk)
        self.assertEqual(list(response.context["service_records"]), [self.record_2])

    def test_filters_combine(self):
        self.client.force_login(self.manager)
        response = self._get(vehicle=self.vehicle_1.pk, status=ServiceRecord.Status.BOOKED)
        self.assertEqual(list(response.context["service_records"]), [self.record_3])

    def test_sort_by_scheduled_date_both_directions(self):
        self.client.force_login(self.manager)
        response = self._get(status=ServiceRecord.Status.BOOKED, sort="scheduled_date", dir="asc")
        self.assertEqual(list(response.context["service_records"]), [self.record_3, self.record_2])

        response = self._get(status=ServiceRecord.Status.BOOKED, sort="scheduled_date", dir="desc")
        self.assertEqual(list(response.context["service_records"]), [self.record_2, self.record_3])

    def test_sort_by_updated_at_both_directions(self):
        ServiceRecord.objects.filter(pk=self.record_1.pk).update(updated_at=timezone.now() - timedelta(days=5))
        ServiceRecord.objects.filter(pk=self.record_2.pk).update(updated_at=timezone.now() - timedelta(days=1))
        ServiceRecord.objects.filter(pk=self.record_3.pk).update(updated_at=timezone.now() - timedelta(days=10))

        self.client.force_login(self.manager)
        response = self._get(sort="updated_at", dir="asc")
        self.assertEqual(list(response.context["service_records"]), [self.record_3, self.record_1, self.record_2])

        response = self._get(sort="updated_at", dir="desc")
        self.assertEqual(list(response.context["service_records"]), [self.record_2, self.record_1, self.record_3])

    def test_invalid_sort_falls_back_and_surfaces_a_message(self):
        self.client.force_login(self.manager)
        response = self._get(sort="vehicle__owner__password")
        self.assertEqual(response.status_code, 200)
        messages_text = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("sort" in m.lower() for m in messages_text))
        # Falls back to the default order rather than erroring or being
        # passed through to order_by() unsanitised.
        self.assertEqual(set(response.context["service_records"]), {self.record_1, self.record_2, self.record_3})

    def test_pagination_total_count_is_correct_with_filters_applied(self):
        for i in range(30):
            make_record(self.vehicle_1, self.manager, status=ServiceRecord.Status.DUE, description=f"Filler {i}")
        self.client.force_login(self.manager)
        response = self._get(status=ServiceRecord.Status.DUE)
        # 30 filler records + record_1, all DUE -- record_2 and record_3 are
        # BOOKED and must not be counted.
        self.assertEqual(response.context["paginator"].count, 31)

    def test_filter_state_survives_pagination(self):
        for i in range(30):
            make_record(self.vehicle_1, self.manager, status=ServiceRecord.Status.DUE, description=f"Brake job {i}")
        self.client.force_login(self.manager)
        response = self._get(status=ServiceRecord.Status.DUE, page=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].number, 2)
        for record in response.context["service_records"]:
            self.assertEqual(record.status, ServiceRecord.Status.DUE)
        self.assertIn("status=", response.context["querystring"])

    def test_query_count_does_not_grow_with_result_count(self):
        self.client.force_login(self.manager)
        with self.assertNumQueries(7):
            self._get()

        for i in range(15):
            extra = make_record(self.vehicle_2, self.manager, status=ServiceRecord.Status.DUE, description=f"Extra {i}")
            ServiceAssignment.objects.create(service_record=extra, technician=self.tech_a, assigned_by=self.manager)

        with self.assertNumQueries(7):
            self._get()
