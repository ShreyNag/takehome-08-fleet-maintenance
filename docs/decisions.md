# Decisions

## 1. Django over FastAPI

**Chose:** Django 5.2 with server-rendered templates.
**Rejected:** FastAPI + SQLAlchemy + a React frontend.

Django ships email/password auth, migrations, an admin for inspecting data,
and pagination. On FastAPI all four are hand-built, and none of that plumbing
scores against the ten goals. Goal 6 also requires that search, filtering and
pagination happen on the server rather than in the browser — with
server-rendered templates that is structurally true rather than something to
argue for, and it removes CORS, token handling and a second deployment.

The cost is that this is not an API-first design. If the fleet ever needed a
mobile client, the views would need splitting into a serialised API layer. For
a single web client that cost is hypothetical and the savings are immediate.

## 2. AbstractBaseUser over AbstractUser, migrated first

**Chose:** A custom user model subclassing `AbstractBaseUser` +
`PermissionsMixin`, with email as `USERNAME_FIELD`, a `role` field, and a
hand-written manager. Created and migrated before any other app touched the
database.
**Rejected:** `AbstractUser` (keeps a username field the app never uses), and
Django's default user with a separate profile table holding the role.

The brief says people sign in with an email, so a username column would be
dead weight and a second uniqueness constraint to reason about. A profile
table would mean a join on every permission check.

The ordering mattered more than the choice. Django cannot swap
`AUTH_USER_MODEL` after the initial migration without resetting the database,
so this was deliberately the first commit that touched models.

## 3. Neon for Postgres rather than Render's own free database

**Chose:** Web service on Render, database on Neon.
**Rejected:** Both on Render.

Render's free Postgres instances expire 30 days after creation. A reviewer
opening this app five weeks after submission would find the data gone. Neon's
free tier has no such expiry. The cost is a second provider and a connection
string to manage; the benefit is that the live URL still works whenever it
gets clicked.

## 4. Seeding from build.sh — reversed

**Originally chose:** Run `python manage.py seed_users` manually on Render
after the first deploy.
**Reversed to:** Run it as the final step of `build.sh`, on every deploy.
**Later reversed:** Render's free tier has no shell access — that's a paid
feature — so the manual step the original plan depended on could never
actually be run.

The original plan assumed shell access. Render's free tier doesn't have it —
that's a paid feature. Since `seed_users` was already written with
`get_or_create` and verified idempotent, moving it into the build was safe:
it does nothing after the first run.

The reversal was prompted by a real gap, not a script bug. `build.sh` already
had `set -o errexit` and already ran `migrate` before anything else — but it
never called `seed_users` at all. The first deploy came up healthy with an
empty `accounts_user` table, because the only place `seed_users` was invoked
was a manual "run this in the Render Shell" step in `DEPLOY.md`, and nobody
ran it. Fix: call `seed_users` after `migrate` as the last line of
`build.sh`, so seeding happens automatically on every deploy instead of
depending on a manual step reviewers have no reason to know about.

## 5. Storing next-due values on the vehicle rather than computing them

**Session 2.**

**Chose:** `next_due_date` and `next_due_odometer` as indexed columns on
`Vehicle`, recalculated by `complete_service()` on every completion.
**Rejected:** Computing due-ness on read, from the last completed service plus
the intervals.

Computing is simpler and cannot drift, but goal 6 needs server-side filtering,
sorting and pagination with a total count, and goal 8 needs aggregate
due/overdue counts — both require due-ness to exist in SQL. As a Python
property it's a full table scan, and `Paginator` can't total without
materialising every row: exactly the "load everything into the browser"
pattern goal 6 forbids, one layer down.

The cost is that these columns can drift from the truth. The mitigation is one
write path, `complete_service()`, stated as an invariant in the model
docstring. An odometer update reads these fields — `ensure_due_record()`
checks whether a reading crossed the existing threshold — but never writes
them: the thresholds are anchored to the last completed service, not to
odometer edits in between.

