from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class TimelineImmutableError(Exception):
    """Raised when code tries to update or delete an existing TimelineEvent.

    A plain exception (not ValidationError) because this isn't bad user
    input — it's a programming error. Anything that hits this has a bug.
    """


class VehicleManager(models.Manager):
    """Excludes archived vehicles. Declared first, see Vehicle docstring."""

    def get_queryset(self):
        return super().get_queryset().filter(is_archived=False)


class Vehicle(models.Model):
    """A fleet vehicle.

    ``next_due_date`` / ``next_due_odometer`` are DELIBERATELY DENORMALISED:
    they are derived from the vehicle's last completed ServiceRecord, but
    stored on the row rather than computed on read. This is what lets "which
    vehicles are due" be a plain indexed SQL query (``.filter()`` /
    ``.order_by()`` / pagination) instead of a Python loop over every
    vehicle. Only the service-completion and odometer-update code paths
    (session 4) may write these two fields — nothing else should ever set
    them directly.
    """

    registration_number = models.CharField(max_length=32, unique=True)
    # unique=True already creates a unique index in Postgres; no separate
    # db_index=True needed.
    make = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    current_odometer = models.PositiveIntegerField()
    service_interval_days = models.PositiveIntegerField()
    service_interval_km = models.PositiveIntegerField()

    # blank=True alongside null=True: `blank` (not `null`) is what
    # full_clean()/ModelForm validation checks, so null=True alone still
    # left these "required" from a validation standpoint.
    next_due_date = models.DateField(null=True, blank=True, db_index=True)
    next_due_odometer = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    is_archived = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Django uses the FIRST declared manager as the model's default manager
    # (`_default_manager`) and base manager (`_base_manager`) unless told
    # otherwise. Those are what back FK dropdown querysets in forms/admin,
    # and what other code gets from `Vehicle.objects` by convention. Putting
    # the filtered manager first means "give me vehicles" defaults to
    # "give me vehicles you can still act on" everywhere in the app. The
    # cost: admin (or anything else) that needs archived vehicles has to
    # opt in explicitly via `all_objects` — see VehicleAdmin.get_queryset.
    objects = VehicleManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["registration_number"]

    def __str__(self):
        return self.registration_number


class ServiceRecord(models.Model):
    class Status(models.TextChoices):
        DUE = "DUE", "Due"
        BOOKED = "BOOKED", "Booked"
        IN_SERVICE = "IN_SERVICE", "In service"
        COMPLETED = "COMPLETED", "Completed"

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.PROTECT, related_name="service_records"
    )
    description = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, db_index=True)

    # When the record entered DUE. Overdue is derived as
    # `due_since + grace_period < now` at read time (session 4 decides
    # where the grace period constant lives) — never store an is_overdue
    # flag, since that would just be next_due_date recomputed a second way
    # and could drift out of sync with it.
    due_since = models.DateTimeField(null=True, blank=True)
    scheduled_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_odometer = models.PositiveIntegerField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    technicians = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="ServiceAssignment",
        # ServiceAssignment has two FKs to User (technician, assigned_by),
        # so Django can't infer which one is the M2M side — spell it out.
        through_fields=("service_record", "technician"),
        related_name="assigned_service_records",
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # Goal 10's alerts query and goal 8's dashboard counts both
            # filter on exactly this pair.
            models.Index(fields=["status", "due_since"], name="servicerecord_status_due_idx"),
        ]
        constraints = [
            # clean() below gives a friendly ValidationError in forms/admin,
            # but Django never calls full_clean() from save() automatically
            # — any code path that just calls .save() would bypass it. This
            # constraint is the actual guarantee at the database level.
            models.CheckConstraint(
                check=(
                    models.Q(completed_at__isnull=True, completed_odometer__isnull=True)
                    | models.Q(completed_at__isnull=False, completed_odometer__isnull=False)
                ),
                name="servicerecord_completed_fields_together",
            ),
        ]

    def __str__(self):
        return f"{self.vehicle.registration_number} — {self.get_status_display()}"

    def clean(self):
        super().clean()
        if bool(self.completed_at) != bool(self.completed_odometer):
            raise ValidationError(
                "completed_at and completed_odometer must both be set or both left null."
            )


class ServiceAssignment(models.Model):
    """Explicit M2M through-model — a bare ManyToManyField has nowhere to
    put assigned_at / assigned_by, and goal 9's timeline needs both."""

    service_record = models.ForeignKey(
        ServiceRecord, on_delete=models.CASCADE, related_name="assignments"
    )
    technician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="service_assignments"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    # related_name="+" — assigning users don't need a reverse accessor for
    # "assignments I made"; nothing in the brief asks for that view.
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        unique_together = [("service_record", "technician")]

    def __str__(self):
        return f"{self.technician} on {self.service_record_id}"


class TimelineEventQuerySet(models.QuerySet):
    def delete(self):
        raise TimelineImmutableError("TimelineEvent rows are append-only and cannot be bulk-deleted.")


class TimelineEvent(models.Model):
    """Append-only audit log for a ServiceRecord. Goal 9: nothing here can
    be edited or deleted after the fact, including by fleet managers — this
    is enforced below (save/delete overrides + queryset delete override +
    admin permissions), not just documented."""

    class EventType(models.TextChoices):
        CREATED = "CREATED", "Created"
        STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
        TECHNICIAN_ASSIGNED = "TECHNICIAN_ASSIGNED", "Technician assigned"
        TECHNICIAN_UNASSIGNED = "TECHNICIAN_UNASSIGNED", "Technician unassigned"
        NOTE_ADDED = "NOTE_ADDED", "Note added"

    service_record = models.ForeignKey(
        ServiceRecord, on_delete=models.CASCADE, related_name="timeline"
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    # null = system-generated (e.g. an automated status change), not tied
    # to a human actor.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    old_value = models.CharField(max_length=255, blank=True)
    new_value = models.CharField(max_length=255, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = TimelineEventQuerySet.as_manager()

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.event_type} on {self.service_record_id}"

    def save(self, *args, **kwargs):
        # pk is None only until the very first save; any save with a pk
        # already set is necessarily an update, which append-only forbids.
        if self.pk is not None:
            raise TimelineImmutableError("TimelineEvent rows cannot be modified after creation.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TimelineImmutableError("TimelineEvent rows cannot be deleted.")


class AlertDismissal(models.Model):
    """Keyed to the ServiceRecord, not the Vehicle — that's what makes goal
    10's reappearance rule work with no extra logic: once a vehicle comes
    due again, a new ServiceRecord exists that no dismissal covers, so the
    alert reappears on its own."""

    service_record = models.ForeignKey(ServiceRecord, on_delete=models.CASCADE)
    dismissed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("service_record", "dismissed_by")]

    def __str__(self):
        return f"{self.service_record_id} dismissed by {self.dismissed_by_id}"
