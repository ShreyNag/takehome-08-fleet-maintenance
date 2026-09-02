from django.core.management.base import BaseCommand

from fleet.models import Vehicle
from fleet.services import ensure_due_record


class Command(BaseCommand):
    """Sweep every active vehicle and create a DUE ServiceRecord for any
    that have crossed a threshold.

    ensure_due_record() is otherwise only triggered by an odometer edit or
    a service completion, but the date threshold can be crossed by the
    calendar alone with nobody touching the vehicle at all. Render's free
    tier has no scheduled-job/cron feature, so for now this has to be
    triggered by hand (e.g. from the Render shell, on a paid plan, or by an
    external free scheduler hitting a management endpoint if one gets
    added later) rather than running automatically on a timer.
    """

    help = "Create DUE service records for vehicles that have crossed a due threshold."

    def handle(self, *args, **options):
        created = 0
        for vehicle in Vehicle.objects.all():
            record = ensure_due_record(vehicle)
            if record is not None:
                created += 1
                self.stdout.write(f"Created DUE record for {vehicle.registration_number}")
        self.stdout.write(self.style.SUCCESS(f"Done. {created} record(s) created."))
