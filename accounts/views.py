from django.contrib.auth import views as auth_views
from django.urls import reverse
from django.views.generic import RedirectView


def default_landing_url_name(user):
    """Where a user lands "by default" -- goal 5's rule (a technician's
    landing page is their cross-vehicle record list, not the manager
    dashboard) in exactly one place, shared by FleetLoginView (the
    post-login redirect) and HomeRedirectView ('/') below.

    Session 6 broke this by duplication rather than by getting the rule
    itself wrong: '/' was a bare `RedirectView(pattern_name='dashboard')`
    that never consulted this rule at all, so once goal 8 made the
    dashboard manager-only, a technician with an existing session visiting
    '/' got redirected straight into a 403 -- a second copy of "where does
    this user belong" that fell out of sync the moment one of its two
    possible destinations changed permissions. Fixed by having both
    call-sites go through this one function instead of a second one
    reappearing later.
    """
    if user.is_authenticated and user.is_technician:
        return "service-record-list"
    return "dashboard"


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
        return reverse(default_landing_url_name(self.request.user))


class HomeRedirectView(RedirectView):
    """'/' -- routes through the same default_landing_url_name() the
    post-login redirect uses, so the two can't disagree about where a
    given user belongs. Anonymous visitors go straight to login rather
    than chaining through a second redirect (dashboard's own
    LoginRequiredMixin would eventually get them there too, but directly
    is simpler and doesn't depend on dashboard staying the manager
    landing page forever).
    """

    permanent = False

    def get_redirect_url(self, *args, **kwargs):
        user = self.request.user
        if not user.is_authenticated:
            return reverse("login")
        return reverse(default_landing_url_name(user))
