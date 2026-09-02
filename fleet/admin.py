from django.contrib import admin

from .models import (
    AlertDismissal,
    ServiceAssignment,
    ServiceRecord,
    TimelineEvent,
    Vehicle,
)


class ServiceAssignmentInline(admin.TabularInline):
    model = ServiceAssignment
    extra = 0
    autocomplete_fields = ["technician", "assigned_by"]


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = [
        "registration_number",
        "make",
        "model",
        "current_odometer",
        "next_due_date",
        "next_due_odometer",
        "is_archived",
    ]
    list_filter = ["is_archived", "make"]
    search_fields = ["registration_number", "make", "model"]

    def get_queryset(self, request):
        # Vehicle.objects (the default manager) excludes archived vehicles;
        # admin needs to see everything, including what it's excluding, so
        # it opts into the unfiltered manager explicitly.
        return Vehicle.all_objects.all()


@admin.register(ServiceRecord)
class ServiceRecordAdmin(admin.ModelAdmin):
    list_display = ["vehicle", "status", "due_since", "scheduled_date", "completed_at"]
    list_filter = ["status"]
    search_fields = ["vehicle__registration_number", "description"]
    autocomplete_fields = ["vehicle", "created_by"]
    inlines = [ServiceAssignmentInline]


@admin.register(ServiceAssignment)
class ServiceAssignmentAdmin(admin.ModelAdmin):
    list_display = ["service_record", "technician", "assigned_by", "assigned_at"]
    autocomplete_fields = ["service_record", "technician", "assigned_by"]


@admin.register(TimelineEvent)
class TimelineEventAdmin(admin.ModelAdmin):
    list_display = ["service_record", "event_type", "actor", "created_at"]
    list_filter = ["event_type"]
    readonly_fields = [f.name for f in TimelineEvent._meta.fields]

    # Goal 9: nothing here can be edited or deleted after the fact, by
    # anyone, including in the admin. The model already raises on update
    # and delete; these two just keep the admin from offering the buttons
    # (and from generating a confusing 500 when clicked).
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AlertDismissal)
class AlertDismissalAdmin(admin.ModelAdmin):
    list_display = ["service_record", "dismissed_by", "dismissed_at"]
    autocomplete_fields = ["service_record", "dismissed_by"]
