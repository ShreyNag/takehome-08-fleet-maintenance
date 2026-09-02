from django.test import TestCase
from django.urls import reverse

from .models import User


def make_user(email, role):
    return User.objects.create_user(email=email, password="irrelevant", role=role)


class LoginRedirectTests(TestCase):
    """Goal 5: a technician's landing page after login is their
    cross-vehicle record list; a manager's is unchanged (the dashboard)."""

    def setUp(self):
        self.manager = make_user("login-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("login-tech@example.com", User.Role.TECHNICIAN)

    def test_technician_redirected_to_their_record_list(self):
        response = self.client.post(
            reverse("login"), {"username": "login-tech@example.com", "password": "irrelevant"}
        )
        self.assertRedirects(response, reverse("service-record-list"))

    def test_manager_redirected_to_dashboard(self):
        response = self.client.post(
            reverse("login"), {"username": "login-mgr@example.com", "password": "irrelevant"}
        )
        self.assertRedirects(response, reverse("dashboard"))

    def test_next_param_still_takes_priority_over_role(self):
        response = self.client.post(
            reverse("login") + "?next=" + reverse("vehicle-list"),
            {"username": "login-tech@example.com", "password": "irrelevant"},
        )
        self.assertRedirects(response, reverse("vehicle-list"))
