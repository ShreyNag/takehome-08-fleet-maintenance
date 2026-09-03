from django.contrib.auth import views as auth_views
from django.urls import reverse


class FleetLoginView(auth_views.LoginView):
    """Goal 5: a technician's landing page after login is their
    cross-vehicle record list, not the manager dashboard. A plain
    LOGIN_REDIRECT_URL can't express that -- it's a single fixed name, and
    which page is "the" landing page depends on request.user.role, which
    doesn't exist until after authentication succeeds. Overriding
    get_default_redirect_url() (rather than get_success_url(), which also
    has to account for a `next` param) keeps that one param's behaviour
    exactly as LoginView already implements it -- this only changes what
    "default" means.
    """

    def get_default_redirect_url(self):
        if self.request.user.is_technician:
            return reverse("service-record-list")
        return super().get_default_redirect_url()
