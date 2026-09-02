from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import User
from fleet.models import ServiceRecord, TimelineEvent, TimelineImmutableError, Vehicle


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
