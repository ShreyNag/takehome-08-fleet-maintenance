"""Service-lifecycle business logic.

Views collect the request, call one of the functions below, catch
ServiceLifecycleError, and render a message. Deliberately no transition
logic in views, no transition logic in models (the model layer is limited
to field-level validation -- see ServiceRecord.clean() and its
CheckConstraint), and no Django signals for the timeline: every write here
happens inside an explicit transaction.atomic() block, in this one file, so
there is exactly one place the rules live.
"""

from .models import ServiceRecord

# Explicit per-state allow-list rather than a generic "what's the next
# status" check. COMPLETED maps to an empty set, which is what makes it
# terminal -- there's no special-case "is this terminal" branch anywhere
# else, it falls out of the table.
ALLOWED_TRANSITIONS = {
    ServiceRecord.Status.DUE: {ServiceRecord.Status.BOOKED},
    ServiceRecord.Status.BOOKED: {ServiceRecord.Status.IN_SERVICE},
    ServiceRecord.Status.IN_SERVICE: {ServiceRecord.Status.COMPLETED},
    ServiceRecord.Status.COMPLETED: set(),
}

_STATUS_LABELS = dict(ServiceRecord.Status.choices)


class ServiceLifecycleError(Exception):
    """Base class for every error a view must catch and turn into a
    message, never a 500. Two concrete kinds below: a move that is illegal
    from the record's current status, and a move that IS legal but was
    called with missing/invalid arguments.
    """


class InvalidTransition(ServiceLifecycleError):
    """The attempted status is not reachable from the record's current
    status. Carries current status, attempted status, and what IS legal
    from here, built straight into the message -- goal 4 requires the
    server reject illegal moves "with a message explaining why", which a
    bare 400 doesn't satisfy, so this message is written to be shown to a
    user directly rather than logged for a developer.
    """

    def __init__(self, record, attempted):
        self.current = record.status
        self.attempted = attempted
        allowed = ALLOWED_TRANSITIONS.get(record.status, set())
        allowed_desc = (
            ", ".join(_STATUS_LABELS[status] for status in sorted(allowed))
            if allowed
            else "nothing -- this is a terminal state"
        )
        message = (
            f'Cannot move this service record from "{_STATUS_LABELS[record.status]}" '
            f'to "{_STATUS_LABELS[attempted]}". Allowed from '
            f'"{_STATUS_LABELS[record.status]}": {allowed_desc}.'
        )
        super().__init__(message)


class InvalidTransitionInput(ServiceLifecycleError):
    """The move is legal from this status, but required arguments are
    missing or invalid (no technician, no scheduled date, a completion
    odometer lower than the vehicle's current reading). Kept distinct from
    InvalidTransition because the fix is different: filling in the form,
    not "you can't do this from here at all".
    """


def _check_transition(record, target):
    if target not in ALLOWED_TRANSITIONS.get(record.status, set()):
        raise InvalidTransition(record, target)
