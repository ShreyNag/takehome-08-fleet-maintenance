from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView
from django.views.generic.detail import SingleObjectMixin

from accounts.mixins import (
    FleetManagerRequiredMixin,
    ManagerOrAssignedTechnicianMixin,
    VehicleManagerOrAssignedTechnicianMixin,
    VehicleTechnicianScopedQuerysetMixin,
)
from accounts.models import User
from .csv_io import CsvImportError, export_service_records_csv, import_odometer_readings
from .dashboard import dashboard_context
from .filters import SORT_FIELDS, filtered_service_records
from .forms import (
    AssignTechnicianForm,
    BookServiceForm,
    CompleteServiceForm,
    OdometerImportForm,
    ServiceRecordDescriptionForm,
    TimelineNoteForm,
    VehicleForm,
)
from .models import ServiceRecord, TimelineEvent, Vehicle
from .services import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionInput,
    ServiceLifecycleError,
    assign_technician,
    book_service,
    complete_service,
    ensure_due_record,
    start_service,
    unassign_technician,
)


class DashboardView(FleetManagerRequiredMixin, TemplateView):
    """Goal 8: the manager's landing page. Manager-only -- a technician's
    landing page is ServiceRecordListView (goal 5); requesting this URL
    directly gets the same 403 FleetManagerRequiredMixin gives every other
    manager-only view, not a redirect (decision #16).

    All the actual aggregation lives in fleet.dashboard.dashboard_context
    so it can be unit-tested (query count included) without going through
    a view/template at all.
    """

    template_name = "dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(dashboard_context())
        return context


class VehicleListView(VehicleTechnicianScopedQuerysetMixin, LoginRequiredMixin, ListView):
    """Visible to both roles -- but a technician now sees only vehicles
    they have at least one ServiceAssignment against (any status,
    including completed -- past work is still their work). Create/edit/
    archive controls are hidden in the template for non-managers, but
    that's cosmetic; the views those links point to enforce it
    server-side on their own.

    with_service_status() is annotated here, not computed per-row in the
    template: it runs as Exists() subqueries in the one query this view
    already makes, so it doesn't scale with vehicle count.
    """

    model = Vehicle
    template_name = "fleet/vehicle_list.html"
    context_object_name = "vehicles"

    def get_queryset(self):
        # super().get_queryset() is VehicleTechnicianScopedQuerysetMixin's:
        # ListView's default (Vehicle.objects.all(), archived excluded),
        # technician-filtered on top if applicable. with_service_status()
        # chains onto whatever that returns -- the annotation doesn't care
        # how many rows survived the scoping filter above it.
        return super().get_queryset().with_service_status()


class VehicleDetailView(VehicleManagerOrAssignedTechnicianMixin, DetailView):
    """Visible to both roles -- managers unconditionally, technicians only
    for a vehicle they have at least one ServiceAssignment against (any
    status). VehicleManagerOrAssignedTechnicianMixin.get_object() 403s an
    unassigned technician rather than 404ing them, same reasoning as
    ManagerOrAssignedTechnicianMixin for ServiceRecord.

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
        return Vehicle.all_objects.with_service_status()

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
        # "The" open record -- ensure_due_record and the transition state
        # machine both assume at most one open record per vehicle, but
        # nothing at the DB level enforces that (a manager could create a
        # second one by hand while one is already open), so this picks the
        # most recently due one if more than one somehow exists rather
        # than asserting there's exactly one.
        context["open_service_record"] = (
            self.object.service_records.filter(
                status__in=[
                    ServiceRecord.Status.DUE,
                    ServiceRecord.Status.BOOKED,
                    ServiceRecord.Status.IN_SERVICE,
                ]
            )
            .order_by("-due_since")
            .first()
        )
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

    def form_valid(self, form):
        response = super().form_valid(form)
        # This is the one place a manager can change current_odometer, so
        # it's one of the two places (the other is complete_service) that
        # needs to re-derive due-ness -- the mileage threshold could have
        # just been crossed by this edit.
        ensure_due_record(self.object)
        return response


class VehicleArchiveView(FleetManagerRequiredMixin, View):
    """POST-only: this changes state, so a GET would be CSRF-exposed and
    crawlable (a link-prefetcher or a stray GET could archive a vehicle).
    Matches how logout is already done in base.html -- a small POST form
    with a button, not a link.

    Looked up via all_objects, not the default manager: an already-
    archived vehicle isn't findable through Vehicle.objects, and archiving
    it again should still be a well-defined no-op rather than a 404.
    """

    http_method_names = ["post"]

    def post(self, request, pk):
        vehicle = get_object_or_404(Vehicle.all_objects, pk=pk)
        vehicle.is_archived = True
        vehicle.save(update_fields=["is_archived", "updated_at"])
        messages.success(request, f"{vehicle.registration_number} archived.")
        return redirect("vehicle-list")


class VehicleRestoreView(FleetManagerRequiredMixin, View):
    """See VehicleArchiveView -- same POST-only, same all_objects lookup,
    opposite direction."""

    http_method_names = ["post"]

    def post(self, request, pk):
        vehicle = get_object_or_404(Vehicle.all_objects, pk=pk)
        vehicle.is_archived = False
        vehicle.save(update_fields=["is_archived", "updated_at"])
        messages.success(request, f"{vehicle.registration_number} restored.")
        return redirect("vehicle-archived-list")


class ArchivedVehicleListView(FleetManagerRequiredMixin, ListView):
    """Manager-only: this is where archived vehicles actually live once
    they're off the default list."""

    template_name = "fleet/vehicle_archived_list.html"
    context_object_name = "vehicles"

    def get_queryset(self):
        return Vehicle.all_objects.filter(is_archived=True)


