from django import forms

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
        # technicians are all lifecycle/assignment fields owned by later
        # sessions -- deliberately absent, not merely hidden in the
        # template. Used for both create (view sets vehicle/status/
        # due_since/created_by in form_valid) and edit (description-only
        # change, goal 3).
        fields = ["description"]
