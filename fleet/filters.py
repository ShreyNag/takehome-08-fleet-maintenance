"""Server-side scoping, search, filtering and sorting for the cross-vehicle
service record list (goal 6) -- and shared as-is with the CSV export (goal
7), so the export of "the current filters" can never drift from what the
list view actually applied. Read-side query building only; nothing here
mutates a record, which is why it isn't in services.py alongside the
transition/assignment functions.
"""

from .models import ServiceRecord

# The only fields a `sort` query parameter is allowed to select. Passing an
# arbitrary column name straight to order_by() is an injection surface (it
# can reach across relations, e.g. "vehicle__owner__password"), so this is
# an allowlist, not a denylist.
SORT_FIELDS = {
    "scheduled_date": "scheduled_date",
    "status": "status",
    "updated_at": "updated_at",
}
DEFAULT_SORT = "scheduled_date"


def scoped_service_records(user):
    """Managers see every record; technicians see only ones they hold an
    assignment against -- same `technicians` M2M convention
    accounts.mixins.TechnicianScopedQuerysetMixin already uses, spelled out
    again here because this also has to serve the CSV export view, which
    isn't a ListView and so can't reuse that mixin directly.

    select_related("vehicle") and prefetch_related("technicians") up front:
    every consumer of this function renders both, so N+1 prevention belongs
    here once rather than being re-added by each caller.
    """
    queryset = ServiceRecord.objects.select_related("vehicle").prefetch_related("technicians")
    if user.is_technician:
        queryset = queryset.filter(technicians=user)
    return queryset


def apply_filters(queryset, params):
    """params is any dict-like of query-string values (typically
    request.GET) -- a blank/missing value for any filter means "no
    filter", not "match blank"."""
    query = params.get("q", "").strip()
    if query:
        # icontains is fine at this scale. If the service_records table
        # ever gets large enough for this to matter, the fix is Postgres
        # full-text search with a GIN index on description -- not worth
        # the setup cost here.
        queryset = queryset.filter(description__icontains=query)

    vehicle_id = params.get("vehicle")
    if vehicle_id:
        queryset = queryset.filter(vehicle_id=vehicle_id)

    status = params.get("status")
    if status in ServiceRecord.Status.values:
        queryset = queryset.filter(status=status)

    technician_id = params.get("technician")
    if technician_id:
        queryset = queryset.filter(technicians__id=technician_id)

    return queryset


def resolve_sort(params):
    """Returns (order_by_arg, sort_was_valid). An unrecognised `sort` value
    falls back to DEFAULT_SORT rather than being trusted -- sort_was_valid
    tells the caller whether that fallback happened, so the view can
    surface it as a message instead of silently substituting a different
    order (goal 6's correction: silent fallback is inconsistent with how
    the rest of this app rejects-with-a-reason).
    """
    field = params.get("sort", DEFAULT_SORT)
    sort_was_valid = field in SORT_FIELDS
    if not sort_was_valid:
        field = DEFAULT_SORT
    direction = "-" if params.get("dir") == "desc" else ""
    return f"{direction}{SORT_FIELDS[field]}", sort_was_valid


def filtered_service_records(user, params):
    """The one function the list view and the CSV export both call --
    identical scoping, filters and sort, so an export taken from a
    filtered page can never show different rows than the page itself.

    "pk" as a secondary order_by: makes pagination stable when the primary
    sort field has ties (e.g. many DUE records with no scheduled_date yet),
    which page-3-keeps-its-order requires; it's appended here, not user
    controllable, so it's not part of the allowlist.
    """
    queryset = scoped_service_records(user)
    queryset = apply_filters(queryset, params)
    ordering, sort_was_valid = resolve_sort(params)
    queryset = queryset.order_by(ordering, "pk")
    return queryset, sort_was_valid
