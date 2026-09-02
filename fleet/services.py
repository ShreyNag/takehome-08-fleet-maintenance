"""Service-lifecycle business logic.

Views collect the request, call one of the functions below, catch
ServiceLifecycleError, and render a message. Deliberately no transition
logic in views, no transition logic in models (the model layer is limited
to field-level validation -- see ServiceRecord.clean() and its
CheckConstraint), and no Django signals for the timeline: every write here
happens inside an explicit transaction.atomic() block, in this one file, so
there is exactly one place the rules live.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ServiceAssignment, ServiceRecord, TimelineEvent

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


def _record_status_change(record, actor, old_status):
    TimelineEvent.objects.create(
        service_record=record,
        event_type=TimelineEvent.EventType.STATUS_CHANGED,
        actor=actor,
        old_value=old_status,
        new_value=record.status,
    )


# Every transition function below follows the same shape: validate
# everything (legality of the move, then required arguments) BEFORE
# mutating anything. That ordering isn't just tidy -- it's what guarantees
# ServiceLifecycleError is only ever raised pre-mutation, so a caller that
# catches it (the view layer) never has to worry about the record object
# being left half-updated in memory even though the DB write rolled back.


def assign_technician(record, technician, actor):
    """Goal 5: add `technician` to `record`, at booking or at any point
    after. Not a state transition (no status check, no ALLOWED_TRANSITIONS
    involvement) -- any number of technicians can be on a record in any
    status.

    Idempotent by design: assigning someone already on the record must be a
    no-op, not an IntegrityError off the (service_record, technician)
    unique constraint and not a second timeline event for something that
    didn't actually change. get_or_create + the `created` flag is what
    makes both of those true in one round trip rather than a separate
    existence check.

    Permission (manager-only, even for a technician already assigned) is
    the view layer's job, same convention as every other function here --
    this function trusts its caller.
    """
    with transaction.atomic():
        assignment, created = ServiceAssignment.objects.get_or_create(
            service_record=record, technician=technician, defaults={"assigned_by": actor}
        )
        if created:
            TimelineEvent.objects.create(
                service_record=record,
                event_type=TimelineEvent.EventType.TECHNICIAN_ASSIGNED,
                actor=actor,
                new_value=str(technician),
            )
    return assignment, created


def unassign_technician(record, technician, actor):
    """Goal 5's inverse. Symmetric no-op if `technician` isn't currently on
    the record -- same reasoning as assign_technician: removing someone who
    isn't there didn't change anything, so no event.
    """
    with transaction.atomic():
        deleted, _ = ServiceAssignment.objects.filter(
            service_record=record, technician=technician
        ).delete()
        if deleted:
            TimelineEvent.objects.create(
                service_record=record,
                event_type=TimelineEvent.EventType.TECHNICIAN_UNASSIGNED,
                actor=actor,
                old_value=str(technician),
            )
    return bool(deleted)


def book_service(record, scheduled_date, technician, actor):
    """DUE -> BOOKED.

    Goal 4: "booking assigns a scheduled date and a technician" -- both are
    hard preconditions, not optional metadata filled in later, so a missing
    one rejects the whole transition rather than booking with a gap.
    """
    _check_transition(record, ServiceRecord.Status.BOOKED)
    if not scheduled_date or not technician:
        raise InvalidTransitionInput(
            "Booking requires both a scheduled date and a technician."
        )
    with transaction.atomic():
        old_status = record.status
        record.status = ServiceRecord.Status.BOOKED
        record.scheduled_date = scheduled_date
        record.save(update_fields=["status", "scheduled_date", "updated_at"])
        # One code path for assignment (goal 5): booking a technician who's
        # already on the record (assigned ahead of booking, e.g. via the
        # goal-5 assignment UI) is just the no-op assign_technician already
        # handles -- no separate get_or_create here. This does mean a
        # booking call now writes TWO timeline events (TECHNICIAN_ASSIGNED
        # + STATUS_CHANGED) rather than one: goal 9 wants "every technician
        # assignment" on the timeline, and a booking that silently omitted
        # its own assignment event would leave a reviewer unable to see when
        # the technician actually came onto the record from the timeline
        # alone.
        assign_technician(record, technician, actor)
        _record_status_change(record, actor, old_status)
    return record


def start_service(record, actor):
    """BOOKED -> IN_SERVICE. No extra data required for this one."""
    _check_transition(record, ServiceRecord.Status.IN_SERVICE)
    with transaction.atomic():
        old_status = record.status
        record.status = ServiceRecord.Status.IN_SERVICE
        record.save(update_fields=["status", "updated_at"])
        _record_status_change(record, actor, old_status)
    return record


def complete_service(record, completed_odometer, actor):
    """IN_SERVICE -> COMPLETED.

    Resets both due-counters on the vehicle from THIS completion's date and
    odometer -- not from "today" and not from vehicle.current_odometer --
    so a completion recorded a day after the actual work still schedules
    the next service the correct interval-length after the work, not after
    whenever someone got around to logging it.
    """
    _check_transition(record, ServiceRecord.Status.COMPLETED)
    vehicle = record.vehicle
    if completed_odometer < vehicle.current_odometer:
        raise InvalidTransitionInput(
            f"Completion odometer ({completed_odometer} km) is lower than the "
            f"vehicle's current odometer ({vehicle.current_odometer} km) -- "
            "a vehicle cannot have driven backwards."
        )
    completed_at = timezone.now()
    with transaction.atomic():
        old_status = record.status
        record.status = ServiceRecord.Status.COMPLETED
        record.completed_at = completed_at
        record.completed_odometer = completed_odometer
        record.save(
            update_fields=["status", "completed_at", "completed_odometer", "updated_at"]
        )

        vehicle.next_due_date = completed_at.date() + timedelta(days=vehicle.service_interval_days)
        vehicle.next_due_odometer = completed_odometer + vehicle.service_interval_km
        if completed_odometer > vehicle.current_odometer:
            vehicle.current_odometer = completed_odometer
        vehicle.save(
            update_fields=["next_due_date", "next_due_odometer", "current_odometer", "updated_at"]
        )

        _record_status_change(record, actor, old_status)

        # Re-derive due-ness for the same vehicle inside this same
        # transaction: if the freshly-computed thresholds are already met
        # (a very short interval, or the vehicle was already past its old
        # due point by the time this completion got logged), the next DUE
        # record should exist the instant this one closes, not wait for a
        # future odometer edit or the next check_due_vehicles run.
        ensure_due_record(vehicle)
    return record


def ensure_due_record(vehicle):
    """Create a new DUE ServiceRecord for `vehicle` if it has crossed
    either due threshold and doesn't already have one open. Returns the
    created record, or None.

    Called from exactly two places: complete_service above, and
    VehicleUpdateView after an odometer edit -- both are moments the
    vehicle's due-ness could have just changed. Everything else (the
    calendar alone crossing next_due_date with nobody touching the
    vehicle) is covered by the check_due_vehicles management command
    instead, since there's no third code path that runs on a timer.
    """
    if vehicle.is_archived:
        return None

    has_open_record = ServiceRecord.objects.filter(
        vehicle=vehicle,
        status__in=[
            ServiceRecord.Status.DUE,
            ServiceRecord.Status.BOOKED,
            ServiceRecord.Status.IN_SERVICE,
        ],
    ).exists()
    if has_open_record:
        return None

    if vehicle.next_due_date is None and vehicle.next_due_odometer is None:
        # Never serviced -- no baseline to judge due-ness against yet.
        # Treated as not-yet-due rather than immediately-due, so a
        # brand-new vehicle isn't flagged on day one. Flagged to the brief
        # author as a judgment call; see docs/decisions.md.
        return None

    today = timezone.localdate()
    date_triggered = vehicle.next_due_date is not None and vehicle.next_due_date <= today
    odometer_triggered = (
        vehicle.next_due_odometer is not None
        and vehicle.current_odometer >= vehicle.next_due_odometer
    )
    if not (date_triggered or odometer_triggered):
        return None

    # Whichever threshold fired first is named in the description so a
    # manager looking at the record doesn't have to cross-reference the
    # vehicle to see why it exists.
    if date_triggered:
        reason = f"scheduled service date ({vehicle.next_due_date.isoformat()}) has passed"
    else:
        reason = (
            f"odometer ({vehicle.current_odometer} km) reached the "
            f"{vehicle.next_due_odometer} km service point"
        )

    with transaction.atomic():
        record = ServiceRecord.objects.create(
            vehicle=vehicle,
            description=f"Automatically flagged due -- {reason}.",
            status=ServiceRecord.Status.DUE,
            due_since=timezone.now(),
            created_by=None,
        )
        TimelineEvent.objects.create(
            service_record=record,
            event_type=TimelineEvent.EventType.CREATED,
            actor=None,
            new_value=ServiceRecord.Status.DUE,
        )
    return record
