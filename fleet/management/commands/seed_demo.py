"""Seed a demonstrable fleet -- ~30 vehicles spanning every state a
reviewer needs to see, eight weeks of completed service history so goal
8's dashboard chart has a real shape, and several technicians with
assignments spread across vehicles so the by-technician breakdown means
something.

Idempotent: every row this command creates is tagged with a registration-
number prefix (SEED_PREFIX), and handle() clears everything under that
prefix -- vehicles, their service records, and (via CASCADE) the
timeline/assignment/dismissal rows that hang off those records -- before
recreating it, so running this twice gives back the same fleet rather
than doubling it. Confirmed empirically that cascading through
ServiceRecord's delete doesn't hit TimelineEvent's immutability guard:
that guard is enforced at the instance/queryset .delete() call Django's
deletion Collector never goes through for a cascade, only at the
application-code paths it exists to protect.

Deliberately NOT wired into build.sh, per the brief -- this is demo data
run by hand, not something every deploy should redo. See DEPLOY.md for
how to run it against the deployed database.

Never creates, deletes, or modifies a row seed_users owns: the fleet
manager used as the actor for every write below is looked up (never
created) from manager@fleetcare.demo, and the technicians this command
needs are its own accounts, created with get_or_create and never deleted
on a re-run -- only the vehicles/records get cleared and rebuilt.

The historical completions are produced by actually driving
fleet.services' state machine (book_service / start_service /
complete_service) with django.utils.timezone.now patched to a backdated
instant, rather than hand-building ServiceRecord/TimelineEvent rows --
that's what gives a completed seed record its full CREATED /
TECHNICIAN_ASSIGNED / STATUS_CHANGED(x3) event chain for free, consistent
with how a real completed service would look, instead of a bare row.
"""

from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from fleet.models import ServiceRecord, TimelineEvent, Vehicle
from fleet.services import book_service, complete_service, ensure_due_record, start_service

SEED_PREFIX = "FC-DEMO-"

# Cycled by vehicle index, not chosen per-vehicle by hand -- keeps this
# file short while still giving every vehicle a plausible, varied make/
# model/interval/odometer rather than 30 identical rows.
VEHICLE_TEMPLATES = [
    ("Ford", "Transit"),
    ("Mercedes-Benz", "Sprinter"),
    ("Iveco", "Daily"),
    ("Renault", "Master"),
    ("Volkswagen", "Crafter"),
    ("MAN", "TGL"),
    ("DAF", "LF"),
    ("Isuzu", "NPR"),
    ("Volvo", "FL"),
    ("Peugeot", "Boxer"),
]
INTERVAL_DAYS_CYCLE = [60, 90, 120, 150, 180]
INTERVAL_KM_CYCLE = [6_000, 8_000, 10_000, 12_000, 15_000]
BASE_ODOMETER_CYCLE = [1_500, 18_000, 42_000, 76_000, 118_000, 162_000]

TECHNICIANS = [
    ("t.alvarez@fleetcare.demo", "Marisol", "Alvarez"),
    ("t.chen@fleetcare.demo", "Wei", "Chen"),
    ("t.diallo@fleetcare.demo", "Ibrahim", "Diallo"),
    ("t.fitzgerald@fleetcare.demo", "Aoife", "Fitzgerald"),
    ("t.nakamura@fleetcare.demo", "Haruto", "Nakamura"),
]
TECHNICIAN_DEMO_PASSWORD = "demo-tech-pass1"

TOTAL_VEHICLES = 30
NEVER_SERVICED_COUNT = 3
ARCHIVED_COUNT = 4
# Every vehicle from here on gets one backdated historical completion,
# then is steered to a specific CURRENT state -- index ranges below are in
# the same 0-based space as _create_vehicle's `index`, not re-based per
# bucket, so they read directly against TOTAL_VEHICLES.
DUE_INDICES = set(range(12, 16))
OVERDUE_INDICES = set(range(16, 20))
BOOKED_INDICES = set(range(20, 23))
IN_SERVICE_INDICES = set(range(23, 26))
# Everything else from NEVER_SERVICED_COUNT + ARCHIVED_COUNT onward that
# isn't in one of the four sets above is left at OK.


