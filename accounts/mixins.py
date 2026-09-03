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


class ManagerOrAssignedTechnicianMixin(LoginRequiredMixin):
    """Object-level companion to TechnicianScopedQuerysetMixin, for
    DetailView/UpdateView rather than ListView.

    TechnicianScopedQuerysetMixin filters get_queryset() -- right for a
    list (a technician just sees fewer rows), wrong here:
    DetailView/UpdateView build get_object() from
    get_queryset().get(pk=...), so a filtered-out row would surface as a
    plain Http404, not a permission error. Goal 3 wants a technician who
    tries a record they're not assigned to to be refused with a real 403,
    not told the record doesn't exist. So this fetches the object from the
    FULL queryset (existence is checked normally) and then explicitly
    denies access on it, instead of filtering it out beforehand.

    Same `technicians` field-name convention as TechnicianScopedQuerysetMixin
    -- assumes the object being fetched exposes a `technicians` M2M to the
    user model.
    """

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        user = self.request.user
        if user.is_fleet_manager:
            return obj
        if user.is_technician and obj.technicians.filter(pk=user.pk).exists():
            return obj
        raise PermissionDenied


def scope_vehicles_to_technician(queryset, user):
    """The actual filter behind VehicleTechnicianScopedQuerysetMixin below,
    pulled out to a plain function so a non-CBV call site (a filter-dropdown
    queryset, say) can reuse the exact same scoping rule instead of
    re-deriving it inline -- see fleet.views.ServiceRecordListView, which
    does exactly that for its "Vehicle" filter.

    A technician's claim on a vehicle is inherited from every ServiceRecord
    ever raised against it, in ANY status -- including COMPLETED, since past
    work is still their work -- so this traverses a reverse FK plus an M2M
    (`service_records__technicians`), and a vehicle with several matching
    records would otherwise repeat once per record without `.distinct()`.
    Still a plain field-name-string filter, so accounts stays free of a hard
    dependency on fleet.models.
    """
    if user.is_technician:
        return queryset.filter(service_records__technicians=user).distinct()
    return queryset


class VehicleTechnicianScopedQuerysetMixin:
    """Vehicle's sibling to TechnicianScopedQuerysetMixin above -- kept
    separate rather than folded in with a branch, because the two don't
    share a filter shape.

    TechnicianScopedQuerysetMixin filters a queryset whose OWN model has a
    direct `technicians` M2M field (ServiceRecord): `.filter(technicians=
    user)`, one hop, no row fan-out (ServiceAssignment's (service_record,
    technician) uniqueness means at most one matching through-row per
    record). Vehicle has no such field, hence the different shape --
    see scope_vehicles_to_technician() above for why.
    """

    def get_queryset(self):
        return scope_vehicles_to_technician(super().get_queryset(), self.request.user)


class VehicleManagerOrAssignedTechnicianMixin(LoginRequiredMixin):
    """Object-level companion to VehicleTechnicianScopedQuerysetMixin, for
    VehicleDetailView -- same reasoning as ManagerOrAssignedTechnicianMixin:
    a queryset pre-filtered to "vehicles this technician can see" would
    turn an unauthorized vehicle into a plain Http404 via get_object(), not
    the 403 the brief calls for. Fetches from the view's own (unfiltered)
    queryset and denies access explicitly afterward instead.
    """

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        user = self.request.user
        if user.is_fleet_manager:
            return obj
        if user.is_technician and obj.service_records.filter(technicians=user).exists():
            return obj
        raise PermissionDenied