**Correction, final review:** this invariant was documented — here, in
`architecture.md`, in `schema.md`, and in the `Vehicle` docstring — as TWO
write paths, completion and odometer update, from session 2 onward. Never
true; only `complete_service()` ever wrote these fields. Caught against the
actual code in a final review and corrected rather than silently fixed — a
documented invariant that was never true is worth showing.

The decision I'd most expect to be challenged on, and the one I'd revisit
first if this app were read-light and write-heavy instead.

## 6. The whole schema in one pass, before any views

**Session 2.**

**Chose:** Model every table in session 2 — vehicles, records, assignments,
timeline, dismissals — even though views for most arrive in sessions 4 to 6.
**Rejected:** Growing the schema feature by feature alongside the UI.

Feature-by-feature produces a migration per feature, each reshaping tables that
already hold data, and each reshape is time not spent on the ten goals. The
relationships also constrain each other: the timeline's need for `assigned_by`
is what forces the assignment through-model, and that only becomes visible when
both are designed together.

The risk is designing for requirements I have not built yet and getting them
wrong. Accepted because the brief specifies all ten goals up front, so the
requirements are known rather than speculative.

## 7. Assignment as an explicit through-model

**Session 2.**

**Chose:** A `ServiceAssignment` model with `assigned_at` and `assigned_by`.
**Rejected:** A plain `ManyToManyField` between `ServiceRecord` and users.

A bare M2M gives Django a hidden join table with nowhere to record who
assigned whom or when. Goal 9 requires every assignment and unassignment in
the timeline with an actor, so the metadata has to live somewhere. Declaring
the through-model up front avoids a later migration converting the implicit
table into an explicit one.

## 8. Enforcing immutability in code, not by convention

**Session 2.**

**Chose:** `TimelineEvent.save()` raises when the instance already has a
primary key, `delete()` raises on both model and queryset, and the admin
returns `False` from `has_change_permission` and `has_delete_permission`.
**Rejected:** Documenting the rule and simply never writing an update path.

Goal 9 says nothing in the timeline can be edited or deleted after the fact,
*including by fleet managers*. A rule that exists only as a convention is one
careless queryset away from being broken, and the Django admin would otherwise
have handed a superuser full edit rights on the audit trail. Enforcing it at
the model means any future code path fails loudly rather than silently
rewriting history.

Visible in the admin index: Timeline events shows "View" where every other
model shows "Change".

**Tested by the rule itself, session 6:** clearing leftover test vehicles
before seeding demo data meant cascading deletes through their service
records into timeline events, which the model correctly refused. Archived
those vehicles instead — goal 2's retirement mechanism — rather than deleting
via raw SQL to route around the guarantee.

Worth being precise about what the rule does and does not promise: no code
path in the application can rewrite history, including a superuser through
the admin. An operator with direct SQL access still can. A database-level
constraint would close that, and is the first thing I would add if this were
production.

## 9. A separate env-driven admin account rather than promoting the demo manager

**Session 2.**

**Chose:** `seed_users` optionally creates a staff/superuser account from
`DJANGO_ADMIN_EMAIL` and `DJANGO_ADMIN_PASSWORD`, skipping silently when
unset. Not recorded in `SUBMISSION.md`.
**Rejected:** Setting `is_staff` on `manager@fleetcare.demo`.

A fleet manager who is also a Django superuser cannot demonstrate role
enforcement honestly — a reviewer could not tell whether the app's own
permission checks work or whether admin rights were carrying it. Keeping them
separate means `manager@fleetcare.demo` is rejected at `/admin/`, which is
itself evidence that application roles and Django staff permissions are
distinct concerns.

## 10. Keeping derived and lifecycle fields off every form

**Session 3.**

**Chose:** Vehicle forms expose registration, make, model, odometer and the two
intervals — never `next_due_date` or `next_due_odometer`. Service record forms
expose description only — never `status`, `due_since`, `scheduled_date`,
`completed_at` or `completed_odometer`.
**Rejected:** Including them and relying on validation, or on managers simply
not editing them.

