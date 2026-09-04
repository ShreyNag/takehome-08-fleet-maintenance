import datetime as dt
from datetime import date, timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.http import StreamingHttpResponse
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from fleet.alerts import overdue_alerts
from fleet.csv_io import MAX_IMPORT_ROWS
from fleet.dashboard import dashboard_context
from fleet.forms import ServiceRecordDescriptionForm, VehicleForm
from fleet.models import (
    AlertDismissal,
    ServiceAssignment,
    ServiceRecord,
    TimelineEvent,
    TimelineImmutableError,
    Vehicle,
)
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
    sweep_due_vehicles,
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


class VehicleRegistrationUniquenessTests(TestCase):
    """Bug fix: registration_number is unique at the DB level across every
    vehicle, archived included, but Vehicle.objects (the model's
    _default_manager, since it's declared first -- see the Vehicle
    docstring) excludes archived rows, and that's what ModelForm's
    automatic validate_unique() queries against. A duplicate against an
    ARCHIVED vehicle's plate used to sail past form validation and only
    fail at save() with an uncaught IntegrityError -- a 500, not a form
    error. VehicleForm.clean_registration_number() now checks against
    Vehicle.all_objects instead, so every conflict, archived or not, is
    caught before save() is ever called."""

    def setUp(self):
        self.manager = make_user("vreg-mgr@example.com", User.Role.FLEET_MANAGER)
        self.active = make_vehicle(registration_number="VREG-ACTIVE")
        self.archived = make_vehicle(registration_number="VREG-ARCHIVED", is_archived=True)
        self.client.force_login(self.manager)

    def _payload(self, **overrides):
        payload = {
            "registration_number": "VREG-NEW",
            "make": "Ford",
            "model": "Transit",
            "current_odometer": 500,
            "service_interval_days": 90,
            "service_interval_km": 8_000,
        }
        payload.update(overrides)
        return payload

    def test_create_with_existing_active_registration_is_a_form_error_not_a_500(self):
        response = self.client.post(reverse("vehicle-create"), self._payload(registration_number="VREG-ACTIVE"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "already exists",
            " ".join(response.context["form"].errors["registration_number"]),
        )
        self.assertEqual(Vehicle.all_objects.filter(registration_number="VREG-ACTIVE").count(), 1)

    def test_create_with_archived_registration_is_a_form_error_that_explains_why(self):
        response = self.client.post(reverse("vehicle-create"), self._payload(registration_number="VREG-ARCHIVED"))
        self.assertEqual(response.status_code, 200)
        message = " ".join(response.context["form"].errors["registration_number"])
        self.assertIn("archived", message.lower())
        self.assertEqual(Vehicle.all_objects.filter(registration_number="VREG-ARCHIVED").count(), 1)

    def test_create_with_unique_registration_succeeds(self):
        response = self.client.post(reverse("vehicle-create"), self._payload())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Vehicle.objects.filter(registration_number="VREG-NEW").exists())

    def test_edit_to_existing_active_registration_is_a_form_error_not_a_500(self):
        other = make_vehicle(registration_number="VREG-OTHER")
        response = self.client.post(
            reverse("vehicle-update", args=[other.pk]),
            self._payload(registration_number="VREG-ACTIVE"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "already exists",
            " ".join(response.context["form"].errors["registration_number"]),
        )
        other.refresh_from_db()
        self.assertEqual(other.registration_number, "VREG-OTHER")

    def test_edit_to_archived_registration_is_a_form_error_that_explains_why(self):
        other = make_vehicle(registration_number="VREG-OTHER2")
        response = self.client.post(
            reverse("vehicle-update", args=[other.pk]),
            self._payload(registration_number="VREG-ARCHIVED"),
        )
        self.assertEqual(response.status_code, 200)
        message = " ".join(response.context["form"].errors["registration_number"])
        self.assertIn("archived", message.lower())
        other.refresh_from_db()
        self.assertEqual(other.registration_number, "VREG-OTHER2")

    def test_editing_a_vehicle_without_changing_its_own_registration_still_succeeds(self):
        # Guards against the exclude-self logic in clean_registration_number
        # regressing into treating a vehicle's own unchanged plate as a
        # conflict with itself.
        response = self.client.post(
            reverse("vehicle-update", args=[self.active.pk]),
            self._payload(registration_number="VREG-ACTIVE", make="Renamed"),
        )
        self.assertEqual(response.status_code, 302)
        self.active.refresh_from_db()
        self.assertEqual(self.active.make, "Renamed")


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

    def test_past_scheduled_date_is_rejected(self):
        # Booking schedules future work -- a date that's already gone by
        # is meaningless and would silently distort sorting by scheduled
        # date and the dashboard.
        yesterday = date.today() - timedelta(days=1)
        with self.assertRaises(InvalidTransitionInput):
            book_service(self.record, scheduled_date=yesterday, technician=self.technician, actor=self.manager)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)
        self.assertFalse(ServiceAssignment.objects.filter(service_record=self.record).exists())
        self.assertEqual(TimelineEvent.objects.filter(service_record=self.record).count(), 0)

    def test_todays_date_is_accepted(self):
        book_service(self.record, scheduled_date=date.today(), technician=self.technician, actor=self.manager)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.BOOKED)

    def test_future_date_is_accepted(self):
        tomorrow = date.today() + timedelta(days=1)
        book_service(self.record, scheduled_date=tomorrow, technician=self.technician, actor=self.manager)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.BOOKED)
        self.assertEqual(self.record.scheduled_date, tomorrow)


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
    """Goal 1/4/5. Start and complete: a technician not assigned to the
    record cannot transition it, enforced server-side as a 403 -- that IS
    their own work once a manager has booked them onto the record, so an
    assigned technician can do both. Booking is different: it assigns a
    technician (goal 4), so goal 5's manager-only rule applies with no
    exception for self-assignment -- an assigned technician gets 403 on
    booking exactly like an unassigned one, even booking themselves in."""

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

    def test_assigned_technician_cannot_book_even_themselves(self):
        # Goal 5 has no self-assignment exception: booking is the
        # scheduling decision goal 4 gives to managers, and being already
        # assigned to the record doesn't change that.
        before = set(ServiceAssignment.objects.filter(service_record=self.record).values_list("technician_id", flat=True))
        self.client.force_login(self.assigned)
        response = self.client.post(
            reverse("service-record-book", args=[self.record.pk]),
            {"scheduled_date": date.today(), "technician": self.assigned.pk},
        )
        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)
        after = set(ServiceAssignment.objects.filter(service_record=self.record).values_list("technician_id", flat=True))
        self.assertEqual(before, after)

    def test_assigned_technician_cannot_book_another_technician(self):
        other_technician = make_user("tview-other@example.com", User.Role.TECHNICIAN)
        self.client.force_login(self.assigned)
        response = self.client.post(
            reverse("service-record-book", args=[self.record.pk]),
            {"scheduled_date": date.today(), "technician": other_technician.pk},
        )
        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)
        self.assertFalse(
            ServiceAssignment.objects.filter(service_record=self.record, technician=other_technician).exists()
        )

    def test_manager_can_book(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("service-record-book", args=[self.record.pk]),
            {"scheduled_date": date.today(), "technician": self.assigned.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.BOOKED)

    def test_manager_cannot_book_a_past_date(self):
        yesterday = date.today() - timedelta(days=1)
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("service-record-book", args=[self.record.pk]),
            {"scheduled_date": yesterday, "technician": self.assigned.pk},
        )
        self.assertEqual(response.status_code, 400)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.DUE)
        self.assertIsNone(self.record.scheduled_date)

    def test_manager_can_book_a_different_technician_than_the_one_already_assigned(self):
        other_technician = make_user("tview-other2@example.com", User.Role.TECHNICIAN)
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("service-record-book", args=[self.record.pk]),
            {"scheduled_date": date.today(), "technician": other_technician.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.BOOKED)
        self.assertTrue(
            ServiceAssignment.objects.filter(service_record=self.record, technician=other_technician).exists()
        )

    def test_assigned_technician_can_start_and_complete_once_booked(self):
        # Booking is manager-only, but starting and completing their own
        # booked work is unchanged -- that's the technician's job, not an
        # assignment action.
        book_service(self.record, scheduled_date=date.today(), technician=self.assigned, actor=self.manager)
        self.client.force_login(self.assigned)

        response = self.client.post(reverse("service-record-start", args=[self.record.pk]))
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.IN_SERVICE)

        response = self.client.post(
            reverse("service-record-complete", args=[self.record.pk]),
            {"completed_odometer": self.vehicle.current_odometer},
        )
        self.assertEqual(response.status_code, 302)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.COMPLETED)

    def test_unassigned_technician_cannot_start(self):
        book_service(self.record, scheduled_date=date.today(), technician=self.assigned, actor=self.manager)
        self.client.force_login(self.unassigned)
        response = self.client.post(reverse("service-record-start", args=[self.record.pk]))
        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, ServiceRecord.Status.BOOKED)

    def test_unassigned_technician_cannot_complete(self):
        book_service(self.record, scheduled_date=date.today(), technician=self.assigned, actor=self.manager)
        start_service(self.record, actor=self.assigned)
        self.client.force_login(self.unassigned)
        response = self.client.post(
            reverse("service-record-complete", args=[self.record.pk]),
            {"completed_odometer": self.vehicle.current_odometer},
        )
        self.assertEqual(response.status_code, 403)
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
        # 3 for the page itself (session, user, the annotated vehicle
        # query) + 1 for goal 10's nav alert badge, forced on every manager
        # page by the SimpleLazyObject in fleet.context_processors.alerts.
        for i in range(3):
            make_vehicle(registration_number=f"VSTATUS-N-{i}")
        self.client.force_login(self.manager)
        with self.assertNumQueries(4):
            self.client.get(reverse("vehicle-list"))

        for i in range(7):
            make_vehicle(registration_number=f"VSTATUS-N2-{i}")
        with self.assertNumQueries(4):
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

    def test_technician_filter_dropdown_only_shows_their_own_vehicles(self):
        # tech_a is assigned to record_1 (on vehicle_1) only -- vehicle_2
        # never appears for them, even though it's a valid registration in
        # the fleet, same rule VehicleListView already enforces (goal 5's
        # scoping, decision #12) and the same shape as the booking-
        # permission bug: a capability locked down on its named surface
        # (the vehicle list) and left open on a sibling one (this dropdown).
        self.client.force_login(self.tech_a)
        response = self._get()
        registrations = {v.registration_number for v in response.context["vehicles"]}
        self.assertEqual(registrations, {"LIST-1"})

    def test_manager_filter_dropdown_still_shows_every_vehicle(self):
        self.client.force_login(self.manager)
        response = self._get()
        registrations = {v.registration_number for v in response.context["vehicles"]}
        self.assertEqual(registrations, {"LIST-1", "LIST-2"})

    def test_technician_technicians_dropdown_only_shows_technicians_they_share_a_record_with(self):
        # tech_a starts out sharing no record with tech_b (record_1 vs.
        # record_2). Adding tech_a onto record_2 alongside tech_b makes
        # tech_b visible in tech_a's dropdown -- but tech_c, who shares no
        # record with tech_a at all, must not appear even though they're a
        # valid technician account, same "would filter to nothing" reasoning
        # as the vehicle dropdown above.
        ServiceAssignment.objects.create(service_record=self.record_2, technician=self.tech_a, assigned_by=self.manager)
        tech_c = make_user("list-tech-c@example.com", User.Role.TECHNICIAN)
        self.client.force_login(self.tech_a)
        response = self._get()
        emails = {t.email for t in response.context["technicians"]}
        self.assertEqual(emails, {self.tech_a.email, self.tech_b.email})
        self.assertNotIn(tech_c.email, emails)

    def test_manager_technicians_dropdown_still_shows_every_technician(self):
        # Includes a technician with zero assignments -- the manager's
        # dropdown must not drop them just because they'd never show up in
        # a "shares a record with the viewer" style scoping.
        idle_tech = make_user("list-tech-idle@example.com", User.Role.TECHNICIAN)
        self.client.force_login(self.manager)
        response = self._get()
        emails = {t.email for t in response.context["technicians"]}
        self.assertEqual(emails, {self.tech_a.email, self.tech_b.email, idle_tech.email})

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
        # 7 for the page itself (session, user, pagination count, filter
        # dropdown data, page of records + N+1-safe joins/prefetch) + 1 for
        # goal 10's nav alert badge -- the manager nav always renders it, so
        # the context processor's SimpleLazyObject count() gets forced on
        # every manager page, not just the alerts page itself.
        with self.assertNumQueries(8):
            self._get()

        for i in range(15):
            extra = make_record(self.vehicle_2, self.manager, status=ServiceRecord.Status.DUE, description=f"Extra {i}")
            ServiceAssignment.objects.create(service_record=extra, technician=self.tech_a, assigned_by=self.manager)

        with self.assertNumQueries(8):
            self._get()


