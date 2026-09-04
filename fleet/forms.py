from django import forms
from django.utils import timezone

from accounts.models import User
from .models import ServiceRecord, Vehicle


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        # Deliberately excludes next_due_date/next_due_odometer -- those
        # are derived fields the session-4 service layer owns (see the
        # Vehicle docstring); putting them on a form would let a manager
        # override the computed due-ness by hand. is_archived is also
        # excluded -- archiving is a separate POST action, not a field to
        # edit inline.
        fields = [
            "registration_number",
            "make",
            "model",
            "current_odometer",
            "service_interval_days",
            "service_interval_km",
        ]

    def clean_registration_number(self):
        # ModelForm's automatic uniqueness check (Model.validate_unique(),
        # via Model._perform_unique_checks()) queries Vehicle._default_manager
        # -- which is Vehicle.objects, the first-declared manager, and
        # excludes archived vehicles (see the Vehicle docstring on why that's
        # the default). registration_number is unique at the DB level across
        # EVERY row though, archived included, so a plate that belongs to an
        # archived vehicle sails past that automatic check, then fails at
        # save() with an uncaught IntegrityError -- a 500, not a form error.
        # This checks against Vehicle.all_objects instead, so the conflict
        # is always caught here, before save() is ever called.
        registration_number = self.cleaned_data["registration_number"]
        conflict = Vehicle.all_objects.filter(registration_number=registration_number)
        if self.instance.pk:
            conflict = conflict.exclude(pk=self.instance.pk)
        existing = conflict.first()
        if existing is not None:
            if existing.is_archived:
                raise forms.ValidationError(
                    "This registration number belongs to an archived vehicle. "
                    "Archiving preserves history rather than freeing the plate "
                    "-- restore that vehicle to reuse it, or choose a different "
                    "registration number."
                )
            raise forms.ValidationError("A vehicle with this registration number already exists.")
        return registration_number


class ServiceRecordDescriptionForm(forms.ModelForm):
    class Meta:
        model = ServiceRecord
        # Only field either role is allowed to touch through a form. status,
        # due_since, scheduled_date, completed_at, completed_odometer and
        # technicians are all lifecycle/assignment fields owned by
        # fleet/services.py -- deliberately absent from every form,
        # everywhere, not merely hidden in a template. Used for both
        # create (the view sets vehicle/status/due_since/created_by in
        # form_valid) and edit (description-only change, goal 3).
        fields = ["description"]


class BookServiceForm(forms.Form):
    """DUE -> BOOKED. Not a ModelForm -- these two inputs are arguments to
    book_service(), not a 1:1 mapping onto ServiceRecord fields (the
    technician goes into a ServiceAssignment row, not a ServiceRecord
    column). Manager-only at the view layer (FleetManagerRequiredMixin on
    ServiceRecordBookView) -- this form doesn't enforce that itself, same
    division of labour as AssignTechnicianForm below."""

    scheduled_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    technician = forms.ModelChoiceField(
        queryset=User.objects.filter(role=User.Role.TECHNICIAN),
        label="Assign technician",
    )

    def clean_scheduled_date(self):
        # Inline echo of the same rule book_service() enforces -- that
        # function is still the authority (it's reachable from outside this
        # form), but a manager who mistypes a date deserves the error next
        # to the field rather than as a page-level message.
        scheduled_date = self.cleaned_data["scheduled_date"]
        if scheduled_date < timezone.localdate():
            raise forms.ValidationError("Scheduled date cannot be in the past.")
        return scheduled_date


class CompleteServiceForm(forms.Form):
    """IN_SERVICE -> COMPLETED."""

    completed_odometer = forms.IntegerField(min_value=0, label="Odometer reading (km)")


class AssignTechnicianForm(forms.Form):
    """Goal 5's add-technician form on the record detail page. Manager-only
    at the view layer (FleetManagerRequiredMixin) -- this form doesn't
    enforce that itself, same division of labour as BookServiceForm."""

    technician = forms.ModelChoiceField(
        queryset=User.objects.filter(role=User.Role.TECHNICIAN),
        label="Add technician",
    )


class OdometerImportForm(forms.Form):
    """Goal 7's bulk odometer upload. Just a file field -- every actual
    validation (file type, size, row shape, per-row rejection reasons)
    happens in fleet.csv_io.import_odometer_readings, not here, since
    those rules produce a per-row report rather than a single form
    error."""

    file = forms.FileField(label="CSV file")


class TimelineNoteForm(forms.Form):
    """NOTE_ADDED timeline events -- open to managers and the assigned
    technician, same permission as viewing the record at all."""

    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