Decision #5 established that the next-due columns are denormalised and safe
only because exactly one code path writes them. A form field would be a
second one. Leaving these off the form isn't a UI choice, it's how that
invariant survives contact with a UI built two sessions before the service
layer that owns it.

The same reasoning covers the lifecycle fields. Goal 4 requires illegal status
moves to be rejected by the server with an explanation; a form that lets a
manager set `status` directly would route around the state machine entirely
before it's even written.

`status` and `due_since` are set in `form_valid`, not by the user.

## 11. No CSS framework, no JavaScript

**Session 3.**

**Chose:** Plain hand-written CSS, server-rendered forms, no client-side
framework of any kind.
**Rejected:** Bootstrap or Tailwind, and HTMX for partial updates.

The brief is explicit that no stack scores better than another and that time
spent on things outside the ten goals will show. Goals 4, 7, 8 and 10 are the
hard ones and they're all server-side. Styling buys nothing here.

The visible cost is that the app is plain. The accepted risk was that the
dashboard in goal 8 would need a chart, which looked like it would force one
small JavaScript dependency as a deliberate, single exception.

**Session 6:** it didn't. Goal 8's eight-week chart turned out to be a div per
week with height scaled as a percentage of the maximum — plain CSS, not
Chart.js from a CDN. Weeks with zero completions still render as empty
columns, so a missing week reads as a bug rather than a tidy axis. The
predicted exception never had to be spent.

## 12. Scoping vehicles to technicians — a reversal

**Session 4.**

**Originally chose:** Both roles see the whole fleet. Goal 1 restricts
what technicians can *do*, not what they can see, so gating the vehicle list
looked like inventing a rule the brief hadn't asked for.

**Reversed to:** Technicians see only vehicles they hold at least one service
assignment against, in any status. Managers see everything.

Re-reading goal 1: technicians "can only see and update service records
assigned to them." The word is *see*, and the vehicle is the context around
the record rather than something separate from it. Showing a technician the
full fleet is a wider reading than the brief supports.

Scoped on *any* assignment ever, not open ones. A technician who serviced a
van last month should still be able to look it up — narrowing to open records
would make their own completed work disappear.

An unauthorised vehicle returns 403, not 404. This needed an object-level
check separate from the queryset filter: filtering the queryset alone means
`get_object()` raises `Http404`, which conflates "not permitted" with "does
not exist" and makes the test weaker.

**Cost:** technicians land on a near-empty vehicle list until goal 5's
cross-vehicle record list gives them a proper home screen. Acceptable, because
that list is the view they should be starting from anyway.

## 13. Sibling mixins rather than one mixin with a branch

**Session 4.**

**Chose:** `VehicleTechnicianScopedQuerysetMixin` alongside the existing
`TechnicianScopedQuerysetMixin`, and a matching pair for the object-level
checks.
**Rejected:** Extending the existing mixins with a branch on model type.

The two filters look similar and aren't. Scoping service records is
`.filter(technicians=user)` — one direct M2M hop on the queryset's own model.
Scoping vehicles is `.filter(service_records__technicians=user)` — a
reverse-FK then an M2M, which duplicates a vehicle once per matching record
and therefore needs `.distinct()`. The record version never has that problem.

Folding "how many hops, and do I need distinct" into one class produces a
branch wearing the costume of an abstraction: shared name, shared file,
nothing actually shared. Two small explicit mixins are longer and easier to
read.

This was proposed by the coding tool in response to a prompt that asked for
one mixin "if it fits, or a sibling if forcing them together makes both
worse." I asked for the reasoning before implementation and accepted it on the
`.distinct()` argument.

## 14. Service status as a SQL annotation, not a model property

**Session 4.**

**Chose:** `VehicleQuerySet.with_service_status()`, annotating two correlated
`Exists()` subqueries into a four-way `Case`/`When` label.
**Rejected:** A Python property on `Vehicle` computing the label per instance.