class VehicleOdometerImportView(FleetManagerRequiredMixin, View):
    """Goal 7: bulk odometer CSV upload. GET shows the upload form; POST
    processes the file and re-renders the SAME page with a per-row report
    -- no redirect on success, since the report is exactly what the
    manager needs to see next, and a redirect would either have to stuff
    the whole report into the session or throw it away.

    A CsvImportError (wrong file type, empty file, over the row/byte cap)
    is a FILE-level problem, not a row's -- shown as a single message with
    a 400, no report table, since per-row processing never ran at all.
    """

    http_method_names = ["get", "post"]
    template_name = "fleet/odometer_import.html"

    def get(self, request):
        return render(request, self.template_name, {"form": OdometerImportForm()})

    def post(self, request):
        form = OdometerImportForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "Choose a file to upload.")
            return render(request, self.template_name, {"form": form}, status=400)

        try:
            report = import_odometer_readings(form.cleaned_data["file"], actor=request.user)
        except CsvImportError as exc:
            messages.error(request, str(exc))
            return render(request, self.template_name, {"form": OdometerImportForm()}, status=400)

        return render(request, self.template_name, {"form": OdometerImportForm(), "report": report})


class ServiceRecordCreateView(FleetManagerRequiredMixin, CreateView):
    """Created against a vehicle taken from the URL, not a dropdown the
    user could tamper with to point a new record at a different vehicle.

    Vehicle looked up via the default (active-only) manager: an archived
    vehicle keeps the service records it already has (goal 2), but this
    form is how NEW records get created, and creating fresh work against
    an archived vehicle isn't a described use case -- it 404s here until
    the vehicle is restored.
    """

    model = ServiceRecord
    form_class = ServiceRecordDescriptionForm
    template_name = "fleet/servicerecord_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.vehicle = get_object_or_404(Vehicle, pk=kwargs["vehicle_pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["vehicle"] = self.vehicle
        return context

    def form_valid(self, form):
        # Lifecycle fields the form doesn't expose -- set here, not on the
        # form, so neither role can submit them. Session 4 owns everything
        # past this initial DUE state.
        form.instance.vehicle = self.vehicle
        form.instance.status = ServiceRecord.Status.DUE
        form.instance.due_since = timezone.now()
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, "Service record created.")
        return response

    def get_success_url(self):
        return reverse("service-record-detail", kwargs={"pk": self.object.pk})


class ServiceRecordDetailContextMixin:
    """Shared by the read-only detail view and every transition action
    view below: both can end up rendering the same servicerecord_detail.html
    template -- the detail view always, an action view only when it
    rejects an illegal move and needs to re-render the page with an error
    message and a 4xx status rather than redirecting (see
    ServiceRecordActionView.post).
    """

    def build_detail_context(self, service_record):
        # Who may act is identical to who may view this page at all
        # (ManagerOrAssignedTechnicianMixin), so no separate permission
        # check is needed here -- reaching this method already proves it.
        # Which action is legal is read straight from ALLOWED_TRANSITIONS,
        # the single source of truth the service layer itself checks
        # against, rather than duplicating the state machine's shape here.
        allowed = ALLOWED_TRANSITIONS.get(service_record.status, set())
        context = {
            "service_record": service_record,
            "timeline": service_record.timeline.select_related("actor"),
            "note_form": TimelineNoteForm(),
            "technicians": service_record.technicians.all(),
            "show_book_form": ServiceRecord.Status.BOOKED in allowed,
            "show_start_button": ServiceRecord.Status.IN_SERVICE in allowed,
            "show_complete_form": ServiceRecord.Status.COMPLETED in allowed,
        }
        if context["show_book_form"]:
            context["book_form"] = BookServiceForm()
        if context["show_complete_form"]:
            context["complete_form"] = CompleteServiceForm()
        # Assignment (goal 5) is manager-only, including for a technician
        # already on the record -- the form (and the remove buttons the
        # template renders alongside `technicians` above) only appears for
        # a manager, same permission FleetManagerRequiredMixin enforces
        # server-side on the two views below.
        if self.request.user.is_fleet_manager:
            context["assign_technician_form"] = AssignTechnicianForm()
        return context