def make_csv(content):
    return SimpleUploadedFile("readings.csv", content.encode("utf-8"), content_type="text/csv")


class CsvImportTests(TestCase):
    """Goal 7's central requirement: every row judged independently, valid
    rows applied even when other rows in the same file are rejected, and
    every one of the six rejection reasons produces its own message."""

    def setUp(self):
        self.manager = make_user("csv-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("csv-tech@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="CSV-1", current_odometer=10_000)
        self.other_vehicle = make_vehicle(registration_number="CSV-2", current_odometer=5_000)
        self.archived_vehicle = make_vehicle(
            registration_number="CSV-ARCHIVED", current_odometer=1_000, is_archived=True
        )

    def _post(self, content):
        self.client.force_login(self.manager)
        return self.client.post(
            reverse("vehicle-odometer-import"), {"file": make_csv(content)}
        )

    def test_technician_gets_403(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("vehicle-odometer-import"), {"file": make_csv("CSV-1,12000\n")}
        )
        self.assertEqual(response.status_code, 403)

    def test_valid_row_updates_the_vehicle(self):
        self._post("CSV-1,12000\n")
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.current_odometer, 12_000)

    def test_mixed_valid_and_invalid_applies_valid_rows_and_reports_each_rejection(self):
        response = self._post(
            "CSV-1,12000\n"
            "NOT-A-VEHICLE,5000\n"
            "CSV-2,-10\n"
        )
        report = response.context["report"]
        self.assertEqual(report.total_rows, 3)
        self.assertEqual(report.succeeded, 1)
        self.assertEqual(report.rejected, 2)

        self.vehicle.refresh_from_db()
        self.other_vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.current_odometer, 12_000)
        self.assertEqual(self.other_vehicle.current_odometer, 5_000)  # unchanged -- its row was rejected

    def test_malformed_row_rejected(self):
        response = self._post("CSV-1\n")  # missing the reading column
        report = response.context["report"]
        self.assertEqual(report.rejected, 1)
        self.assertIn("Malformed", report.rejected_rows[0].reason)

    def test_non_integer_reading_rejected(self):
        response = self._post("CSV-1,not-a-number\n")
        report = response.context["report"]
        self.assertEqual(report.rejected, 1)
        self.assertIn("valid whole number", report.rejected_rows[0].reason)

    def test_single_bad_row_is_not_mistaken_for_a_header(self):
        # A lone data row with an unparseable reading looks -- by the
        # naive "does column 2 parse as an int" signal -- exactly like a
        # header. It must still be reported as a rejected row, not
        # silently swallowed as a header with zero data rows.
        response = self._post("CSV-1,not-a-number\n")
        report = response.context["report"]
        self.assertEqual(report.total_rows, 1)
        self.assertEqual(report.rejected, 1)

    def test_negative_reading_rejected(self):
        response = self._post("CSV-1,-500\n")
        report = response.context["report"]
        self.assertEqual(report.rejected, 1)
        self.assertIn("negative", report.rejected_rows[0].reason.lower())

    def test_duplicate_registration_first_occurrence_wins(self):
        response = self._post("CSV-1,11000\nCSV-1,13000\n")
        report = response.context["report"]
        self.assertEqual(report.succeeded, 1)
        self.assertEqual(report.rejected, 1)
        self.assertIn("Duplicate", report.rejected_rows[0].reason)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.current_odometer, 11_000)  # the first row won, not the second

    def test_registration_not_found_rejected(self):
        response = self._post("NO-SUCH-REG,5000\n")
        report = response.context["report"]
        self.assertEqual(report.rejected, 1)
        self.assertIn("not found", report.rejected_rows[0].reason.lower())

    def test_archived_vehicle_rejected_distinctly_from_not_found(self):
        response = self._post("CSV-ARCHIVED,2000\n")
        report = response.context["report"]
        self.assertEqual(report.rejected, 1)
        self.assertIn("archived", report.rejected_rows[0].reason.lower())
        self.archived_vehicle.refresh_from_db()
        self.assertEqual(self.archived_vehicle.current_odometer, 1_000)

    def test_reading_lower_than_current_rejected(self):
        response = self._post("CSV-1,9000\n")
        report = response.context["report"]
        self.assertEqual(report.rejected, 1)
        self.assertIn("lower", report.rejected_rows[0].reason.lower())

    def test_rejected_row_leaves_the_vehicle_completely_unchanged(self):
        before = (self.vehicle.current_odometer, self.vehicle.next_due_date, self.vehicle.next_due_odometer)
        self._post("CSV-1,9000\n")  # rejected: lower than current
        self.vehicle.refresh_from_db()
        after = (self.vehicle.current_odometer, self.vehicle.next_due_date, self.vehicle.next_due_odometer)
        self.assertEqual(before, after)

    def test_successful_row_crossing_mileage_threshold_creates_a_due_record(self):
        self.vehicle.next_due_odometer = 12_000
        self.vehicle.next_due_date = date.today() + timedelta(days=365)
        self.vehicle.save(update_fields=["next_due_odometer", "next_due_date"])

        self._post("CSV-1,15000\n")

        self.assertTrue(
            ServiceRecord.objects.filter(vehicle=self.vehicle, status=ServiceRecord.Status.DUE).exists()
        )

    def test_header_row_is_skipped_when_present(self):
        response = self._post("registration_number,odometer\nCSV-1,12000\n")
        report = response.context["report"]
        self.assertEqual(report.total_rows, 1)
        self.assertEqual(report.succeeded, 1)

    def test_works_without_a_header_row(self):
        response = self._post("CSV-1,12000\n")
        report = response.context["report"]
        self.assertEqual(report.total_rows, 1)
        self.assertEqual(report.succeeded, 1)

    def test_extra_columns_are_ignored(self):
        response = self._post("CSV-1,12000,serviced by Bob,note\n")
        report = response.context["report"]
        self.assertEqual(report.succeeded, 1)

    def test_whitespace_and_blank_lines_are_tolerated(self):
        response = self._post("  CSV-1 , 12000 \n\n\nCSV-2,6000\n")
        report = response.context["report"]
        self.assertEqual(report.succeeded, 2)

    def test_crlf_line_endings_are_handled(self):
        response = self._post("CSV-1,12000\r\nCSV-2,6000\r\n")
        report = response.context["report"]
        self.assertEqual(report.succeeded, 2)

    def test_bom_on_first_cell_is_stripped(self):
        content = "﻿CSV-1,12000\n"
        response = self._post(content)
        report = response.context["report"]
        self.assertEqual(report.succeeded, 1)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.current_odometer, 12_000)

    def test_non_csv_file_is_rejected_cleanly(self):
        self.client.force_login(self.manager)
        upload = SimpleUploadedFile("readings.txt", b"CSV-1,12000\n", content_type="text/plain")
        response = self.client.post(reverse("vehicle-odometer-import"), {"file": upload})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("report", response.context)

    def test_row_cap_exceeded_is_rejected_before_touching_the_database(self):
        content = "\n".join(f"CSV-1,{10_000 + i}" for i in range(MAX_IMPORT_ROWS + 1))
        response = self._post(content)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("report", response.context)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.current_odometer, 10_000)


