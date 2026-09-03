"""Goal 10's overdue-alerts surface -- shared by the nav badge context
processor, the alerts list view, and (indirectly, since dismissal just
writes a row against a record this function would otherwise return) the
dismiss view. One function so all three can never disagree about which
records are "currently an alert".
"""

from django.db.models import Exists, OuterRef

from .models import AlertDismissal, ServiceRecord


def overdue_alerts():
    """Every overdue ServiceRecord (ServiceRecord.objects.overdue() --
    session 4's due_since + grace period formula, not reimplemented here)
    that nobody has dismissed yet.

    Dismissal is global per record, not per viewer: AlertDismissal is keyed
    to (service_record, dismissed_by) only to stop one manager's double
    submit from raising IntegrityError, not to scope suppression to that
    manager -- ANY dismissal row against a record drops it from this list
    for everyone. That's what makes the goal 10 reappearance rule need no
    extra logic: a new due threshold creates a brand new ServiceRecord that
    no dismissal references, overdue or not, so it's untouched by this
    Exists() regardless of who dismissed the old one.

    Exists(), not a join or a values_list of dismissed ids -- same reason
    with_service_status() uses it in models.py: stays one query regardless
    of how many dismissals exist, and composes cleanly with .overdue()
    rather than duplicating its filter.
    """
    dismissed = AlertDismissal.objects.filter(service_record=OuterRef("pk"))
    return (
        ServiceRecord.objects.overdue()
        .annotate(_dismissed=Exists(dismissed))
        .filter(_dismissed=False)
        .select_related("vehicle")
        .order_by("due_since")
    )
