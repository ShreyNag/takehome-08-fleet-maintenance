from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from fleetcare.settings import env

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
    help = (
        "Create demo fleet manager and technician accounts for testing login, "
        "plus an optional Django admin account from DJANGO_ADMIN_EMAIL/"
        "DJANGO_ADMIN_PASSWORD."
    )

    def handle(self, *args, **options):
        created = []
        existing = []

        for spec in DEMO_USERS:
            spec = spec.copy()
            email = spec.pop("email")
            password = spec.pop("password")
            # get_or_create so redeploys don't crash on a duplicate email.
            user, was_created = User.objects.get_or_create(email=email, defaults=spec)
            if was_created:
                user.set_password(password)
                user.save(update_fields=["password"])
                created.append(email)
                self.stdout.write(self.style.SUCCESS(f"Created {email} / {password}"))
            else:
                existing.append(email)
                self.stdout.write(f"Already exists: {email}")

        self._seed_admin(created, existing)

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Created: {', '.join(created) if created else 'none'}")
        )
        self.stdout.write(f"Already present: {', '.join(existing) if existing else 'none'}")

    def _seed_admin(self, created, existing):
        # Deliberately NOT one of the demo accounts, and deliberately not a
        # promotion of one: a fleet manager who is also a Django superuser
        # would make it impossible to honestly demonstrate that role
        # enforcement (accounts/mixins.py) is doing anything. This is a
        # separate, operator-controlled account that only exists to view
        # /admin/, sourced from env vars so it's never in git history.
        admin_email = env("DJANGO_ADMIN_EMAIL", default="")
        admin_password = env("DJANGO_ADMIN_PASSWORD", default="")

        if not admin_email or not admin_password:
            self.stdout.write(
                "DJANGO_ADMIN_EMAIL/DJANGO_ADMIN_PASSWORD not set — skipping admin account."
            )
            return

        # defaults only apply on creation, so an existing row (including one
        # from a stale/mistaken email match) is never modified here — role,
        # is_staff and is_superuser are only ever set the first time.
        user, was_created = User.objects.get_or_create(
            email=admin_email,
            defaults={
                "role": User.Role.FLEET_MANAGER,
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if was_created:
            # Password set only on creation, same as the demo accounts —
            # a redeploy must never silently reset a password you changed.
            user.set_password(admin_password)
            user.save(update_fields=["password"])
            created.append(admin_email)
            self.stdout.write(self.style.SUCCESS(f"Created admin account: {admin_email}"))
        else:
            existing.append(admin_email)
            self.stdout.write(f"Already exists: {admin_email} (password left unchanged)")