class ServiceRecordExportTests(TestCase):
    """Goal 7's export: StreamingHttpResponse, expected columns, role
    scope (a technician's export contains only their records), and the
    active filters carried over from the list view's querystring."""

    def setUp(self):
        self.manager = make_user("export-mgr@example.com", User.Role.FLEET_MANAGER)
        self.tech_a = make_user("export-tech-a@example.com", User.Role.TECHNICIAN)
        self.tech_b = make_user("export-tech-b@example.com", User.Role.TECHNICIAN)
        self.vehicle = make_vehicle(registration_number="EXPORT-1")

        self.record_a = make_record(self.vehicle, self.manager, status=ServiceRecord.Status.DUE, description="Brake job")
        ServiceAssignment.objects.create(service_record=self.record_a, technician=self.tech_a, assigned_by=self.manager)

        self.record_b = make_record(self.vehicle, self.manager, status=ServiceRecord.Status.DUE, description="Oil change")
        ServiceAssignment.objects.create(service_record=self.record_b, technician=self.tech_b, assigned_by=self.manager)

    def _rows(self, response):
        import csv as csv_module
        import io as io_module

        content = b"".join(response.streaming_content).decode("utf-8")
        return list(csv_module.reader(io_module.StringIO(content)))

    def test_response_is_streaming(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("service-record-export"))
        self.assertIsInstance(response, StreamingHttpResponse)
        self.assertEqual(response["Content-Type"], "text/csv")

    def test_expected_columns(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("service-record-export"))
        rows = self._rows(response)
        self.assertEqual(
            rows[0], ["Vehicle", "Status", "Scheduled date", "Completed at", "Description", "Technicians"]
        )

    def test_manager_export_contains_every_record(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("service-record-export"))
        rows = self._rows(response)
        descriptions = {row[4] for row in rows[1:]}
        self.assertEqual(descriptions, {"Brake job", "Oil change"})

    def test_technician_export_contains_only_their_own_records(self):
        self.client.force_login(self.tech_a)
        response = self.client.get(reverse("service-record-export"))
        rows = self._rows(response)
        descriptions = {row[4] for row in rows[1:]}
        self.assertEqual(descriptions, {"Brake job"})

    def test_export_respects_active_filters(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("service-record-export"), {"q": "brake"})
        rows = self._rows(response)
        descriptions = {row[4] for row in rows[1:]}
        self.assertEqual(descriptions, {"Brake job"})


