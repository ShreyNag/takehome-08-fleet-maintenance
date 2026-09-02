from django import forms

from .models import Vehicle


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
