from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from accounts.mixins import FleetManagerRequiredMixin
from .forms import VehicleForm
from .models import Vehicle


class VehicleListView(LoginRequiredMixin, ListView):
    """Visible to both roles -- technicians need to see the fleet too.
    Create/edit/archive controls are hidden in the template for
    non-managers, but that's cosmetic; the views those links point to
    enforce it server-side on their own.

    No get_queryset() override: ListView defaults to
    Vehicle._default_manager.all(), and Vehicle.objects (the filtered,
    archived-excluding manager) is declared first in session 2, so
    archived vehicles are excluded here for free.
    """

    model = Vehicle
    template_name = "fleet/vehicle_list.html"
    context_object_name = "vehicles"


class VehicleDetailView(LoginRequiredMixin, DetailView):
    """Visible to both roles.

    Overrides get_queryset() to Vehicle.all_objects: the default manager
    excludes archived vehicles, but goal 2 requires archiving to preserve
    history, which means the detail page -- and the service records on it
    -- must still render for an archived vehicle. Same escape hatch
    VehicleAdmin uses in session 2.
    """

    model = Vehicle
    template_name = "fleet/vehicle_detail.html"
    context_object_name = "vehicle"

    def get_queryset(self):
        return Vehicle.all_objects.all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # ServiceRecord has no default ordering (unlike TimelineEvent) --
        # newest first is a display choice for this page, not a model
        # invariant, so it's ordered here rather than in Meta.
        #
        # This traverses the reverse FK (service_records), which is scoped
        # by ServiceRecord's own manager, not Vehicle's -- archiving the
        # vehicle never hides its records, and the PROTECT on
        # ServiceRecord.vehicle means they can't be deleted out from under
        # it either.
        context["service_records"] = self.object.service_records.order_by("-created_at")
        return context


class VehicleFormMixin:
    """Shared by create and edit -- same form, same template, same
    success-redirect and message. Enough real duplication (5+ identical
    lines twice) to be worth factoring, unlike the two-line cases the
    project otherwise leaves alone."""

    model = Vehicle
    form_class = VehicleForm
    template_name = "fleet/vehicle_form.html"

    def get_success_url(self):
        return reverse("vehicle-detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"{self.object.registration_number} saved.")
        return response


class VehicleCreateView(FleetManagerRequiredMixin, VehicleFormMixin, CreateView):
    pass


class VehicleUpdateView(FleetManagerRequiredMixin, VehicleFormMixin, UpdateView):
    """Scoped to the default manager (excludes archived) on purpose:
    editing specs on an archived vehicle isn't a use case this session's
    brief describes. Restore it first."""
