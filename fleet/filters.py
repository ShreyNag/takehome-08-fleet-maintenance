"""Server-side scoping for the cross-vehicle service record list (goal 6,
fuller version coming next commit) -- and shared with the CSV export (goal
7), so the two can never disagree about which records a viewer can see.
"""

from .models import ServiceRecord


def scoped_service_records(user):
    """Managers see every record; technicians see only ones they hold an
    assignment against -- same `technicians` M2M convention
    accounts.mixins.TechnicianScopedQuerysetMixin already uses, spelled out
    again here because this also has to serve the CSV export view, which
    isn't a ListView and so can't reuse that mixin directly.

    select_related("vehicle") and prefetch_related("technicians") up
    front: every consumer of this function renders both, so N+1
    prevention belongs here once rather than being re-added by each
    caller.
    """
    queryset = ServiceRecord.objects.select_related("vehicle").prefetch_related("technicians")
    if user.is_technician:
        queryset = queryset.filter(technicians=user)
    return queryset