class Command(BaseCommand):
    help = "Seed (or re-seed) a demonstration fleet. Clears its own previous data first; never touches seed_users accounts."

    def handle(self, *args, **options):
        manager = User.objects.filter(email="manager@fleetcare.demo").first()
        if manager is None:
            raise CommandError(
                "manager@fleetcare.demo not found -- run `python manage.py seed_users` first."
            )
        technicians = [self._get_or_create_technician(*spec) for spec in TECHNICIANS]

        with transaction.atomic():
            self._clear_previous()

            vehicles = [self._create_vehicle(i) for i in range(TOTAL_VEHICLES)]
            never_serviced = vehicles[:NEVER_SERVICED_COUNT]
            archived = vehicles[NEVER_SERVICED_COUNT:NEVER_SERVICED_COUNT + ARCHIVED_COUNT]
            active = vehicles[NEVER_SERVICED_COUNT + ARCHIVED_COUNT:]

            # One backdated completion per history-bearing vehicle, spread
            # across the last 8 weeks -- archived vehicles get history too
            # (goal 2: archiving preserves it), never-serviced ones get none.
            real_now = timezone.now()
            history_vehicles = archived + active
            for local_index, vehicle in enumerate(history_vehicles):
                technician = technicians[local_index % len(technicians)]
                self._seed_historical_completion(vehicle, manager, technician, local_index, real_now)

            for vehicle in archived:
                vehicle.is_archived = True
                vehicle.save(update_fields=["is_archived", "updated_at"])

            for index, vehicle in enumerate(active, start=NEVER_SERVICED_COUNT + ARCHIVED_COUNT):
                technician = technicians[index % len(technicians)]
                self._apply_current_state(vehicle, manager, technician, index)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(vehicles)} vehicles ({len(never_serviced)} never serviced, "
            f"{len(archived)} archived, {len(active)} active) and {len(technicians)} technicians."
        ))

    # -- setup / teardown --

    def _get_or_create_technician(self, email, first_name, last_name):
        technician, created = User.objects.get_or_create(
            email=email,
            defaults={"role": User.Role.TECHNICIAN, "first_name": first_name, "last_name": last_name},
        )
        if created:
            technician.set_password(TECHNICIAN_DEMO_PASSWORD)
            technician.save(update_fields=["password"])
        return technician

    def _clear_previous(self):
        old_vehicles = Vehicle.all_objects.filter(registration_number__startswith=SEED_PREFIX)
        # ServiceRecord.vehicle is on_delete=PROTECT, so records have to go
        # before vehicles; deleting them cascades to their own
        # TimelineEvent/ServiceAssignment/AlertDismissal rows.
        deleted_records, _ = ServiceRecord.objects.filter(vehicle__in=old_vehicles).delete()
        deleted_vehicles, _ = old_vehicles.delete()
        if deleted_vehicles:
            self.stdout.write(
                f"Cleared {deleted_vehicles} previously seeded vehicle(s) and {deleted_records} record(s)."
            )

    # -- vehicle creation --

    def _create_vehicle(self, index):
        make, model = VEHICLE_TEMPLATES[index % len(VEHICLE_TEMPLATES)]
        return Vehicle.objects.create(
            registration_number=f"{SEED_PREFIX}{index + 1:02d}",
            make=make,
            model=model,
            current_odometer=BASE_ODOMETER_CYCLE[index % len(BASE_ODOMETER_CYCLE)],
            service_interval_days=INTERVAL_DAYS_CYCLE[index % len(INTERVAL_DAYS_CYCLE)],
            service_interval_km=INTERVAL_KM_CYCLE[index % len(INTERVAL_KM_CYCLE)],
        )

    # -- history --

    def _seed_historical_completion(self, vehicle, manager, technician, local_index, real_now):
        """One full DUE -> BOOKED -> IN_SERVICE -> COMPLETED cycle, backdated
        somewhere in the last 8 weeks, driven through the real service-layer
        functions with timezone.now patched for the duration -- so
        due_since/scheduled_date/completed_at AND every TimelineEvent's
        auto_now_add created_at land on the same backdated instant, not a
        completed record with a "created today" timeline.
        """
        weeks_ago = local_index % 8
        completed_at = real_now - timedelta(
            weeks=weeks_ago, days=(local_index * 3) % 7, hours=(local_index * 5) % 24
        )
        with patch("django.utils.timezone.now", return_value=completed_at):
            record = ServiceRecord.objects.create(
                vehicle=vehicle,
                description="Scheduled maintenance",
                status=ServiceRecord.Status.DUE,
                due_since=timezone.now(),
                created_by=manager,
            )
            TimelineEvent.objects.create(
                service_record=record,
                event_type=TimelineEvent.EventType.CREATED,
                actor=manager,
                new_value=ServiceRecord.Status.DUE,
            )
            book_service(record, scheduled_date=timezone.localdate(), technician=technician, actor=manager)
            start_service(record, actor=manager)
            complete_service(record, completed_odometer=vehicle.current_odometer + 400, actor=manager)

    # -- current state --

    def _apply_current_state(self, vehicle, manager, technician, index):
        """Steers an already-completed-once vehicle to a specific CURRENT
        service_status, by forcing its date threshold into the past and
        re-deriving due-ness through ensure_due_record() -- the same
        function an odometer edit or check_due_vehicles would call, not a
        re-implementation of the due/overdue rules.
        """
        if index not in DUE_INDICES | OVERDUE_INDICES | BOOKED_INDICES | IN_SERVICE_INDICES:
            return  # left OK: the historical completion's own next_due fields stand.

        vehicle.refresh_from_db()
        vehicle.next_due_date = timezone.localdate() - timedelta(days=1)
        vehicle.save(update_fields=["next_due_date"])
        record = ensure_due_record(vehicle)
        if record is None:
            return

        if index in OVERDUE_INDICES:
            record.due_since = timezone.now() - timedelta(days=settings.SERVICE_GRACE_PERIOD_DAYS + 3)
            record.save(update_fields=["due_since"])
        elif index in BOOKED_INDICES:
            book_service(
                record, scheduled_date=timezone.localdate() + timedelta(days=7),
                technician=technician, actor=manager,
            )
        elif index in IN_SERVICE_INDICES:
            book_service(record, scheduled_date=timezone.localdate(), technician=technician, actor=manager)
            start_service(record, actor=manager)
        # DUE_INDICES: ensure_due_record() above already leaves it there.