A property is simpler and produces an N+1 — one query per vehicle on the fleet
list, and the same again on the dashboard. Worse, it can't be filtered,
sorted or counted, which goals 6 and 8 both require. This is the same argument
as decision #5: due-ness has to exist in SQL, not only in Python.

`Exists()` subqueries rather than joins, so the query count stays flat
regardless of fleet size, and the annotation can't interact badly with the
`.distinct()` from technician scoping — it's computed per vehicle pk, not per
joined row. Asserted with `assertNumQueries` at two different fleet sizes.

The overdue subquery is built from the existing `ServiceRecord.objects
.overdue()` with a vehicle filter added, not a reimplementation. The grace
period comparison already existed as a queryset filter and a model property; a
third copy is how those quietly diverge.

Case ordering matters: OVERDUE is tested before DUE, since an overdue record
is also an open one and `Case` returns on first match.


## 15. Writing the assignment event on booking

**Session 5.**

**Chose:** `book_service` calls `assign_technician`, which writes a
`TECHNICIAN_ASSIGNED` event alongside the `DUE → BOOKED` status event. Two
events per booking.
**Rejected:** Suppressing the assignment event on the booking path, keeping one
event per booking.

The tool proposed a `write_event=False` flag so that an existing test asserting
exactly one event per booking would keep passing, reasoning that the
`ServiceAssignment` row already records who assigned whom and when.

The reasoning is real but goal 9 says the timeline shows "every technician
assignment and unassignment." Under the suppressed version, a booking attaches
a technician and the timeline shows only a status change — someone reading it
to find when a technician came onto the record would not find out.

The test was updated to expect two events. It encoded an assumption from before
assignment existed as a first-class action; changing a test because
requirements grew is legitimate, suppressing an audit event to keep a test green
is not.

## 16. `icontains` over Postgres full-text search

**Session 5.**

**Chose:** `description__icontains` for goal 6's text search.
**Rejected:** A `tsvector` column with a GIN index.

`ILIKE '%term%'` cannot use a standard index, so this degrades at scale. At a
few dozen vehicles and a few hundred records it is imperceptible, and full-text
search would have meant a migration, a trigger or a save hook to maintain the
vector, and a different query API — time better spent on goals 8 and 10.

Recorded in a comment at the call site as the fix if it ever gets slow, and in
`schema.md` as the second thing that breaks at 100x.

## 17. "Due" and "in service" read off ServiceRecord.status, not off with_service_status()

**Session 6.**

**Chose:** The dashboard's `due_vehicles` and `in_service_vehicles` headline numbers come from a
`ServiceRecord` aggregate grouped by status, counting distinct vehicles.
**Rejected:** Reading both off `VehicleQuerySet.with_service_status()`, since the brief calls for
reusing that annotation.

