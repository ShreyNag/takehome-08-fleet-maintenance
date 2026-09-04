"""Goal 8's fleet-wide dashboard.

Everything here is a SQL aggregate -- no Python loop over a vehicle or
service-record queryset anywhere below. dashboard_context() runs exactly
five queries regardless of fleet size (asserted in
DashboardViewTests.test_query_count_is_fixed):

  1. Vehicle.objects.with_service_status().count() filtered to OVERDUE --
     reuses the session-4 annotation (and, through it, ServiceRecord.
     objects.overdue()) rather than recomputing the grace-period comparison
     here.
  2. ServiceRecord counts grouped by status, restricted to {DUE,
     IN_SERVICE} and counting DISTINCT vehicles -- the "due" and "in
     service" headline numbers together, from one query.

     This is deliberately NOT read off with_service_status(): that
     annotation only distinguishes OVERDUE from "any other open record"
     (see models.py's Case/When), so BOOKED and IN_SERVICE both fall under
     its DUE label. Reusing it for "vehicles due" would silently double-
     count a vehicle that's actively IN_SERVICE as also "due", which isn't
     what the two separate headline numbers are supposed to mean. This
     query reads ServiceRecord.status directly instead, so the two numbers
     partition cleanly (a vehicle is in exactly one of DUE/IN_SERVICE at a
     time, per the one-open-record-per-vehicle invariant the state machine
     already maintains).
  3. ServiceRecord counts grouped by status, unfiltered (the status-
     breakdown widget -- all four statuses, all-time).
  4. User counts grouped by assigned-record count (the technician-
     breakdown widget).
  5. ServiceRecord counts grouped by TruncWeek(completed_at) over the last
     eight weeks (the chart). This week's own bucket doubles as the
     "completed this week" headline number, so that doesn't cost a sixth
     query.

The only Python-side work past that is zero-filling and pct-scaling an
8-row result for the chart -- bookkeeping on an already-aggregated result,
not a loop that does the counting itself.
"""

from datetime import datetime, time, timedelta

from django.db.models import Count
from django.db.models.functions import TruncWeek
from django.utils import timezone

from accounts.models import User
from .models import ServiceRecord, Vehicle

WEEKS_IN_CHART = 8

_STATUS_LABELS = dict(ServiceRecord.Status.choices)


def _weekly_completions():
    """Returns (weekly_series, completed_this_week). weekly_series has
    exactly WEEKS_IN_CHART entries, oldest first, one per calendar week
    (Monday-start, matching TruncWeek) -- a week with zero completions
    still gets an entry with count=0 rather than being absent, per goal 8:
    a gap in the x-axis is a bug.
    """
    today = timezone.localdate()
    current_week_start = today - timedelta(days=today.weekday())
    week_starts = [
        current_week_start - timedelta(weeks=offset)
        for offset in range(WEEKS_IN_CHART - 1, -1, -1)
    ]
    range_start = timezone.make_aware(datetime.combine(week_starts[0], time.min))

    rows = (
        ServiceRecord.objects.filter(
            status=ServiceRecord.Status.COMPLETED,
            completed_at__gte=range_start,
        )
        .annotate(week=TruncWeek("completed_at"))
        .values("week")
        .annotate(count=Count("pk"))
    )
    counts_by_week = {row["week"].date(): row["count"] for row in rows}

    counts = [counts_by_week.get(week, 0) for week in week_starts]
    max_count = max(counts) if counts else 0

    weekly_series = [
        {
            "week_start": week,
            "label": week.strftime("%b %d"),
            "count": count,
            # Height as a percentage of the tallest bar in the window --
            # the CSS-only chart goal 8 asks for (decision #11): a div per
            # week, height set inline from this number, no JS/canvas/CDN.
            "pct": round(count / max_count * 100) if max_count else 0,
        }
        for week, count in zip(week_starts, counts)
    ]
    completed_this_week = counts_by_week.get(current_week_start, 0)
    return weekly_series, completed_this_week


def dashboard_context():
    overdue_vehicles = (
        Vehicle.objects.with_service_status()
        .filter(service_status=Vehicle.ServiceStatus.OVERDUE.label)
        .count()
    )

    open_vehicle_counts = {
        row["status"]: row["vehicle_count"]
        for row in ServiceRecord.objects.filter(
            status__in=[ServiceRecord.Status.DUE, ServiceRecord.Status.IN_SERVICE]
        )
        .values("status")
        .annotate(vehicle_count=Count("vehicle", distinct=True))
    }

    status_breakdown = [
        {"status": row["status"], "label": _STATUS_LABELS[row["status"]], "count": row["count"]}
        for row in ServiceRecord.objects.values("status").annotate(count=Count("pk")).order_by("status")
    ]

    technician_breakdown = list(
        User.objects.filter(role=User.Role.TECHNICIAN)
        .annotate(record_count=Count("assigned_service_records", distinct=True))
        .order_by("-record_count", "email")
    )

    weekly_series, completed_this_week = _weekly_completions()

    return {
        "due_vehicles": open_vehicle_counts.get(ServiceRecord.Status.DUE, 0),
        "overdue_vehicles": overdue_vehicles,
        "in_service_vehicles": open_vehicle_counts.get(ServiceRecord.Status.IN_SERVICE, 0),
        "completed_this_week": completed_this_week,
        "status_breakdown": status_breakdown,
        "technician_breakdown": technician_breakdown,
        "weekly_series": weekly_series,
    }
