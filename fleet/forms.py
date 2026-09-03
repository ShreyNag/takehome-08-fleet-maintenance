from django import forms

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
    column).

    `user` is optional and cosmetic only: when passed and it's a
    technician (not a manager), the technician choice is narrowed to just
    that user, so the rendered form can't even offer picking someone else
    in. The actual rule -- a technician actor may only book themselves --
    is enforced server-side in ServiceRecordBookView regardless of what
    this form was constructed with; this narrowing just keeps the UI
    honest about it.
    """

    scheduled_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    technician = forms.ModelChoiceField(
        queryset=User.objects.filter(role=User.Role.TECHNICIAN),
        label="Assign technician",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None and user.is_technician:
            self.fields["technician"].queryset = User.objects.filter(pk=user.pk)
            self.fields["technician"].initial = user.pk


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
