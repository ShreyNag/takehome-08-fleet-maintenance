from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from fleet.forms import ServiceRecordDescriptionForm, VehicleForm
from fleet.models import ServiceAssignment, ServiceRecord, TimelineEvent, TimelineImmutableError, Vehicle


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
