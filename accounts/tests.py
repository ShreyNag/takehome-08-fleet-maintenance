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


class HomeRedirectTests(TestCase):
    """Regression coverage: '/' used to be a bare
    RedirectView(pattern_name='dashboard') that never checked role, so a
    technician with an existing session landed on a 403 the moment goal 8
    made the dashboard manager-only. '/' must route the same way the
    post-login redirect does (LoginRedirectTests above), not disagree with
    it -- and the dashboard's own manager-only rule must be untouched:
    requesting it directly is still a 403 for a technician."""

    def setUp(self):
        self.manager = make_user("home-mgr@example.com", User.Role.FLEET_MANAGER)
        self.technician = make_user("home-tech@example.com", User.Role.TECHNICIAN)

    def test_technician_lands_on_their_record_list(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("service-record-list"))

    def test_manager_lands_on_the_dashboard(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("dashboard"))

    def test_anonymous_visitor_is_sent_to_login(self):
        response = self.client.get(reverse("home"))
        self.assertRedirects(response, reverse("login"))

    def test_technician_still_gets_403_requesting_the_dashboard_directly(self):
        self.client.force_login(self.technician)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 403)