class DashboardHeadlineNumberTests(TestCase):
    """Goal 8's four headline numbers, each against known fixture data --
    one vehicle per number so a wrong aggregate can't hide behind another
    one happening to cancel it out."""

    def setUp(self):
        self.manager = make_user("dash-headline-mgr@example.com", User.Role.FLEET_MANAGER)

    def test_headline_numbers_match_fixture_data(self):
        due_vehicle = make_vehicle(
            registration_number="DASH-DUE",
            next_due_date=timezone.localdate(),
            next_due_odometer=999_999,
        )
        make_record(due_vehicle, self.manager, status=ServiceRecord.Status.DUE, due_since=timezone.now())

        # A DUE record that's also past its grace period is still, quite
        # literally, DUE -- "overdue" is a subset of "due" (it's the aged
        # slice of it), not a separate, mutually-exclusive bucket. So this
        # vehicle counts toward BOTH due_vehicles and overdue_vehicles.
        overdue_vehicle = make_vehicle(
            registration_number="DASH-OVERDUE",
            next_due_date=timezone.localdate() - timedelta(days=30),
            next_due_odometer=999_999,
        )
        old_due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        make_record(overdue_vehicle, self.manager, status=ServiceRecord.Status.DUE, due_since=old_due_since)

        in_service_vehicle = make_vehicle(registration_number="DASH-INSVC")
        make_record(in_service_vehicle, self.manager, status=ServiceRecord.Status.IN_SERVICE)

        completed_vehicle = make_vehicle(registration_number="DASH-COMPLETED")
        make_record(
            completed_vehicle,
            self.manager,
            status=ServiceRecord.Status.COMPLETED,
            completed_at=timezone.now(),
            completed_odometer=completed_vehicle.current_odometer,
        )

        # Present in the fleet but shouldn't move any of the four numbers.
        make_vehicle(
            registration_number="DASH-OK",
            next_due_date=timezone.localdate() + timedelta(days=30),
            next_due_odometer=999_999,
        )
        make_vehicle(registration_number="DASH-NEW")

        context = dashboard_context()
        self.assertEqual(context["due_vehicles"], 2)
        self.assertEqual(context["overdue_vehicles"], 1)
        self.assertEqual(context["in_service_vehicles"], 1)
        self.assertEqual(context["completed_this_week"], 1)

    def test_in_service_vehicle_is_not_also_counted_as_due(self):
        # Regression coverage: with_service_status() alone can't tell
        # BOOKED/IN_SERVICE apart from DUE (see dashboard.py's docstring),
        # so an earlier version of this aggregate double-counted an
        # in-service vehicle as also "due". due_vehicles and
        # in_service_vehicles must partition, not overlap.
        vehicle = make_vehicle(registration_number="DASH-PARTITION")
        make_record(vehicle, self.manager, status=ServiceRecord.Status.IN_SERVICE)

        context = dashboard_context()
        self.assertEqual(context["due_vehicles"], 0)
        self.assertEqual(context["in_service_vehicles"], 1)

    def test_status_breakdown_counts_every_record_once(self):
        vehicle = make_vehicle(registration_number="DASH-STATUS")
        make_record(vehicle, self.manager, status=ServiceRecord.Status.DUE, due_since=timezone.now())
        make_record(
            make_vehicle(registration_number="DASH-STATUS-2"),
            self.manager,
            status=ServiceRecord.Status.BOOKED,
            scheduled_date=timezone.localdate(),
        )
        counts = {row["status"]: row["count"] for row in dashboard_context()["status_breakdown"]}
        self.assertEqual(counts.get(ServiceRecord.Status.DUE, 0), 1)
        self.assertEqual(counts.get(ServiceRecord.Status.BOOKED, 0), 1)

    def test_technician_breakdown_counts_assigned_records(self):
        technician = make_user("dash-tech-breakdown@example.com", User.Role.TECHNICIAN)
        vehicle = make_vehicle(registration_number="DASH-TECH")
        record = make_record(vehicle, self.manager, status=ServiceRecord.Status.DUE, due_since=timezone.now())
        assign_technician(record, technician, actor=self.manager)

        breakdown = {row.pk: row.record_count for row in dashboard_context()["technician_breakdown"]}
        self.assertEqual(breakdown[technician.pk], 1)


