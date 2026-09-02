from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied


class FleetManagerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """403s non-managers server-side. Goal 1 requires this enforced on the
    server, in one place, not as scattered `if request.user.role == ...`
    checks copy-pasted into every view.

    LoginRequiredMixin must come first in the MRO: its dispatch() runs
    before UserPassesTestMixin's, so an anonymous user is redirected to
    login (the normal, expected UX) before test_func() ever sees them. Only
    an authenticated non-manager reaches handle_no_permission() below and
    gets a hard 403, per the spec ("403, not a redirect").
    """

    def test_func(self):
        return self.request.user.is_fleet_manager

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied


class TechnicianScopedQuerysetMixin:
    """Fleet managers see the full queryset; technicians see only records
    they're assigned to.

    Filters by the `technicians` field name rather than importing
    fleet.models.ServiceAssignment directly -- accounts is a lower-level
    app than fleet (fleet's models reference AUTH_USER_MODEL, not the other
    way around), so this avoids giving accounts a hard dependency on
    fleet's models. It does still assume the view's queryset is built from
    a model with a `technicians` M2M to the user (ServiceRecord), which is
    an implicit contract rather than an enforced one -- fine for a single
    consumer, would need revisiting with a second.
    """

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.is_technician:
            return queryset.filter(technicians=user)
        return queryset
