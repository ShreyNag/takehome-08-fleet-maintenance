from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()

# Demo-only credentials, deliberately committed: SUBMISSION.md points
# reviewers at this command rather than at real secrets.
DEMO_USERS = [
    {
        "email": "manager@fleetcare.demo",
        "password": "demo-manager-pass1",
        "role": User.Role.FLEET_MANAGER,
        "first_name": "Fleet",
        "last_name": "Manager",
    },
    {
        "email": "tech@fleetcare.demo",
        "password": "demo-tech-pass1",
        "role": User.Role.TECHNICIAN,
        "first_name": "Demo",
        "last_name": "Technician",
    },
]


class Command(BaseCommand):
    help = "Create demo fleet manager and technician accounts for testing login."

    def handle(self, *args, **options):
        for spec in DEMO_USERS:
            spec = spec.copy()
            email = spec.pop("email")
            password = spec.pop("password")
            # get_or_create so redeploys don't crash on a duplicate email.
            user, created = User.objects.get_or_create(email=email, defaults=spec)
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
                self.stdout.write(self.style.SUCCESS(f"Created {email} / {password}"))
            else:
                self.stdout.write(f"Already exists: {email}")