class DashboardWeeklySeriesTests(TestCase):
    """Goal 8: the 8-week chart series must show zero-completion weeks as
    zero, not omit them -- a gap in the x-axis is a bug, per the brief."""

    def setUp(self):
        self.manager = make_user("dash-week-mgr@example.com", User.Role.FLEET_MANAGER)

    def test_series_has_eight_weeks_with_zero_filled_gaps(self):
        vehicle = make_vehicle(registration_number="DASH-WEEK")
        make_record(
            vehicle,
            self.manager,
            status=ServiceRecord.Status.COMPLETED,
            completed_at=timezone.now(),
            completed_odometer=vehicle.current_odometer,
        )

        weekly = dashboard_context()["weekly_series"]
        self.assertEqual(len(weekly), 8)
        non_zero_weeks = [week for week in weekly if week["count"] > 0]
        self.assertEqual(len(non_zero_weeks), 1)
        self.assertEqual(non_zero_weeks[0]["count"], 1)
        # The one completion is dated "now", so it belongs in the last
        # (current) bucket of the series, not a middle one.
        self.assertEqual(weekly[-1]["count"], 1)
        self.assertEqual(weekly[-1]["pct"], 100)

    def test_series_is_all_zero_with_no_completions(self):
        weekly = dashboard_context()["weekly_series"]
        self.assertEqual(len(weekly), 8)
        self.assertTrue(all(week["count"] == 0 for week in weekly))
        self.assertTrue(all(week["pct"] == 0 for week in weekly))

    def test_completion_eight_weeks_ago_still_falls_inside_the_window(self):
        vehicle = make_vehicle(registration_number="DASH-WEEK-OLD")
        # Computed the same way fleet.dashboard._weekly_completions derives
        # its oldest bucket, rather than approximated with a raw timedelta
        # -- a timedelta offset from "now" (which carries a time-of-day and
        # isn't week-aligned) can land a day either side of the bucket
        # boundary depending on what day the test happens to run.
        today = timezone.localdate()
        current_week_start = today - timedelta(days=today.weekday())
        oldest_week_start = current_week_start - timedelta(weeks=7)
        old_completion = timezone.make_aware(dt.datetime.combine(oldest_week_start, dt.time(hour=12)))
        make_record(
            vehicle,
            self.manager,
            status=ServiceRecord.Status.COMPLETED,
            completed_at=old_completion,
            completed_odometer=vehicle.current_odometer,
        )
        weekly = dashboard_context()["weekly_series"]
        self.assertEqual(weekly[0]["count"], 1)
        self.assertEqual(sum(week["count"] for week in weekly), 1)