class ServiceRecordDetailView(ServiceRecordDetailContextMixin, ManagerOrAssignedTechnicianMixin, DetailView):
    """Visible to managers, and to technicians only if assigned --
    enforced by ManagerOrAssignedTechnicianMixin.get_object(), which 403s
    rather than 404s an unassigned technician (see accounts/mixins.py for
    why TechnicianScopedQuerysetMixin doesn't fit this)."""

    model = ServiceRecord
    template_name = "fleet/servicerecord_detail.html"
    context_object_name = "service_record"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.build_detail_context(self.object))
        return context


class ServiceRecordUpdateView(ManagerOrAssignedTechnicianMixin, UpdateView):
    """Description only -- goal 3 is explicit that the assignee can update
    the work description but not who is assigned. The form itself only
    exposes description (ServiceRecordDescriptionForm), and permission is
    identical to ServiceRecordDetailView (manager or assigned technician),
    so anyone who can reach the record can also edit its description --
    no separate template-level check needed on top of the shared mixin.
    """

    model = ServiceRecord
    form_class = ServiceRecordDescriptionForm
    template_name = "fleet/servicerecord_form.html"
    context_object_name = "service_record"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["vehicle"] = self.object.vehicle
        return context

    def get_success_url(self):
        return reverse("service-record-detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Description updated.")
        return response


class ServiceRecordActionView(ServiceRecordDetailContextMixin, ManagerOrAssignedTechnicianMixin, SingleObjectMixin, View):
    """Base for the three transition actions (book/start/complete). Same
    permission as the detail/edit views: a manager, or a technician
    assigned to this record.

    On success: redirect back to the detail page (post/redirect/get, so a
    refresh doesn't resubmit the action) with a success message.
    On a ServiceLifecycleError: re-render the SAME detail page directly
    (no redirect) with an error message and an HTTP 400 -- goal 4 wants
    the rejection to come back as a 4xx with an explanation, and a
    redirect's eventual 200 wouldn't satisfy that. Safe to reuse
    self.object here because every ServiceLifecycleError in services.py is
    raised before any mutation happens (see the note at the top of
    services.py) -- self.object is never stale when this fires.
    """

    model = ServiceRecord
    http_method_names = ["post"]
    template_name = "fleet/servicerecord_detail.html"
    success_message = ""

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            self.perform(request)
        except ServiceLifecycleError as exc:
            messages.error(request, str(exc))
            context = self.build_detail_context(self.object)
            return render(request, self.template_name, context, status=400)
        messages.success(request, self.success_message)
        return redirect("service-record-detail", pk=self.object.pk)

    def perform(self, request):
        raise NotImplementedError


class ServiceRecordBookView(ServiceRecordActionView):
    success_message = "Service booked."

    def perform(self, request):
        form = BookServiceForm(request.POST)
        if not form.is_valid():
            raise InvalidTransitionInput("Enter a valid scheduled date and technician.")
        book_service(
            self.object,
            scheduled_date=form.cleaned_data["scheduled_date"],
            technician=form.cleaned_data["technician"],
            actor=request.user,
        )


class ServiceRecordStartView(ServiceRecordActionView):
    success_message = "Service started."

    def perform(self, request):
        start_service(self.object, actor=request.user)


class ServiceRecordCompleteView(ServiceRecordActionView):
    success_message = "Service completed."

    def perform(self, request):
        form = CompleteServiceForm(request.POST)
        if not form.is_valid():
            raise InvalidTransitionInput("Enter a valid odometer reading.")
        complete_service(
            self.object,
            completed_odometer=form.cleaned_data["completed_odometer"],
            actor=request.user,
        )


class ServiceRecordAddNoteView(ManagerOrAssignedTechnicianMixin, SingleObjectMixin, View):
    """NOTE_ADDED timeline events -- same permission as viewing the record.
    Not a transition, so it doesn't go through services.py's state machine;
    a single TimelineEvent.objects.create() is already one atomic write on
    its own, no explicit transaction.atomic() needed.
    """

    model = ServiceRecord
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = TimelineNoteForm(request.POST)
        if form.is_valid():
            TimelineEvent.objects.create(
                service_record=self.object,
                event_type=TimelineEvent.EventType.NOTE_ADDED,
                actor=request.user,
                note=form.cleaned_data["note"],
            )
            messages.success(request, "Note added.")
        else:
            messages.error(request, "Note cannot be empty.")
        return redirect("service-record-detail", pk=self.object.pk)


