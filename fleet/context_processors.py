"""Goal 10's nav badge -- registered in settings.TEMPLATES so every view
gets it without adding it to each one individually.

Context processors run on every request that renders a template with the
request context, including the login page and every technician page. The
count itself is wrapped in SimpleLazyObject so evaluating it (one query,
overdue_alerts().count()) only happens if a template actually reads
{{ overdue_alert_count }} -- base.html only does that inside
{% if user.is_fleet_manager %}, so a technician's or anonymous visitor's
page never pays for it.
"""

from django.conf import settings
from django.utils.functional import SimpleLazyObject

from .alerts import overdue_alerts


def alerts(request):
    def count():
        user = request.user
        if not user.is_authenticated or not user.is_fleet_manager:
            return 0
        return overdue_alerts().count()

    return {"overdue_alert_count": SimpleLazyObject(count)}


def service_grace_period(request):
    """Makes the configured overdue grace period available to any template
    that wants to explain it (fleet/alert_list.html), without hardcoding the
    number there or wiring a one-off context variable through a specific
    view -- a plain settings passthrough, same shape as `alerts` above.
    """
    return {"service_grace_period_days": settings.SERVICE_GRACE_PERIOD_DAYS}