`with_service_status()` (decision #14) only distinguishes OVERDUE from "any other open record" —
BOOKED and IN_SERVICE both fall under its DUE label, because the vehicle-list badge it was built
for never needed to tell them apart. Reusing it for the "due" headline would have silently
double-counted an in-service vehicle as also due. `with_service_status()` is still reused, just for
`overdue_vehicles` only — the one number where its OVERDUE branch is exactly what's needed, and
where the alternative really would be re-deriving the grace-period comparison.

Verified with a regression test (`test_in_service_vehicle_is_not_also_counted_as_due`) after an
earlier draft caught exactly this double-count against fixture data.

## 18. A protected endpoint instead of a Render scheduled job

**Session 6.**

**Chose:** `POST /internal/check-due-vehicles/`, authenticated by a `DUE_CHECK_TOKEN` shared
secret, meant to be called by an external free scheduler.
**Rejected:** Render's native Cron Jobs feature, and accepting the gap.

`ensure_due_record` fires on odometer update and service completion, which covers the mileage
threshold fully. The date threshold never fires on its own, so a vehicle sitting untouched past its
date interval was never flagged — leaving goal 4 half-met, and specifically the half the brief's
opening scenario describes.

Checked directly: Render's Cron Jobs have no free tier — billed per-minute from a $1/mo minimum,
separate from the free web-service plan this app runs on (decision #3's Neon-not-Render-Postgres
reasoning was cost-driven for the same underlying reason). Since `check_due_vehicles` already
existed as a management command with nothing to trigger it on a timer (decision from session 4,
recorded there as a known gap), the fix is an endpoint an outside scheduler can hit instead of a
Render-native feature this plan doesn't have.

The endpoint reuses `ensure_due_record()` via a new shared `sweep_due_vehicles()` (factored out of
the management command's own loop, not duplicated) rather than reimplementing the sweep — so the
scheduling mechanism can be swapped for a real cron on a paid tier without touching the logic. Token
comparison uses `constant_time_compare`, and an unset token 403s unconditionally — "not configured"
never quietly means "not checked."

This is a workaround for a hosting constraint, not a design preference — I would rather have the gap
closed with an ugly mechanism than left open with a note about it, and on a paid tier this would be a
scheduled management command instead.

## 19. Seeding demo history by driving the real state machine with time patched, not by hand-building rows

**Session 6.**

**Chose:** `seed_demo`'s historical completions run `book_service` / `start_service` /
`complete_service` for real, with `django.utils.timezone.now` patched to a backdated instant for
the duration of each one.
**Rejected:** Directly constructing `ServiceRecord` rows in a COMPLETED state and hand-writing
matching `TimelineEvent` rows to look right.

The brief wants a completed seed record to have its full CREATED/BOOKED/STARTED/COMPLETED timeline,
not a bare row — hand-building that timeline is exactly the kind of second copy of the state
machine's behaviour decision #15 already argues against elsewhere in this codebase, just applied to
seed data instead of a view. Patching `timezone.now()` (not `fleet.services.timezone.now` alone)
means every `auto_now_add`/`auto_now` field touched during the patched block — `TimelineEvent.
created_at` included — lands on the same backdated instant as the completion itself, so a seeded
record's timeline reads as a coherent history rather than "completed 6 weeks ago, according to
events all stamped today."

Idempotency is a registration-number prefix (`FC-DEMO-`), not a fixed set of primary keys:
`handle()` deletes every vehicle under that prefix (and, via CASCADE, their records/timeline/
assignments) before recreating the fleet, verified empirically that Django's deletion Collector for
a CASCADE doesn't route through `TimelineEventQuerySet.delete()`'s override — that guard exists for
the *application's* code paths, not a data-management script's cleanup of rows it owns outright.

## 20. Overlapping dashboard counts, labelled rather than partitioned

**Session 6.**

**Chose:** The four headline numbers overlap. "Vehicles due" includes both
"currently in service" and "overdue" as subsets, since In Service is an open
state and an aged DUE record still has status DUE. Added a caption saying so.
**Rejected:** Making them mutually exclusive.

Goal 8 names these four numbers. Partitioning them would mean "due" silently
excluding overdue vehicles, which reads worse — a manager scanning for what
needs attention wants overdue vehicles counted among the due, not hidden from
that number.

But I misread my own dashboard, which is a reliable sign a reviewer would too.
Rather than change the queries, the row now carries a caption stating that the
counts overlap and that completed services counts records rather than
vehicles.

## 21. Assignment stays at booking, with guidance instead

**Session 6.**

**Chose:** The service record create form takes a description only. Added
helper text explaining that the record is created as Due and that booking is
where a date and technician are set.
**Rejected:** Adding a technician field to the create form.

The create form reads as incomplete — I wondered myself where assignment had
gone. But moving it earlier breaks the lifecycle: goal 4 places assignment at
booking, and a record exists as Due precisely because nothing has been
scheduled yet. That Due state is what the grace period, the overdue rule and
goal 10's alerts are all built on.

It also cannot apply to auto-generated records. When a threshold fires, the
system creates a Due record with nobody available to pick a technician. Putting
the field on the manual form would make the two creation paths behave
differently, which is worse than either.

Fixed the confusion with guidance rather than by moving the field.

## 22. A permission enforced on the endpoint, not on the capability

**Session 6.**

Goal 5 says only a fleet manager can add or remove a technician's assignment.
The assign and unassign views were gated on `FleetManagerRequiredMixin` from
the start, with tests asserting 403 for a technician — including the
self-assignment case, where the submitted technician is the requester
themselves.

The gate was still incomplete. **Booking** creates a `ServiceAssignment` as
part of moving a record `DUE → BOOKED` (goal 4), and the book endpoint was
reachable by an already-assigned technician. So a technician added to a Due
record could book it, create an assignment, and begin work — without ever
touching the endpoint named "assign".

Found by using the app rather than by testing it. The timeline showed
`DUE → BOOKED` performed by `t.alvarez@fleetcare.demo`, which is a manager's
decision appearing under a technician's name.

**The first fix was insufficient.** It permitted a non-manager to book provided
the submitted technician was themselves — self-assignment with extra steps. A
technician booking themselves still creates an assignment row and still makes
the scheduling decision. It also undermines goal 10: the overdue mechanism
depends on records remaining Due until a manager books them, so a technician
who can self-book can clear a manager's alert by deciding to pick up the work.

Booking is now `FleetManagerRequiredMixin`, in every status, for every
technician including themselves. Assigned technicians can still move a booked
record to In Service and Completed — that is their work, and goal 1 restricts
what technicians do to records, not whether they do the work on them.

**The lesson is about where the tests were pointed.** They asserted "a
technician cannot call the assign endpoint" when the requirement is "a
technician cannot cause an assignment." Those are the same sentence only if you
already know every path that assigns, and booking does it as a side effect of a
transition under a name that does not suggest it.

Afterwards I audited every path that can create or delete a `ServiceAssignment`
row: the assign view, the unassign view, booking, and the Django admin. The
first two were always correct, booking is now fixed, and the admin sits outside
the application's role model entirely — `UserManager.create_user()` hard-codes
`is_staff=False`, so no fleet manager or technician account can reach it. Only
a superuser credential provisioned through environment variables can, which is
the same boundary described for timeline immutability in #8.

Decision #16 — routing `book_service` through `assign_technician` so there is
one code path for assignment — made this easier to reason about but did not
prevent it, because the permission lived on the view rather than in the service
function. A check inside `assign_technician` itself would have caught every
caller. That is what I would change with more time.

**Second instance, same session, final review pass:** `ServiceRecordListView`'s
filter dropdowns had the identical shape. Both were populated from unscoped
querysets — a technician's "Technician" and "Vehicle" dropdowns listed every
account and every registration number in the fleet, in controls that could
never have actually returned them an extra row, since the underlying record
list was already scoped to their own assignments. Same pattern: a capability
enforced on its obvious surface (the record list itself, correctly scoped) and
left open on a sibling nobody had thought of as a scoping decision.

Fixed by scoping only the technician's dropdowns — to technicians and vehicles
that share a visible record with them — and leaving the manager's alone.
Scoping both identically has a real cost: a technician with zero assignments
would silently vanish from a manager's dropdown, removing a filter that
answers a real question today ("what is this person working on?" — nothing,
worth being able to see). The two roles are asking different questions of the
same control, so they get different lists.

## 23. Rejecting scheduled dates in the past

**Session 6.**

**Chose:** `book_service` rejects a `scheduled_date` earlier than today, with
today permitted.
**Rejected:** No validation, and requiring a date strictly in the future.

Not specified by the brief — goal 4 only says booking assigns a scheduled date
and a technician. But a booking in the past is meaningless: booking is
scheduling work that has not happened yet, and a past date distorts sorting by
scheduled date and misrepresents the dashboard.

Today is allowed deliberately. A manager booking a van in for this afternoon is
ordinary, and forbidding it would reject the most common same-day case for no
benefit.

Enforced in the service layer, not only on the form, and raising the same
exception type the other transition rules use. A form-only check would leave
`book_service` callable with a past date from anywhere else, and every other
rule about what a transition permits already lives in `services.py`. The form
validation stays so the error appears inline rather than as a page-level
message, but the service layer is the authority.