class ServiceRecordAssignTechnicianView(FleetManagerRequiredMixin, SingleObjectMixin, View):
    """Goal 5: manager-only, deliberately FleetManagerRequiredMixin rather
    than ManagerOrAssignedTechnicianMixin -- a technician already assigned
    to this record still gets 403 here, unlike the view/edit/note actions
    above which that other mixin allows them into."""

    model = ServiceRecord
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = AssignTechnicianForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Select a valid technician.")
            return redirect("service-record-detail", pk=self.object.pk)
        assignment, created = assign_technician(
            self.object, form.cleaned_data["technician"], actor=request.user
        )
        if created:
            messages.success(request, f"{assignment.technician} assigned.")
        else:
            messages.info(request, f"{assignment.technician} is already assigned.")
        return redirect("service-record-detail", pk=self.object.pk)


class ServiceRecordListView(LoginRequiredMixin, ListView):
    """Goal 6: every service record across every vehicle the viewer can
    see -- and goal 5's technician landing page IS this view, not a
    separate one: scoping comes from fleet.filters.scoped_service_records
    (same rule TechnicianScopedQuerysetMixin encodes elsewhere), which
    restricts a technician to their own records with no extra parameter
    needed, so pointing the login redirect at this URL is sufficient on
    its own.

    Scoping/filtering/sorting all live in fleet.filters rather than here so
    ServiceRecordExportView (goal 7, not a ListView and so unable to reuse
    get_queryset()) can call the exact same function and never show
    different rows than whatever page it was exported from.
    """

    model = ServiceRecord
    template_name = "fleet/servicerecord_list.html"
    context_object_name = "service_records"
    paginate_by = 20

    def get_queryset(self):
        # Cached on self rather than recomputed in get_context_data: the
        # sort-was-valid flag has to survive from here (where it's known)
        # to there (where the message gets surfaced), and ListView only
        # calls get_queryset() once per request.
        queryset, self.sort_was_valid = filtered_service_records(self.request.user, self.request.GET)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.sort_was_valid:
            # Goal 6's correction: an unrecognised sort parameter falls
            # back to the default rather than 500ing or being passed to
            # order_by() unsanitised, but that fallback isn't silent --
            # goal 4's illegal-transition messages set the precedent that
            # a rejected input says so, in a message a user actually sees.
            messages.warning(
                self.request,
                "Unrecognised sort — showing results in the default order instead.",
            )

        querystring = self.request.GET.copy()
        querystring.pop("page", None)
        context["querystring"] = querystring.urlencode()

        # Sort-header links: one (asc, desc) pair per allowed field, each
        # carrying every OTHER current filter/search param forward and
        # resetting to page 1 -- built here rather than with template
        # logic because QueryDict manipulation reads far worse in a
        # template than three lines of Python.
        sort_links = {}
        for field in SORT_FIELDS:
            for direction in ("asc", "desc"):
                link_params = querystring.copy()
                link_params["sort"] = field
                link_params["dir"] = direction
                sort_links[f"{field}_{direction}"] = link_params.urlencode()
        context["sort_links"] = sort_links

        context["vehicles"] = Vehicle.all_objects.order_by("registration_number")
        context["technicians"] = User.objects.filter(role=User.Role.TECHNICIAN)
        context["statuses"] = ServiceRecord.Status.choices
        return context


class ServiceRecordExportView(LoginRequiredMixin, View):
    """Goal 7: streams every record the viewer can see as CSV, respecting
    whatever filters/search/sort are in the querystring -- identical scope
    to ServiceRecordListView because both call
    fleet.filters.filtered_service_records with the same request.GET, so
    an export taken from a filtered page can never show different rows
    than the page itself.
    """

    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        queryset, _ = filtered_service_records(request.user, request.GET)
        response = StreamingHttpResponse(
            export_service_records_csv(queryset), content_type="text/csv"
        )
        response["Content-Disposition"] = 'attachment; filename="service-records.csv"'
        return response


class ServiceRecordUnassignTechnicianView(FleetManagerRequiredMixin, SingleObjectMixin, View):
    """Goal 5's inverse. technician_pk comes from the URL, not a form field
    -- each remove button in the template posts straight to a specific
    technician's own unassign URL."""

    model = ServiceRecord
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        technician = get_object_or_404(User, pk=kwargs["technician_pk"])
        if unassign_technician(self.object, technician, actor=request.user):
            messages.success(request, f"{technician} removed.")
        else:
            messages.info(request, f"{technician} was not assigned.")
        return redirect("service-record-detail", pk=self.object.pk)