class DashboardViewTests(TestCase):
    """Goal 8: manager-only, and a fixed query count that doesn't grow as
    more widgets' worth of data exists -- assertNumQueries so a future
    widget added without care is caught here, not in production."""

    def setUp(self):
        self.manager = make_user("dash-view-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("dash-view-tech@example.com", User.Role.TECHNICIAN)

    def test_technician_gets_403(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_manager_gets_200(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_query_count_is_fixed_regardless_of_fleet_size(self):
        # 2 (session, user) + 5 (dashboard_context()'s own aggregates -- see
        # fleet/dashboard.py's module docstring for what each one is) + 1
        # (goal 10's nav alert badge, forced on every manager page).
        self.client.force_login(self.manager)

        def _make_fixture_data(prefix, n):
            for i in range(n):
                vehicle = make_vehicle(registration_number=f"DASH-QN-{prefix}-{i}")
                make_record(vehicle, self.manager, status=ServiceRecord.Status.DUE, due_since=timezone.now())

        _make_fixture_data("A", 3)
        with self.assertNumQueries(8):
            self.client.get(reverse("dashboard"))

        _make_fixture_data("B", 10)
        with self.assertNumQueries(8):
            self.client.get(reverse("dashboard"))


class AlertListTests(TestCase):
    """Goal 10: the alerts list is exactly overdue_alerts() -- overdue,
    undismissed records -- and nothing a technician can reach."""

    def setUp(self):
        self.manager = make_user("alert-list-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("alert-list-tech@example.com", User.Role.TECHNICIAN)
        old_due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        self.overdue_record = make_record(
            make_vehicle(registration_number="ALERT-OVERDUE"),
            self.manager,
            status=ServiceRecord.Status.DUE,
            due_since=old_due_since,
        )
        self.fresh_due_record = make_record(
            make_vehicle(registration_number="ALERT-FRESH"),
            self.manager,
            status=ServiceRecord.Status.DUE,
            due_since=timezone.now(),
        )
        self.dismissed_record = make_record(
            make_vehicle(registration_number="ALERT-DISMISSED"),
            self.manager,
            status=ServiceRecord.Status.DUE,
            due_since=old_due_since,
        )
        AlertDismissal.objects.create(service_record=self.dismissed_record, dismissed_by=self.manager)

    def test_list_view_matches_overdue_alerts_queryset_exactly(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("alert-list"))
        expected = set(overdue_alerts().values_list("pk", flat=True))
        actual = {record.pk for record in response.context["alerts"]}
        self.assertEqual(actual, expected)
        self.assertEqual(actual, {self.overdue_record.pk})
        self.assertNotIn(self.fresh_due_record.pk, actual)
        self.assertNotIn(self.dismissed_record.pk, actual)

    def test_technician_gets_403(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("alert-list"))
        self.assertEqual(response.status_code, 403)


class AlertDismissTests(TestCase):
    """Goal 10: dismissing is manager-only, POST-only, and removes the
    record from both the list and the badge count."""

    def setUp(self):
        self.manager = make_user("alert-dismiss-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("alert-dismiss-tech@example.com", User.Role.TECHNICIAN)
        old_due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        self.record = make_record(
            make_vehicle(registration_number="ALERT-DISMISS-1"),
            self.manager,
            status=ServiceRecord.Status.DUE,
            due_since=old_due_since,
        )

    def test_dismiss_removes_from_list_and_badge_count(self):
        self.client.force_login(self.manager)
        self.assertEqual(overdue_alerts().count(), 1)

        response = self.client.post(reverse("alert-dismiss", args=[self.record.pk]))
        self.assertRedirects(response, reverse("alert-list"))

        self.assertEqual(overdue_alerts().count(), 0)
        self.assertTrue(
            AlertDismissal.objects.filter(service_record=self.record, dismissed_by=self.manager).exists()
        )

    def test_dismiss_is_idempotent(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("alert-dismiss", args=[self.record.pk]))
        response = self.client.post(reverse("alert-dismiss", args=[self.record.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            AlertDismissal.objects.filter(service_record=self.record, dismissed_by=self.manager).count(), 1
        )

    def test_get_is_not_allowed(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("alert-dismiss", args=[self.record.pk]))
        self.assertEqual(response.status_code, 405)

    def test_technician_gets_403_and_writes_no_dismissal(self):
        self.client.force_login(self.technician)
        response = self.client.post(reverse("alert-dismiss", args=[self.record.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AlertDismissal.objects.filter(service_record=self.record).exists())


class AlertReappearanceTests(TestCase):
    """Goal 10's end-to-end reappearance rule: dismiss, complete the
    service, cross the vehicle's next threshold, and a brand new alert
    appears -- with no extra logic beyond what ensure_due_record and
    AlertDismissal's record-level key already provide, because the
    dismissal references a ServiceRecord that no longer represents the
    vehicle's open work."""

    def test_dismiss_complete_advance_reappears(self):
        manager = make_user("reappear-mgr@example.com", User.Role.FLEET_MANAGER)
        technician = make_user("reappear-tech@example.com", User.Role.TECHNICIAN)
        vehicle = make_vehicle(
            registration_number="REAPPEAR-1",
            current_odometer=10_000,
            service_interval_days=90,
            service_interval_km=8_000,
            next_due_date=timezone.localdate() - timedelta(days=1),
            next_due_odometer=999_999,
        )
        old_due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        first_record = make_record(vehicle, manager, status=ServiceRecord.Status.DUE, due_since=old_due_since)

        # Dismiss the overdue alert.
        AlertDismissal.objects.create(service_record=first_record, dismissed_by=manager)
        self.assertNotIn(first_record.pk, overdue_alerts().values_list("pk", flat=True))

        # Complete it -- moves the record out of DUE and resets the
        # vehicle's due counters from THIS completion's date/odometer
        # (complete_service's own documented behaviour, not re-derived here).
        book_service(first_record, scheduled_date=date.today(), technician=technician, actor=manager)
        start_service(first_record, actor=manager)
        complete_service(first_record, completed_odometer=vehicle.current_odometer, actor=manager)

        # Advance the vehicle past its NEW threshold and re-derive due-ness
        # through the same ensure_due_record() the app itself uses on an
        # odometer edit or a check_due_vehicles run -- not a re-implementation.
        vehicle.refresh_from_db()
        vehicle.next_due_date = timezone.localdate() - timedelta(days=1)
        vehicle.save(update_fields=["next_due_date"])
        second_record = ensure_due_record(vehicle)

        self.assertIsNotNone(second_record)
        self.assertNotEqual(second_record.pk, first_record.pk)

        # Back-date the new record past the grace period, same trick
        # OverdueParityTests/EnsureDueRecordTests already use, so it's
        # overdue immediately rather than waiting on real time to pass.
        second_record.due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        second_record.save(update_fields=["due_since"])

        alert_pks = set(overdue_alerts().values_list("pk", flat=True))
        self.assertIn(second_record.pk, alert_pks)
        self.assertNotIn(first_record.pk, alert_pks)


class AlertBadgeContextProcessorTests(TestCase):
    """Goal 10: the nav badge count is correct for a manager, zero for a
    technician, and lazy -- a technician's page must not pay for the query
    at all, since they never see the badge."""

    def setUp(self):
        self.manager = make_user("badge-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("badge-tech@example.com", User.Role.TECHNICIAN)
        old_due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 1)
        make_record(
            make_vehicle(registration_number="BADGE-1"),
            self.manager,
            status=ServiceRecord.Status.DUE,
            due_since=old_due_since,
        )

    def test_manager_sees_correct_count(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("vehicle-list"))
        self.assertEqual(response.context["overdue_alert_count"], 1)
        self.assertContains(response, "badge-alert-count")

    def test_technician_count_is_zero_and_never_queried(self):
        self.client.force_login(self.technician)
        with patch("fleet.context_processors.overdue_alerts") as mock_overdue:
            response = self.client.get(reverse("service-record-list"))
        mock_overdue.assert_not_called()
        self.assertEqual(response.context["overdue_alert_count"], 0)
        self.assertNotContains(response, "badge-alert-count")


class SweepDueVehiclesTests(TestCase):
    """fleet.services.sweep_due_vehicles -- the shared loop behind both the
    check_due_vehicles management command and CheckDueVehiclesView. Thin on
    purpose: it delegates every actual due-ness decision to
    ensure_due_record, already covered in depth by EnsureDueRecordTests."""

    def test_returns_created_records_only_for_vehicles_that_crossed_a_threshold(self):
        due_vehicle = make_vehicle(
            registration_number="SWEEP-DUE",
            current_odometer=1_000,
            next_due_date=timezone.localdate() - timedelta(days=1),
            next_due_odometer=999_999,
        )
        not_due_vehicle = make_vehicle(
            registration_number="SWEEP-NOTDUE",
            current_odometer=1_000,
            next_due_date=timezone.localdate() + timedelta(days=30),
            next_due_odometer=999_999,
        )
        never_serviced_vehicle = make_vehicle(registration_number="SWEEP-NEW")

        created = sweep_due_vehicles()

        self.assertEqual({record.vehicle_id for record in created}, {due_vehicle.pk})
        self.assertFalse(ServiceRecord.objects.filter(vehicle=not_due_vehicle).exists())
        self.assertFalse(ServiceRecord.objects.filter(vehicle=never_serviced_vehicle).exists())

    def test_does_not_duplicate_an_already_open_record(self):
        vehicle = make_vehicle(
            registration_number="SWEEP-DUP",
            next_due_date=timezone.localdate() - timedelta(days=1),
            next_due_odometer=999_999,
        )
        manager = make_user("sweep-dup-mgr@example.com", User.Role.FLEET_MANAGER)
        make_record(vehicle, manager, status=ServiceRecord.Status.DUE)

        created = sweep_due_vehicles()

        self.assertEqual(created, [])
        self.assertEqual(ServiceRecord.objects.filter(vehicle=vehicle).count(), 1)


@override_settings(DUE_CHECK_TOKEN="sweep-test-token")
class CheckDueVehiclesEndpointTests(TestCase):
    """Goal 4's scheduled-job substitute: a POST endpoint an external
    scheduler hits, since Render's free tier has no cron feature. No
    Django session involved anywhere in these tests -- the token IS the
    auth."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _due_vehicle(self, registration_number):
        return make_vehicle(
            registration_number=registration_number,
            next_due_date=timezone.localdate() - timedelta(days=1),
            next_due_odometer=999_999,
        )

    def test_valid_bearer_token_runs_the_sweep(self):
        vehicle = self._due_vehicle("SWEEP-EP-1")
        response = self.client.post(
            reverse("check-due-vehicles"), HTTP_AUTHORIZATION="Bearer sweep-test-token"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 1)
        self.assertTrue(
            ServiceRecord.objects.filter(vehicle=vehicle, status=ServiceRecord.Status.DUE).exists()
        )

    def test_valid_token_via_query_param(self):
        self._due_vehicle("SWEEP-EP-2")
        response = self.client.post(reverse("check-due-vehicles") + "?token=sweep-test-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 1)

    def test_missing_token_is_forbidden(self):
        self._due_vehicle("SWEEP-EP-3")
        response = self.client.post(reverse("check-due-vehicles"))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ServiceRecord.objects.exists())

    def test_wrong_token_is_forbidden(self):
        response = self.client.post(reverse("check-due-vehicles"), HTTP_AUTHORIZATION="Bearer wrong-token")
        self.assertEqual(response.status_code, 403)

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse("check-due-vehicles") + "?token=sweep-test-token")
        self.assertEqual(response.status_code, 405)

    def test_second_call_within_the_interval_is_rate_limited(self):
        self.client.post(reverse("check-due-vehicles"), HTTP_AUTHORIZATION="Bearer sweep-test-token")
        response = self.client.post(
            reverse("check-due-vehicles"), HTTP_AUTHORIZATION="Bearer sweep-test-token"
        )
        self.assertEqual(response.status_code, 429)


class CheckDueVehiclesUnconfiguredTests(TestCase):
    """No DUE_CHECK_TOKEN set (the out-of-the-box local/default state) --
    the endpoint must fail closed rather than accept anything."""

    def setUp(self):
        cache.clear()

    @override_settings(DUE_CHECK_TOKEN="")
    def test_unconfigured_token_forbids_every_request(self):
        response = self.client.post(reverse("check-due-vehicles"), HTTP_AUTHORIZATION="Bearer anything")
        self.assertEqual(response.status_code, 403)


class SeedDemoCommandTests(TestCase):
    """seed_demo's own contract: idempotent, a real spread of states, and
    never touches a row seed_users owns."""

    def setUp(self):
        call_command("seed_users")

    def test_creates_the_expected_fleet_size(self):
        call_command("seed_demo")
        self.assertEqual(
            Vehicle.all_objects.filter(registration_number__startswith="FC-DEMO-").count(), 30
        )

    def test_running_twice_gives_back_the_same_fleet(self):
        call_command("seed_demo")
        first_pks = set(
            Vehicle.all_objects.filter(registration_number__startswith="FC-DEMO-").values_list(
                "registration_number", flat=True
            )
        )
        call_command("seed_demo")
        second_pks = set(
            Vehicle.all_objects.filter(registration_number__startswith="FC-DEMO-").values_list(
                "registration_number", flat=True
            )
        )
        self.assertEqual(first_pks, second_pks)
        self.assertEqual(
            Vehicle.all_objects.filter(registration_number__startswith="FC-DEMO-").count(), 30
        )

    def test_leaves_seed_users_accounts_untouched(self):
        manager_before = User.objects.get(email="manager@fleetcare.demo")
        technician_before = User.objects.get(email="tech@fleetcare.demo")

        call_command("seed_demo")
        call_command("seed_demo")

        manager_after = User.objects.get(email="manager@fleetcare.demo")
        technician_after = User.objects.get(email="tech@fleetcare.demo")
        self.assertEqual(manager_before.pk, manager_after.pk)
        self.assertEqual(manager_before.password, manager_after.password)
        self.assertEqual(technician_before.pk, technician_after.pk)
        self.assertEqual(technician_before.password, technician_after.password)

    def test_produces_a_spread_of_service_states_including_never_serviced(self):
        call_command("seed_demo")
        statuses = set(
            Vehicle.objects.filter(registration_number__startswith="FC-DEMO-")
            .with_service_status()
            .values_list("service_status", flat=True)
        )
        self.assertIn(Vehicle.ServiceStatus.NOT_YET_SERVICED.label, statuses)
        self.assertIn(Vehicle.ServiceStatus.OVERDUE.label, statuses)
        self.assertIn(Vehicle.ServiceStatus.OK.label, statuses)

        seeded_records = ServiceRecord.objects.filter(vehicle__registration_number__startswith="FC-DEMO-")
        self.assertTrue(seeded_records.filter(status=ServiceRecord.Status.BOOKED).exists())
        self.assertTrue(seeded_records.filter(status=ServiceRecord.Status.IN_SERVICE).exists())
        self.assertTrue(seeded_records.filter(status=ServiceRecord.Status.COMPLETED).exists())

        archived_count = Vehicle.all_objects.filter(
            registration_number__startswith="FC-DEMO-", is_archived=True
        ).count()
        self.assertGreaterEqual(archived_count, 1)

    def test_completed_records_have_a_full_timeline(self):
        call_command("seed_demo")
        completed = ServiceRecord.objects.filter(
            vehicle__registration_number__startswith="FC-DEMO-", status=ServiceRecord.Status.COMPLETED
        ).first()
        self.assertIsNotNone(completed)
        event_types = set(completed.timeline.values_list("event_type", flat=True))
        self.assertEqual(
            event_types,
            {
                TimelineEvent.EventType.CREATED,
                TimelineEvent.EventType.TECHNICIAN_ASSIGNED,
                TimelineEvent.EventType.STATUS_CHANGED,
            },
        )
        # CREATED(1) + booking's STATUS_CHANGED + TECHNICIAN_ASSIGNED pair
        # (decision #16 -- booking writes both) + start's STATUS_CHANGED(1)
        # + complete's STATUS_CHANGED(1) = 5 timeline rows in total.
        self.assertEqual(completed.timeline.count(), 5)

    def test_completions_are_spread_across_the_eight_week_chart_window(self):
        call_command("seed_demo")
        weekly = dashboard_context()["weekly_series"]
        self.assertEqual(len(weekly), 8)
        non_zero_weeks = [week for week in weekly if week["count"] > 0]
        # "a real shape rather than one bar" -- more than a single week
        # should have completions in it.
        self.assertGreater(len(non_zero_weeks), 1)

    def test_requires_seed_users_manager_to_already_exist(self):
        User.objects.filter(email="manager@fleetcare.demo").delete()
        with self.assertRaises(CommandError):
            call_command("seed_demo")


