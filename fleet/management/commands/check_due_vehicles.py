from django.core.management.base import BaseCommand

from fleet.services import sweep_due_vehicles


class Command(BaseCommand):
    """Sweep every active vehicle and create a DUE ServiceRecord for any
    that have crossed a threshold.

    ensure_due_record() is otherwise only triggered by an odometer edit or
    a service completion, but the date threshold can be crossed by the
    calendar alone with nobody touching the vehicle at all. Render's free
    tier has no scheduled-job/cron feature (confirmed session 6: Render's
    Cron Jobs have no free tier, billed per-minute from a $1/mo minimum),
    so this same sweep is also exposed as a protected endpoint
    (fleet.views.CheckDueVehiclesView) for an external free scheduler to
    hit instead of running this command on a timer. This command remains
    for a manual run (a paid Render Shell, or locally against DATABASE_URL
    pointed at the deployed database).
    """

    help = "Create DUE service records for vehicles that have crossed a due threshold."

    def handle(self, *args, **options):
        created = sweep_due_vehicles()
        for record in created:
            self.stdout.write(f"Created DUE record for {record.vehicle.registration_number}")
        self.stdout.write(self.style.SUCCESS(f"Done. {len(created)} record(s) created."))
