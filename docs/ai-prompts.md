# AI prompts

The prompts you actually used, in the order you used them, grouped by what you were trying to achieve. For each significant one: what you asked, what you got back, and what you had to correct.

Include at least one prompt that produced something wrong, and what you did about it.

If you did not use AI at all, say so here, and describe your process instead.

## <What you were trying to achieve>

### Prompt

### What you got

### What you corrected


I used two AI surfaces, doing different jobs.

**Claude (chat)** for thinking: sequencing the work, choosing a stack, weighing
the due-date modelling options, debugging deploys, and deciding what *not* to
build. This is where most of what ended up in `decisions.md` was argued out.

**Claude Code** for implementation: long, prescriptive prompts encoding
decisions already made, each ending with an explicit list of what not to build.

The second only looks the way it does because of the first. The specificity in
the session prompts below — the composite index, the through-model, the
record-keyed dismissal — is the output of the chat conversations above them,
not something the coding model proposed.

Prompts are in the order I used them, grouped by what I was trying to do.

---

## Understanding the brief

**Chat.** Pasted the assignment brief and asked it to explain what the task was
actually asking for, before planning anything.

Useful output: it identified the dual date/mileage interval as the structural
centre of the whole brief — nearly every other goal hangs off it — and flagged
that the docs and git history carry a large share of the score rather than the
app itself. Both changed how I approached the work.

---

## Planning the build order

**Chat:**

> ok first tell me the proposed pipeline i should follow and how i should start
> building the app

Then, after choosing Python:

> [selected: Python (Django or FastAPI) / session-by-session task breakdown]

Three things from this I took directly:

1. **Decide how "due" is computed before writing any code.** Stored on the
   vehicle versus computed on read is the single decision the rest of the app
   hangs off, and getting it wrong means rewriting the list view, the dashboard
   and the alerts. See `decisions.md` #7.
2. **Deploy a skeleton on day one, not day six.** Hosting is where a submission
   dies quietly, and free-tier problems are slow to diagnose. This turned out
   to be correct — session 1 lost more time to deployment than to code.
3. **Design the whole schema in one pass.** Growing it feature by feature means
   a migration per feature, each reshaping tables that already hold data.

It also argued for Django over FastAPI, and for server-rendered templates over
a separate SPA, on the grounds that goal 6 requires server-side
search/filter/pagination and templates make that structurally true rather than
a claim to defend. I agreed and it became `decisions.md` #1.

---

## Session 1 — project scaffold

**Chat:**

> ok so give me the prompt for claude code to complete session 1

**Claude Code** (the generated prompt, used as-is):

> I'm building a fleet maintenance tracking app as a take-home assignment. This
> is session 1 of 6: project foundation and getting a skeleton deployed. Keep
> strictly to the scope below — do not scaffold any domain models beyond what I
> list.
>
> Stack: Django 5, PostgreSQL, server-rendered templates (no separate SPA).
> Deployment target: Render (web service) + Neon (Postgres).
>
> CRITICAL FIRST: Create the custom user model and run the initial migration
> before anything else. Django cannot swap the user model after the first
> migrate without a painful reset.
>
> Build, in this order:
>
> 1. Django project `fleetcare` with an `accounts` app containing a custom user
>    model:
>    - Subclass AbstractBaseUser + PermissionsMixin (not AbstractUser).
>    - Email as USERNAME_FIELD, unique, no username field.
>    - A `role` field using TextChoices with FLEET_MANAGER and TECHNICIAN.
>    - A custom manager with create_user and create_superuser.
>    - Convenience properties `is_fleet_manager` and `is_technician`.
>    - Register in AUTH_USER_MODEL, then makemigrations + migrate.
>
> 2. Settings configured for both local and production:
>    - django-environ or python-dotenv for env vars; dj-database-url for
>      DATABASE_URL.
>    - SECRET_KEY, DEBUG, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS all from env.
>    - WhiteNoise for static files.
>    - LOGIN_URL / LOGIN_REDIRECT_URL / LOGOUT_REDIRECT_URL set.
>    - A .env.example with every variable listed and no real values.
>    - .gitignore covering .env, __pycache__, staticfiles, venv.
>
> 3. Auth screens using Django's built-in LoginView/LogoutView with my custom
>    user, plus a `base.html` template with a nav bar and a message-display
>    block, and a placeholder `/dashboard/` view that requires login and just
>    shows the logged-in user's email and role.
>
> 4. Django admin registration for the user model so I can inspect data.
>
> 5. Deployment files for Render:
>    - requirements.txt (pinned versions)
>    - build.sh running pip install, collectstatic and migrate
>    - gunicorn start command
>    - render.yaml if it makes the setup simpler, otherwise tell me the exact
>      dashboard settings to enter by hand.
>
> 6. A management command `seed_users` creating one fleet manager and one
>    technician with known demo passwords, so I have credentials to test the
>    deployed login with.
>
> 7. Stub files under docs/: architecture.md, schema.md, plan.md, decisions.md,
>    ai-prompts.md — headings and the questions each must answer only, no
>    invented content. Plus a SUBMISSION.md skeleton with sections for the repo
>    URL, live URL, demo credentials per role, and a cold-start note.
>
> Do NOT create: vehicles, service records, assignments, timelines, alerts,
> dashboards or any styling beyond minimal readable CSS. Those are later
> sessions.
>
> Git: initialise the repo and commit at each logical step above with clear
> messages, rather than one commit at the end. My git history is being assessed.
>
> Before you start, give me a short plan and flag anything in the above you
> think is a mistake. As you go, add a one-line comment on any choice that
> isn't obvious — I have to explain this code in an interview.

**Where I deviated from the output.** The prompt offered django-environ *or*
python-dotenv plus dj-database-url. I dropped the latter two: django-environ
already parses `DATABASE_URL` into Django's nested dict and reads `.env` files,
so the other two libraries would have added dependencies without adding
capability. `decisions.md` #3.

---

## Choosing hosts

**Chat:**

> should i use render or vercel

Answer was Render, and the reasoning was the part I kept: Vercel runs
serverless functions, so Django gets no persistent process, a read-only
filesystem, and no straightforward way to run `migrate` or a seed command.
Vercel would only have made sense with a separate React frontend, which I had
already decided against.

Two free-tier facts came out of this that shaped the submission: Render's free
services sleep after 15 minutes with a cold start of roughly a minute (noted in
`SUBMISSION.md` so a slow first load isn't read as a broken deploy), and
Render's own free Postgres expires 30 days after creation — which is why the
database is on Neon instead. `decisions.md` #4.


---

## Session 1 — deployment debugging

**Chat, three problems in sequence:**

> when i open the url it says 400 bad request

Diagnosed as `DisallowedHost` — Render's assigned hostname didn't match the
`ALLOWED_HOSTS` placeholder, and `DEBUG=False` means Django returns a bare 400
with no detail. Fixed by setting the real hostname, and by deriving it from
Render's injected `RENDER_EXTERNAL_HOSTNAME` so it can't go stale.

> ok the app is deployed, now how to log in without signup

> no i created seed_users first and migrate later thus log in failed, thus it
> says invalide credentials

This is the interesting one. **`SELECT email, role FROM accounts_user;` on Neon
returned the table but zero rows** — which ruled out a SQLite fallback and
pointed straight at seeding rather than migrations.

**Claude Code, the fix:**

> Fix a broken build script in this Django project (fleetcare, deployed on
> Render + Neon).
>
> ## The bug
>
> build.sh runs `python manage.py seed_users` BEFORE `python manage.py migrate`.
> On the first Render deploy this meant seed_users ran against a database with
> no accounts_user table, so it failed and created no users. The deploy still
> reported "succeeded" and the site came up, but login fails with invalid
> credentials. Confirmed: `SELECT email, role FROM accounts_user;` on Neon
> returns zero rows, though the table itself exists.
>
> ## What to do
>
> 1. Read build.sh and report back what it currently contains before changing
>    it.
>
> 2. Reorder it so the sequence is:
>    - pip install -r requirements.txt
>    - python manage.py collectstatic --no-input
>    - python manage.py migrate
>    - python manage.py seed_users
>
> 3. Ensure the script starts with `#!/usr/bin/env bash` and `set -o errexit`.
>    If errexit is missing, that is the root cause of the silent failure — a
>    failing command didn't abort the build, so Render reported success on a
>    broken deploy. Add it and say so in the commit message.
>
> 4. Confirm the executable bit is still set on build.sh in git
>    (`git ls-files -s build.sh` should show mode 100755). If it isn't, fix it
>    with `git update-index --chmod=+x build.sh`.
>
> 5. Print the demo credentials that seed_users creates — read them out of
>    accounts/management/commands/seed_users.py — so I can test the login and
>    fill in SUBMISSION.md.
>
> 6. Commit with a message that describes the actual bug and fix, not just
>    "update build.sh". My git history is being assessed and this is a real
>    problem worth showing.
>
> ## Do not
>
> - Change any application code, settings, models or templates.
> - Add vehicles, service records or any domain models — those are session 2.
> - Squash or amend earlier commits.
>
> After committing, tell me what to verify on Render and Neon once the redeploy
> finishes.

**This is the prompt that produced something wrong.** The original session 1
prompt asked for a `build.sh` running "pip install, collectstatic and migrate"
and separately for a `seed_users` command, without specifying that seeding must
run *after* migrations. The generated script put them in the wrong order and
omitted `set -o errexit`, so the failure was swallowed and the deploy reported
success on an app whose login didn't work.

The ordering was the visible bug; the missing `errexit` was the real one. Fixed
both, and the lesson generalised into how I verify — see below.

---

## Session 2 — full schema and role enforcement

**Chat:**

> ok lets move to session 2, give me claude code prompt to implement it and
> then explain me also what all we are doing in session 2, first before all
> this tell me exactly what all we have done till now in session 1

**Claude Code** (used as-is):

> Session 2 of 6 on this Django project (fleetcare). Session 1 built the custom
> user model, auth, and deployment — all live on Render + Neon. Read
> docs/decisions.md and accounts/models.py before starting.
>
> This session: the COMPLETE domain schema in one pass, plus server-side role
> enforcement. No views, no templates, no forms beyond what the admin needs.
>
> Create a new app `fleet`. Models below.
>
> ## Vehicle
>
> - registration_number: CharField, unique, indexed
> - make, model: CharField
> - current_odometer: PositiveIntegerField
> - service_interval_days: PositiveIntegerField
> - service_interval_km: PositiveIntegerField
> - next_due_date: DateField, null=True, indexed
> - next_due_odometer: PositiveIntegerField, null=True, indexed
> - is_archived: BooleanField, default False, indexed
> - created_at / updated_at
>
> next_due_date and next_due_odometer are DELIBERATELY DENORMALISED — derived
> from the last completed service, stored so that "which vehicles are due" is
> an indexed SQL query rather than a Python loop. Goals 6 and 8 need
> server-side filtering, sorting and pagination over due-ness, which is
> impractical if it only exists as a computed property. Add a docstring on the
> model stating this invariant and that only the service-completion and
> odometer-update code paths may write these fields. Do not write that logic
> yet — session 4.
>
> Managers: a default manager excluding archived vehicles, plus an
> `all_objects` manager that includes them. Be careful — Django uses the FIRST
> declared manager for related-object access, so declare deliberately and add a
> comment explaining the choice.
>
> ## ServiceRecord
>
> - vehicle: FK to Vehicle, on_delete=PROTECT, related_name="service_records"
> - description: TextField
> - status: CharField with TextChoices DUE / BOOKED / IN_SERVICE / COMPLETED,
>   indexed
> - due_since: DateTimeField, null=True — when the record entered DUE. Overdue
>   is derived as due_since + grace period < now; do not store an `is_overdue`
>   boolean.
> - scheduled_date: DateField, null=True — set on booking
> - completed_at: DateTimeField, null=True
> - completed_odometer: PositiveIntegerField, null=True
> - created_by: FK to user, on_delete=PROTECT
> - created_at / updated_at
> - technicians: M2M to user THROUGH ServiceAssignment
>
> Add a composite index on (status, due_since) — the alerts query in goal 10
> and the dashboard counts in goal 8 both filter on exactly that pair.
>
> Model-level validation only (no transition logic yet): completed_at and
> completed_odometer must both be set or both null.
>
> ## ServiceAssignment (explicit through-model, not a bare M2M)
>
> - service_record: FK, on_delete=CASCADE
> - technician: FK to user, on_delete=PROTECT
> - assigned_at: auto
> - assigned_by: FK to user, on_delete=PROTECT, related_name="+"
> - unique_together on (service_record, technician)
>
> Explicit because goal 9 needs assigned_at and assigned_by in the timeline,
> and a bare ManyToManyField has nowhere to put them.
>
> ## TimelineEvent — APPEND-ONLY
>
> - service_record: FK, on_delete=CASCADE, related_name="timeline"
> - event_type: TextChoices — CREATED, STATUS_CHANGED, TECHNICIAN_ASSIGNED,
>   TECHNICIAN_UNASSIGNED, NOTE_ADDED
> - actor: FK to user, on_delete=PROTECT, null=True (null = system-generated)
> - old_value / new_value: CharField, blank
> - note: TextField, blank
> - created_at: auto, indexed
>
> Goal 9 says nothing in this timeline can be edited or deleted after the fact,
> including by fleet managers. Enforce it, don't just document it:
> - Override save() to raise if the instance already has a pk.
> - Override delete() on both model and queryset to raise.
> - Register in admin with has_change_permission and has_delete_permission
>   returning False.
>
> ## AlertDismissal
>
> - service_record: FK, on_delete=CASCADE
> - dismissed_by: FK to user, on_delete=PROTECT
> - dismissed_at: auto
> - unique_together on (service_record, dismissed_by)
>
> Keyed to the RECORD, not the vehicle. That is what makes goal 10's
> reappearance rule work with no extra logic: when a vehicle becomes due again
> a new ServiceRecord exists, so no dismissal covers it and the alert returns.
>
> ## Role enforcement — accounts/mixins.py
>
> - FleetManagerRequiredMixin: LoginRequiredMixin + a test on
>   request.user.is_fleet_manager, returning 403 (not a redirect) on failure.
> - TechnicianScopedQuerysetMixin: managers get the full queryset; technicians
>   get only records where they appear in ServiceAssignment.
>
> One place, not scattered if-statements in views. Goal 1 requires this on the
> server.
>
> ## Also
>
> - Register everything in the Django admin so I can inspect data while
>   building later sessions.
> - Tests for: TimelineEvent rejecting update and delete, the archived-vehicle
>   manager split, the completed_at/completed_odometer pairing constraint.
> - makemigrations + migrate, and confirm the migration applies cleanly.
>
> ## Do not
>
> - Write lifecycle/transition logic, due-date calculation, or any service
>   layer. Session 4.
> - Write views, templates, forms or URLs beyond admin registration.
> - Touch accounts/models.py or any deployment file.
>
> ## Git
>
> Commit per logical step — models, then mixins, then admin, then tests — not
> one commit at the end. History is graded.
>
> Before you start, give me a short plan and flag anything above you think is
> wrong. As you go, comment any non-obvious choice; I have to explain this in
> an interview.

**Why it's written that way.** Three deliberate choices. It states the *reason*
for each modelling decision inline, so the generated code carries that
reasoning in comments instead of my reconstructing it three weeks later. It
names the goals each index and constraint serves. And it ends with an explicit
"do not" list, because without one the model scaffolds a plausible-looking
service layer that would then have to be unpicked in session 4.

Asking it to flag anything it thought was wrong before starting is worth doing
on a schema prompt specifically — mistakes here are expensive to migrate away
from.

### Follow-up: admin access

**Claude Code:**

> Small follow-up to session 2. I need access to the Django admin on the
> deployed app so I can inspect the fleet models, but neither seeded demo
> account has is_staff set, so /admin/ rejects them.
>
> Do NOT promote manager@fleetcare.demo to superuser. A fleet manager who is
> also a Django superuser makes the role enforcement impossible to demonstrate
> honestly — the demo manager must stay an ordinary application user.
>
> ## What to change
>
> Extend `accounts/management/commands/seed_users.py` to optionally create a
> separate admin account, driven by environment variables:
>
> - Read DJANGO_ADMIN_EMAIL and DJANGO_ADMIN_PASSWORD from the environment
>   (use the same django-environ instance the settings use, for consistency —
>   check how settings.py reads env vars and match it).
> - If either is unset or empty, skip this step silently and print a short line
>   saying admin seeding was skipped. Local runs and any deploy without those
>   vars must be unaffected.
> - If both are set, get_or_create a user with that email, role FLEET_MANAGER,
>   is_staff=True and is_superuser=True.
> - Set the password only when the user is newly created, so a redeploy does
>   not silently reset a password I may have changed.
> - Keep the whole command idempotent — running it twice in a row must not
>   error and must not duplicate anything. Verify this by actually running it
>   twice locally.
>
> Print a clear summary at the end listing which accounts exist and which were
> created versus already present.
>
> ## Also
>
> - Add DJANGO_ADMIN_EMAIL and DJANGO_ADMIN_PASSWORD to .env.example as empty
>   placeholders, with a one-line comment saying they are optional and that
>   leaving them blank skips admin seeding.
> - Do not put real values in .env.example or anywhere else in the repo.
> - Do not add these credentials to SUBMISSION.md — the demo credentials there
>   are for reviewers; this admin account is mine.
>
> ## Confirm for me
>
> - That the fleet models are registered in the admin and will appear once I
>   can log in (list_display etc. as set in session 2).
> - That TimelineEvent's admin registration still returns False from
>   has_change_permission and has_delete_permission — the append-only rule from
>   goal 9 must hold in the admin too, including for a superuser.
>
> ## Do not
>
> - Change any model, migration, view, template or deployment file.
> - Scaffold anything from session 3 onward.
>
> ## Then
>
> Commit with a message describing why a separate env-driven admin account
> exists rather than promoting a demo user, and tell me exactly what to set in
> Render's Environment tab.

The "do NOT promote the demo manager" instruction is the point of this prompt.
Adding `is_staff` to the existing manager was the one-line fix, and it would
have quietly destroyed the ability to demonstrate that role enforcement works
— a reviewer couldn't tell whether the app's permission checks hold or whether
superuser rights were carrying them. `decisions.md` #11.

### What went wrong this session

Nothing wrong with the generated code. The failure was mine: I loaded the
deployed URL expecting to see session 2's changes, saw an unchanged page, and
assumed the migration had failed. Two things were true at once — I had
committed locally but not pushed, so Render had nothing new to deploy; and
session 2 shipped no UI at all, so the front page would have looked identical
either way. Checking Neon's table list and the admin index settled it in under
a minute.

---


## A pattern I noticed

Two verification failures across two sessions, and they're the same mistake.

Session 1: Render reported a successful deploy while `seed_users` had silently
failed, and the only symptom was a login rejecting valid credentials.

Session 2: an unchanged front page read as a failed migration, when nothing had
been pushed and the session had shipped no UI regardless.

Both times I trusted a proxy signal instead of the thing itself. A green deploy
badge is not a working app; an unchanged page is not evidence of an unchanged
database. From session 3 onward I develop against a local SQLite server for
speed, verify effects by querying Neon or opening the admin, and treat the
Render deploy as a check that the deployment still works rather than as my
development loop.

## Session 3 — first UI, role enforcement wired in

**Chat:**

> ok eveyrthing works, now give all the docs files
>
> [and before that] done and tested, lets move ahead to session 3, give the
> claude prompt for it, explain in simple words what has been done till now and
> what we will be doing in the 3rd session

**Claude Code:**

> Session 3 of 6 on this Django project (fleetcare). Sessions 1 and 2 built the custom user model, auth, deployment, and the complete domain schema with role mixins. Read fleet/models.py, accounts/mixins.py and docs/decisions.md before starting.
> This session: the first real UI. Vehicle CRUD with archive/restore, service record creation and description editing, and role enforcement actually wired into views. Goals 1 (manager/technician split enforced server-side), 2 (vehicles) and 3 (service records).
> Build in vertical slices — model to view to URL to template, one feature working end to end before starting the next. Do not build five half-finished things.

> ## Vehicles

> Class-based views in fleet/views.py:

> - VehicleListView — the default manager, so archived vehicles are excluded.Show registration, make/model, current odometer, both intervals. Visible to BOTH roles (technicians need to see the fleet), but create/edit/archive controls render only for managers.
> - VehicleDetailView — vehicle fields plus its service history, newest first.
  Visible to both roles.
> - VehicleCreateView / VehicleUpdateView — FleetManagerRequiredMixin. A ModelForm; do NOT expose next_due_date or next_due_odometer in the form. Those are derived fields written only by the service layer in session 4, and putting them on a form would break that invariant.
> - Archive and restore — POST-only views, manager-only, flipping is_archived. Not GET: they change state, so a GET would be CSRF-exposed and crawlable. Use a small POST form with a button, matching how logout is already done in base.html.
> - An archived-vehicles list using all_objects filtered to is_archived=True, with a restore action. Manager-only.

> Goal 2 requires archiving to preserve service history — confirm the PROTECT on the FK means an archived vehicle keeps its records, and that the detail page still renders for an archived vehicle.

## Service records

> - ServiceRecordCreateView — FleetManagerRequiredMixin. Created against a vehicle (from the URL, not a dropdown the user could tamper with). Form exposes description only. Set status=DUE, due_since=now, created_by=request.user in form_valid. Do NOT expose status, due_since, scheduled_date, completed_at or completed_odometer on the form — those are lifecycle fields owned by session 4's service layer.
> - ServiceRecordDetailView — visible to managers, and to technicians only if assigned. Show the record plus a placeholder region for the timeline (session 4) — an empty section with a comment, not fake data.
> - ServiceRecordUpdateView — description ONLY. Permitted for managers, and for technicians assigned to that record. Enforce in the view, not the template. Goal 3 is explicit that the assignee can update the work description but not who is assigned.

> There is no assignment UI this session (session 5), so I will create assignments in the admin to test the technician path. Build the permission check properly anyway.

## Role enforcement

> Wire accounts/mixins.py into every view above. If either mixin needs adjusting to fit real usage, change it and say what you changed and why — better now than patching around it later.

> Manager-only actions must return 403 from the server for a technician, not a redirect and not a hidden button. Templates should also hide controls the user cannot use, but that is cosmetic — the server check is the requirement.

## Templates

> Extend base.html. Plain, readable, minimal CSS. Add nav links for Vehicles. Use Django's messages framework for feedback after create/edit/archive. No JavaScript. No CSS framework. Styling is worth nothing here and I would rather spend the budget on goals 4-10.

## Tests

> These matter more than the views — they are how goal 1 is evidenced:

> - Technician GETs the vehicle create/edit/archive views → 403.
> - Technician POSTs to archive → 403, and the vehicle is unchanged.
> - Manager can create, edit, archive and restore a vehicle.
> - Archived vehicle absent from the default list, present in the archived list, and its service records still exist.
> - Technician cannot create a service record → 403.
> - Technician assigned to a record CAN update its description.
> - Technician NOT assigned CANNOT view or update that record → 403.
> - Neither role can submit status or assignment fields through the service record form — confirm they are not in the form's fields at all.

## Do not

> - Write status transition logic, due-date calculation, or any timeline event writing. Session 4.
> - Build the assignment UI, cross-vehicle search, CSV, dashboard or alerts.
> - Touch models.py except where a view genuinely requires a change — and if it does, tell me before doing it.

## Git

> Commit per vertical slice: vehicle list+detail, vehicle create+edit, archive +restore, service record create, service record edit, tests. Not one commit.

> Before starting, give me a short plan and flag anything above you think is wrong — particularly if the mixins from session 2 do not fit the actual view usage. As you go, comment any non-obvious choice; I have to explain this in an interview.

**Why it's written that way.** Two instructions carry most of the weight.

The first is the explicit list of fields that must NOT appear on any form —
the derived next-due columns and every lifecycle field. Left unsaid, a coding
model generates a ModelForm over all editable fields, which is the obvious
thing to do and would have quietly broken the invariant established in session
2 before the service layer that protects it was even written.

The second is "build in vertical slices, do not build five half-finished
things." Session 3 is the largest stretch of plain CRUD in the project and the
easiest place to end up with scaffolding everywhere and nothing working.

The prompt also asks it to say if the session 2 mixins don't fit real view
usage and to change them rather than patch around them — mixins written against
imagined usage rarely survive first contact, and it's cheaper to fix the
abstraction than to accumulate workarounds.


### What I verified myself

Not the passing tests — those were the point of the prompt, so trusting them
alone would repeat the mistake from session 1. Specifically:

- Logged in as the technician and confirmed the create controls are hidden,
  then hit `/vehicles/new/` directly and confirmed a 403 from the server. The
  hidden button is cosmetic; the 403 is goal 1.
- Assigned the technician to a record through the admin, confirmed they could
  edit its description, and confirmed a technician not assigned to a record
  gets a 403 on both view and edit.
- Archived a vehicle, confirmed it left the default list and its service
  records survived.

## Session 4 — lifecycle, due calculation, timeline

**Chat:**

> ok lets move onto session 4 now, give claude code prompt and explain what
> exactly we will be doing in this session

**Claude Code:**

> Session 4 of 6 on this Django project (fleetcare). Sessions 1-3 built auth,
> the full schema, and vehicle/service-record CRUD with role enforcement. Read
> fleet/models.py, fleet/views.py and docs/decisions.md before starting.
>
> This session is the core of the assignment: the service lifecycle state
> machine, due-date calculation, and the immutable timeline. Goals 4 and 9.
>
> ALL business logic goes in a new fleet/services.py. Views collect the
> request, call a service function, handle the exception, render. No transition
> logic in views, no logic in models beyond validation, no Django signals — I
> need to be able to point at one file and say "the rules live here."
>
> ## The state machine
>
> Define ALLOWED_TRANSITIONS explicitly as a dict:
>
>     DUE        -> {BOOKED}
>     BOOKED     -> {IN_SERVICE}
>     IN_SERVICE -> {COMPLETED}
>     COMPLETED  -> {}   # terminal
>
> A custom exception (InvalidTransition or similar) carrying a human-readable
> message naming the current status, the attempted status, and what IS allowed
> from here. Goal 4 requires the server to reject illegal moves "with a message
> explaining why" — a bare 400 does not satisfy that.
>
> Views catch it and surface the message via Django's messages framework, with
> a 4xx status. Do not let it 500.
>
> ## Transition functions
>
> One function per transition, not a generic transition(record, new_status).
> Each has different required arguments and different side effects, and a
> generic one collapses into a chain of if-statements:
>
> - book_service(record, scheduled_date, technician, actor)
>   Goal 4: "Booking assigns a scheduled date and a technician." Both are
>   required. Assign the technician via ServiceAssignment (assigned_by=actor),
>   set scheduled_date, move to BOOKED.
>
> - start_service(record, actor) -> IN_SERVICE
>
> - complete_service(record, completed_odometer, actor)
>   Sets completed_at=now and completed_odometer. Then RESETS BOTH COUNTERS on
>   the vehicle:
>       vehicle.next_due_date = completed_at.date() + service_interval_days
>       vehicle.next_due_odometer = completed_odometer + service_interval_km
>   Also update vehicle.current_odometer if completed_odometer is higher.
>   Reject a completed_odometer lower than vehicle.current_odometer with a
>   clear message — the vehicle cannot have driven backwards.
>
> Every one of these must:
> - Validate the transition is legal before doing anything.
> - Run inside transaction.atomic().
> - Write its TimelineEvent INSIDE that same transaction, not after and not via
>   a signal. If the event write fails the transition must roll back. Goal 9's
>   timeline is worthless if it can silently miss entries.
> - Take an `actor` argument. Timeline events must record who acted.
>
> ## Due-record generation
>
> def ensure_due_record(vehicle) -> ServiceRecord | None
>
> A vehicle is due when EITHER threshold is reached:
>     vehicle.next_due_date <= today
>     OR vehicle.current_odometer >= vehicle.next_due_odometer
> Whichever comes first (goal 4).
>
> - Returns None if the vehicle is archived, or already has an open record
>   (status in DUE/BOOKED/IN_SERVICE) — never create a duplicate.
> - Returns None if both next_due_* are null (no completed service yet, so no
>   baseline). Handle this explicitly and comment it: a brand-new vehicle has
>   never been serviced, and I am treating "never serviced" as not-yet-due
>   rather than immediately-due. If you think that is the wrong call, say so
>   before implementing.
> - On creation: status=DUE, due_since=now, actor=None (system-generated),
>   description auto-generated naming which threshold triggered it.
> - Writes a CREATED timeline event.
>
> Call it from exactly two places:
> 1. When a vehicle's odometer is updated (hook into the existing vehicle
>    update view).
> 2. After complete_service, on the same vehicle.
>
> Plus a management command `check_due_vehicles` that runs it across the whole
> fleet, so the date-based trigger can fire without a vehicle being touched.
> Render's free tier has no scheduler, so this is manual for now — note that
> limitation in a comment.
>
> ## Overdue
>
> Derived, never stored. A record is overdue when:
>     status == DUE AND due_since + SERVICE_GRACE_PERIOD_DAYS < now
>
> Put the grace period in settings, read from env with a sensible default (7
> days). Provide it as BOTH a queryset filter (for goals 8 and 10, which need
> it in SQL) and a property on the model (for templates). The queryset version
> is the one that must exist — a Python property alone cannot be filtered or
> counted.
>
> ## Timeline
>
> - Render the timeline on the service record detail page, chronological,
>   showing event type, old -> new value, actor, and timestamp.
> - Add a "leave a note" form on the record — NOTE_ADDED events, available to
>   managers and to assigned technicians.
> - No edit or delete controls anywhere. The model already refuses; the UI must
>   not suggest otherwise.
>
> ## Views and templates
>
> Add transition action buttons to the service record detail page, showing only
> the transitions legal from the current status and permitted for the current
> user's role. Booking needs a small form (scheduled date + technician);
> completion needs one (odometer reading).
>
> Who may transition: fleet managers, and technicians assigned to that record.
> Enforce with the session 2/3 mixins in the view, not the template.
>
> ## Tests
>
> These are the most important tests in the project:
>
> - Every legal transition succeeds.
> - Every illegal transition raises, with a message naming current and
>   attempted status. Test the interesting ones explicitly: DUE -> COMPLETED,
>   DUE -> IN_SERVICE, COMPLETED -> anything, BOOKED -> DUE.
> - complete_service sets BOTH next_due_date and next_due_odometer correctly
>   from the completion date and odometer, not from today or the vehicle's
>   current reading.
> - complete_service with an odometer lower than current is rejected.
> - book_service without a technician or without a scheduled date is rejected.
> - Every transition writes exactly one timeline event with the right actor,
>   old_value and new_value.
> - If the timeline write fails, the transition rolls back (force a failure and
>   assert the status is unchanged).
> - ensure_due_record fires on the date threshold alone, on the mileage
>   threshold alone, and does not duplicate when an open record exists.
> - ensure_due_record returns None for archived vehicles and for vehicles with
>   null next_due_* values.
> - The overdue queryset filter matches the model property on the same data.
> - A technician not assigned to a record cannot transition it (403).
>
> ## Do not
>
> - Build cross-vehicle search, CSV, the dashboard, or the alerts UI. Sessions
>   5 and 6.
> - Use Django signals for timeline writes.
> - Store an is_overdue boolean.
> - Add a status field to any form.
>
> ## Git
>
> Commit per logical step: the transition table and exception, then each
> transition function, then due-record generation, then the timeline UI, then
> tests. Not one commit.
>
> Before starting, give me a short plan and flag anything above you disagree
> with — especially the "never serviced means not yet due" call and the
> decision to hook due-checks to odometer updates rather than run them on every
> request. As you go, comment any non-obvious choice; I have to explain this in
> an interview.

**Why it's written that way.** The instruction carrying most weight is that all
business logic goes in `fleet/services.py`, with views only collecting the
request, calling a function and handling the exception. Without that, the
transition rules end up spread across four views and there is no single place
to point at when asked where the rules live.

Second: one function per transition rather than a generic
`transition(record, new_status)`. Each transition takes different arguments —
booking needs a date and a technician, completion needs an odometer reading —
and different side effects, so a generic function collapses into a chain of
if-statements on the target status.

Third: timeline writes inside the same `transaction.atomic()` block as the
status change, and explicitly not via Django signals. An audit trail that can
silently miss entries when a signal handler fails is not an audit trail. There
is a test that forces the event write to fail and asserts the transition rolls
back.

The prompt also asks it to argue back on two specific judgement calls — whether
a never-serviced vehicle should count as due, and whether hooking due-checks to
odometer updates rather than every request is acceptable. Both are genuinely
arguable and I wanted the objection before the implementation, not after.

Result: 56 tests passing, up from 13 at the end of session 3.

---

### Working out the model before the follow-up

**Chat.** Before writing the next prompt I described my understanding of the
whole flow back and asked for it to be corrected:

> ok so basically manager assigns a car as due, then a technician has an option
> to work on it and then finally sets it as completed, there the technician
> enters current odometer reading, now that reading is updated on both the
> managers and the technician's screen, now a manager can create a new car,
> entering the name reg num current odometer reading and interval of distance
> and days. if any of it is hit then it becomes due, so basically if i put
> tmmw's date then it will automatically become due? and to what technician
> will it be assigned? also there is an admin page who can see when next the car
> will be due this is not visible to either the manager or the technician

Three corrections came out of this, and each changed something:

1. **A Due record is unassigned.** Booking is where a technician gets attached,
   and only a manager can book. Goal 5 says only a fleet manager can add or
   remove an assignment, so auto-assignment would contradict the brief.
2. **Setting tomorrow's date does not make a vehicle due**, for two separate
   reasons — a new vehicle has null next-due values so no baseline exists, and
   nothing checks the date unless something else touches the vehicle. That
   second one is a real gap in goal 4, now recorded in `plan.md` and being
   closed in session 6.
3. **The next-due fields were visible only in the Django admin**, which is an
   inspection tool and not part of the app. A fleet manager whose job is
   knowing what is due could not see it anywhere in the actual interface.

Worth recording that I found the gap in goal 4 by explaining the system back
rather than by testing it.

**Chat, on visibility and scoping:**

> ok before sending this why are technicians seeing all the cars wont it be
> better if they only see the cars assigned to them and also why show them next
> due that also should only be seen by manager, the technician should only see u
> have this car due and complete it by this date, correct me if im wrong

The answer argued for keeping vehicles visible to both roles — goal 1 restricts
what technicians *do*, and goal 5's cross-vehicle "my records" list is the
proper fix for a technician wading through the fleet. I disagreed after
re-reading the brief:

> it says technician can only see service records assigned to them, so lets do
> this only unless its in the future session make that change now i want tech to
> only see the cars assigned to it also the change u just suggested of showing
> the due date that too give all of that in a claude code prompt and if any of
> this is in future session then skip it for now lets follow session wise tasks
> strictly

"Can only see and update service records assigned to them" reads more naturally
as scoping the surrounding context too. That reversal is decision #18.

---

### Follow-up: technician scoping and next-due visibility

**Claude Code:**

> Two changes to existing work on this Django project (fleetcare). Both modify
> sessions 3 and 4; nothing here is from session 5 or 6. Read fleet/views.py,
> fleet/models.py, accounts/mixins.py and docs/decisions.md first.
>
> ## Change 1 — scope the vehicle list to technicians
>
> Currently both roles see the whole fleet (decision #15). Tightening this:
> goal 1 says technicians "can only see and update service records assigned to
> them", and I am extending that to the vehicles behind those records.
>
> New rule:
> - Fleet managers see the whole fleet, unchanged.
> - Technicians see only vehicles they have at least one ServiceAssignment
>   against — any record on that vehicle, in any status, including completed
>   ones. Past work is still their work.
> - A technician opening a vehicle they have no assignment on gets 403, not a
>   redirect and not an empty page.
>
> Implement this in the existing TechnicianScopedQuerysetMixin if it fits, or
> as a sibling mixin if vehicle scoping and record scoping are different enough
> that forcing them together makes both worse. Tell me which you chose and why
> — I do not want one mixin with a branch inside it pretending to be a single
> abstraction.
>
> Do NOT scope by "vehicles with an open assigned record". A technician who
> completed a service last month should still be able to look the vehicle up.
>
> Update the archived-vehicles list the same way. It is manager-only already,
> so it should be unaffected — confirm that.
>
> ## Change 2 — surface next-due on the vehicle views
>
> next_due_date and next_due_odometer are populated by session 4's
> complete_service but appear nowhere except the Django admin, which is my
> inspection tool and not part of the app.
>
> Add to the vehicle LIST:
> - Next due (date) — next_due_date, or "—" when null
> - Next due (odometer) — next_due_odometer, or "—" when null
> - Service status, one of:
>     OVERDUE            — open DUE record whose due_since + grace period < now
>     DUE                — open record in DUE / BOOKED / IN_SERVICE
>     NOT YET SERVICED   — both next_due_* are null
>     OK                 — otherwise
>   Keep NOT YET SERVICED distinct from OK. A vehicle with no service history
>   is a meaningfully different state and collapsing them hides it.
>
> Add to the vehicle DETAIL page: the same three values, plus the current open
> service record if there is one, linked.
>
> ## Performance — this matters
>
> Computing the status label per vehicle in the template gives an N+1 query.
> Annotate it on the queryset instead, reusing the overdue filter that already
> exists in the service layer from session 4. Do NOT reimplement the grace
> period comparison in a second place — it currently exists as a queryset
> filter and a model property, and a third copy is how those drift apart.
>
> Put the annotation on a Vehicle queryset or manager method, not inline in the
> view. The dashboard in session 6 needs the same thing.
>
> Before writing it, tell me how you plan to do the annotation so I can check
> it reuses the existing filter.
>
> ## Tests
>
> - Technician sees only vehicles they have an assignment against.
> - Technician with a COMPLETED assignment still sees that vehicle.
> - Technician gets 403 on a vehicle detail page they have no assignment for.
> - Manager still sees everything, archived list included.
> - The status label is correct for each of the four states.
> - assertNumQueries on the vehicle list does not grow with vehicle count (test
>   with 3 vehicles, then 10).
>
> ## Do not
>
> - Build the technician's cross-vehicle "my records" list. That is goal 5,
>   session 5.
> - Build sorting or filtering on the new columns. That is goal 6, session 5.
> - Add next_due_date or next_due_odometer to any form — they are derived and
>   written only by the service layer (decision #13).
> - Add any CSS framework or JavaScript.
>
> ## Git
>
> Separate commits: the scoping change with its tests, then the next-due
> columns with theirs. They are unrelated changes and should read that way in
> the history.

### Where the tool corrected me

Two things, both worth recording.

**Numbering that did not exist.** The prompt cites "decision #15" and
"decision #13". Before starting, Claude Code checked `docs/decisions.md` and
its git history, found exactly six numbered entries unchanged since session 2,
and flagged that neither referenced number existed in the file. It was right —
I had drafted those entries and not committed them. It proceeded on the
substance, having located both concepts in code comments instead (the
both-roles-see-vehicles behaviour in `VehicleListView`'s docstring, the
forms-exclusion reasoning in `VehicleForm`'s comment). Fixed by committing the
missing entries before continuing.

Worth noting that this only surfaced because the prompt told it to read
`docs/decisions.md` first. A prompt that just described the change would have
implemented it and never noticed my documentation was eleven entries behind
my code.

**A better design than the one I specified.** The prompt offered one mixin "if
it fits, or a sibling if forcing them together makes both worse." It chose
siblings, with the reasoning: scoping service records is `.filter(technicians
=user)`, one direct M2M hop on the queryset's own model; scoping vehicles is
`.filter(service_records__technicians=user)`, a reverse-FK-then-M2M traversal
that repeats a vehicle once per matching record and therefore needs
`.distinct()`, which the record version never has to think about. Cramming
"how many hops, and do I need distinct" into one class is a branch pretending
to be an abstraction.

It also caught a gap I had not seen. I asked for 403 on an unauthorised
vehicle, but a queryset pre-filtered to "vehicles I can see" turns that into a
plain 404 via `get_object()`. The object-level check has to be separate from
the queryset filter. Two new mixins resulted:
`VehicleTechnicianScopedQuerysetMixin` and
`VehicleManagerOrAssignedTechnicianMixin`.

For the annotation it proposed `VehicleQuerySet.with_service_status()`, using
two correlated `Exists()` subqueries on `OuterRef("pk")` — one built from the
existing `ServiceRecord.objects.overdue()` with a vehicle filter added rather
than re-deriving the grace-period maths, the other checking for any
open-status record — feeding a `Case`/`When` into the four-way label.
Subqueries rather than joins, so the query count stays flat regardless of
fleet size and `.distinct()` from the technician filter cannot interfere,
since the annotation is computed per vehicle pk rather than per joined row.

Both improvements came from asking for the plan before the implementation.
That is now standing practice on anything touching a shared abstraction.

### What went wrong

Not the generated code. I spent a while convinced `complete_service` was not
writing back to the vehicle, because `manage.py shell` reported zero vehicles
and `Vehicle.objects.get(registration_number="2411")` raised `DoesNotExist`.
The data was in Neon, created through the deployed app, while the shell was
reading the local SQLite fallback. One line settled it:

    print(settings.DATABASES["default"]["ENGINE"])
    django.db.backends.sqlite3

Third instance of the failure mode recorded in decision #12 — reading a proxy
signal as if it were the thing itself. Sessions 5 and 6 are developed entirely
locally so the browser, the shell and the tests all read the same database.

## Session 5 — assignment, cross-vehicle search, CSV import and export

Goals 5, 6 and 7 in one session. Too much for one sitting — see the end.

### A bug report that wasn't a bug

A technician couldn't see a record I'd booked and assigned to them. Rather
than describe the symptom and ask for a fix, I wrote the prompt to diagnose
first, enumerating four possible causes and explicitly permitting the
conclusion that none applied:

**Claude Code:**

> Bug report — technician cannot see a service record assigned to them.
>
> As a fleet manager: created a vehicle, created a service record on it,
> booked it and assigned a technician. Then logged in as that technician —
> the record does not appear for them. I have not isolated whether this is a
> scoping failure, a 403, or the assignment row never being created, so
> diagnose before fixing.
>
> My data is on Neon (the deployed app), not local SQLite, so `manage.py
> shell` against the local fallback shows nothing. Reproduce it with a test
> rather than by inspecting my data.
>
> ## Diagnose first, in this order
>
> 1. Write a failing test reproducing it end to end: manager creates a vehicle
>    and record, calls book_service with a technician, then the technician
>    client requests the vehicle detail and the record detail. Run it and tell
>    me which assertion fails before changing any code.
> 2. If book_service is not creating a ServiceAssignment row, that is the bug —
>    the technician has no assignment, so every scoping mixin correctly
>    excludes them.
> 3. If the assignment exists but the technician gets 403 or an empty list, the
>    bug is in last session's mixins. The vehicle-side traversal is
>    `service_records__technicians=user` with `.distinct()` — verify it matches
>    the actual related_name and M2M path.
> 4. If the record is reachable directly but simply not listed anywhere, that
>    is NOT a bug — the technician's cross-vehicle record list is goal 5, which
>    I am building next. Say so and stop; do not build it here.
>
> Tell me which of the four it is before fixing.
>
> ## Do not
>
> - Build anything from session 5. I am running that as a separate prompt next.
> - Change the scoping rule itself (decision #18). If that rule is producing
>   the wrong result, tell me rather than changing it.

**Result: category 4.** Nothing was broken — there was simply no view
surfacing a technician's work across vehicles yet.

Step 4 is why that came back honestly. A coding agent asked to fix a bug will
find something to change; naming "nothing is wrong" as a permitted conclusion
is what prevented an invented fix. Fourth instance of decision #12.

Separately, I asked it to justify a session 4 commit I couldn't account for
(`e30ada6`). It held up: `book_service` used `.create()` against a
`unique_together`, so booking a record whose technician was already assigned
raised `IntegrityError` and surfaced as a 500. Fixed with `get_or_create`.

### The session 5 prompt

Structure was left open deliberately — the prompt asked for a plan first on
where the CSV logic should live and how the per-row report should be shaped,
since those are cheapest to correct before implementation.

**Claude Code:**

> [PASTE THE FULL SESSION 5 PROMPT HERE — it's in the chat above, unchanged.]

### What the plan step produced

Three improvements on what I'd specified, all accepted:

- **Three modules, not one.** CSV parsing is an IO concern with no overlap with
  lifecycle rules. Separately, a shared `filters.py` between the list view and
  the export means goal 7's "respect the active filters" can't drift from what
  goal 6 actually applied. Decision #22.
- **Archived as its own rejection reason**, via `Vehicle.all_objects` — a
  manager uploading readings for a van archived last week should be told that,
  not told it doesn't exist.
- **First-occurrence-wins on duplicate registrations.** Processing both means
  the later row silently overwrites the earlier; rejecting both penalises a
  valid row for a later mistake.

### Where I corrected it

**Silent sort fallback.** It proposed swallowing an unrecognised sort
parameter. Inconsistent with a codebase where illegal transitions were built
to explain themselves. Fall back, but say so. Decision #24.

**Suppressing the assignment event on booking.** It proposed a
`write_event=False` flag so an existing single-event test would keep passing.
Goal 9 requires every assignment in the timeline. The test encoded an
assumption from before assignment was a first-class action, so the test
changed instead. Decision #23.

The principle: changing a test because requirements grew is legitimate.
Changing behaviour to keep a test green inverts which is authoritative.

### The bug worth reading: header detection swallowing a row

Found by its own tests before I ran the import. I asked for specifics rather
than accepting "caught and fixed":

**Claude Code:**

> Question about commit 9ca29d2 (bulk odometer CSV import), not a request to
> change anything. You mentioned catching a header-detection bug — "a lone bad
> row was being misread as a header." I'm writing it up and need specifics:
> what the original logic did and what input it got wrong; what the
> user-visible consequence was; what the fix was; which test demonstrates it;
> and whether you found it yourself while writing tests or it came out of an
> existing test failing. Do not change any code.

The original heuristic decided "is row 1 a header?" by whether its second cell
failed to parse as an integer — true for a header, but equally true for a bad
data row. So a one-line file `CSV-1,not-a-number` was classified as a header
and skipped.

The consequence was worse than a mis-parse. The row didn't produce a wrong
rejection; it vanished. The report returned `total_rows=0, succeeded=0,
rejected=0` — silent data loss presented as "nothing to report", which is
exactly what goal 7's per-row report exists to prevent.

Fixed by matching against a fixed vocabulary of known column names on either
cell instead of inferring from a parse failure. A bad data row now flows into
normal validation and returns a proper rejection.

It surfaced from `test_non_integer_reading_rejected` — one of the "test every
rejection reason separately" tests the prompt required — failing on first run
with `AssertionError: 0 != 1`. A named regression test,
`test_single_bad_row_is_not_mistaken_for_a_header`, was added alongside.

What made it findable: requiring header-absence handling and each rejection
reason tested in isolation. One combined test with a well-formed header would
never have hit it.

### What went wrong, and what I verified

**Scope.** Three goals, two new modules, five commits, 56 tests to 127. Should
have been split across two sittings; the clearest estimation error in this
project.

**Verification.** Exercised the import by hand against the deployed app with a
file built to fail four ways — unknown registration, duplicate row, text where
an integer belongs, reading lower than current — alongside three valid rows.
Four distinct reasons with correct line numbers, three rows applied. Also
confirmed a headerless file parses and a non-CSV upload is rejected cleanly.

## Session 6 — dashboard, alerts, scheduling, seed data

**Claude Code:**

> Session 6 of 6 — the final session on this Django project (fleetcare).
> Sessions 1-5 built auth, the schema, vehicle and record CRUD, the full
> service lifecycle with an immutable timeline, assignment, server-side
> search/filter/pagination, and CSV import/export. 127 tests passing.
>
> This session: goals 8 (dashboard) and 10 (overdue alerts), closing a known
> gap in goal 4, and seed data.
>
> ## Goal 8 — dashboard
>
> The manager's landing view. Four headline numbers: vehicles due for service,
> vehicles currently in service, services completed this week, vehicles overdue
> for service. Plus a breakdown of records by status, a breakdown by
> technician, and a chart of services completed per week over the last eight
> weeks.
>
> Every number must come from SQL aggregation. No Python loops over querysets,
> no counting in the template. Reuse the existing
> `VehicleQuerySet.with_service_status()` annotation and the
> `ServiceRecord.objects.overdue()` filter — do not reimplement the grace
> period comparison anywhere.
>
> Use `TruncWeek` for the eight-week series, and make sure weeks with zero
> completions still appear as zero rather than being absent. A gap in the
> x-axis is a bug.
>
> Aim for a small number of queries total, not one per widget. Add an
> `assertNumQueries` test so the dashboard cannot silently grow query count as
> someone adds a widget later.
>
> **The chart: no JavaScript.** Decision #17 says no JS and no CSS framework,
> and that stands. Draw the eight-week series as CSS-scaled bars — a div per
> week with a height percentage relative to the maximum, labelled with the week
> and the count. No Chart.js, no CDN, no canvas. If you think this is the wrong
> call, say so before implementing rather than after.
>
> Technicians should not see the fleet-wide dashboard. Their landing page stays
> the cross-vehicle record list from session 5. A technician requesting the
> dashboard URL gets 403.
>
> ## Goal 10 — overdue alerts
>
> - An alerts area listing every overdue record — status DUE with
>   `due_since + grace period < now` — using the existing overdue filter.
> - A count badge in the nav, on every page, via a context processor. It must
>   not add a query on pages that do not display it, so keep it lazy.
> - A fleet manager can dismiss an alert. `AlertDismissal` already exists,
>   keyed to the service record (session 2) — dismissing writes a row and the
>   alert drops off the list and out of the badge count.
> - Because dismissal is keyed to the RECORD, goal 10's reappearance rule needs
>   no extra logic: when the vehicle becomes due again a new ServiceRecord
>   exists, no dismissal references it, and the alert returns. Write a test
>   proving this end to end — dismiss, complete the service, advance the
>   vehicle past its next threshold, confirm a new alert appears.
> - Manager-only. A technician gets 403 on dismiss.
> - Dismissal is a state change, so POST only, consistent with
>   archive/restore.
>
> ## Closing the goal 4 gap
>
> `ensure_due_record` currently runs on odometer update and service completion,
> which covers the mileage threshold. The date threshold only fires if
> something else touches the vehicle, so a vehicle sitting untouched past its
> date interval is never flagged. `check_due_vehicles` exists but nothing runs
> it.
>
> Check whether Render's free tier supports scheduled jobs. If it does, add the
> configuration and tell me exactly what to set in the dashboard. If it does
> not, implement a protected endpoint that runs the same check — authenticated
> by a secret token from an environment variable, not open to the world — so an
> external scheduler can hit it. Rate-limit or guard it sensibly.
>
> Either way, document the approach and its limitations in a comment and tell
> me what to configure.
>
> ## Seed data
>
> A `seed_demo` management command producing a fleet that demonstrates the
> system rather than an empty shell:
>
> - ~30 vehicles, realistic vans and trucks, varied intervals and readings.
> - A spread of states: some OK, some due, some overdue, some booked, some in
>   service, a few archived, at least one never-serviced.
> - Eight weeks of completed history so the chart has a real shape.
> - Several technicians, with records across multiple vehicles so the
>   by-technician breakdown is meaningful.
> - Timeline events consistent with that history — a completed record should
>   have its created/booked/started/completed events, not a bare row.
> - Idempotent, and it must CLEAR existing demo data first so running it twice
>   gives the same fleet.
> - Must not delete or alter the accounts seeded by `seed_users`.
>
> Do NOT wire this into build.sh — I want to run it deliberately, not on every
> deploy. Tell me how to run it against the deployed database.
>
> ## Tests
>
> - Each of the four headline numbers is correct against known fixture data.
> - The eight-week series includes zero-completion weeks.
> - assertNumQueries on the dashboard.
> - Technician gets 403 on the dashboard.
> - The alerts list matches the overdue queryset filter exactly.
> - Dismissing removes a record from both the list and the badge count.
> - The reappearance rule: dismiss, complete, cross the next threshold, new
>   alert appears.
> - Technician gets 403 on dismiss.
> - The badge count is correct for a manager and appropriate for a technician.
> - seed_demo is idempotent and leaves seeded users intact.
>
> ## Do not
>
> - Add JavaScript or a CSS framework.
> - Reimplement the overdue or due-status logic — reuse what exists.
> - Change any existing model, service function or filter unless a genuine bug
>   requires it, and tell me before doing so.
>
> ## Git
>
> Commit per feature: dashboard, alerts + badge, the scheduled due check, seed
> data. Each with its tests.
>
> Before starting, give me a short plan — particularly how you intend to keep
> the dashboard's query count low, and what you find regarding Render's
> scheduled job support.

The instruction worth highlighting is the chart. Goal 8 wants one and decision
#17 forbids JavaScript; that tension needed resolving in the prompt rather than
being discovered mid-implementation. Also specified: reuse the existing
`with_service_status()` annotation and `overdue()` filter rather than
reimplementing the grace period a fourth time, and an `assertNumQueries` test so
the dashboard cannot silently grow its query count as widgets get added. It came
in at five queries total.

### What it flagged back

**Render's free tier has no cron.** Confirmed by searching rather than assumed,
and it built a token-authenticated endpoint instead. Decision #29.

**The dashboard counts overlap.** Raised unprompted: "due" includes overdue
vehicles as a subset, so due + overdue is not a clean partition. I then misread
the row myself when I looked at the seeded data, which settled it — if the
person who built it misreads it, it needs a caption. Decision #26.

### Running the seed against production

Render's free tier has no shell, so `seed_demo` had to run from my machine with
`DATABASE_URL` pointed at Neon. Rather than ask for commands, I asked for an
explanation first:

**Claude Code:**

> I need to run the seed_demo management command against the deployed Neon
> database. Render's free tier has no shell access, so this has to run from my
> local machine with DATABASE_URL pointed at Neon.
>
> Before anything else, walk me through it rather than doing it — I want to
> understand what I'm running against production data.
>
> ## Tell me
>
> 1. The exact steps to run seed_demo against Neon from my Windows machine
>    (PowerShell). I would rather set DATABASE_URL as a temporary environment
>    variable for one command than edit my .env, so I cannot forget to undo it
>    — tell me the PowerShell syntax for that, or tell me if editing .env is
>    safer and why.
>
> 2. Exactly what seed_demo deletes before it inserts. Be specific about how it
>    identifies demo data by prefix, and confirm what it will NOT touch. I have
>    real accounts on that database — manager@fleetcare.demo,
>    tech@fleetcare.demo, and my admin account — and they must survive.
>
> 3. Whether any data currently on Neon that was NOT created by seed_demo will
>    be removed. I have leftover test vehicles from earlier sessions. I want
>    those gone, but tell me whether seed_demo removes them or whether they
>    survive and I need to delete them separately.
>
> 4. Whether it is safe to run twice, and what happens if it is interrupted
>    partway.
>
> 5. Roughly how long it will take and whether the row volume risks hitting
>    Neon's free tier limits.
>
> ## Then, after I confirm
>
> Give me the commands to run, and a verification query I can paste into Neon's
> SQL editor afterwards to confirm the fleet is there, the counts look right,
> and the demo user accounts are intact.
>
> Do not run anything against my production database yourself and do not modify
> seed_demo unless I ask. Explanation only for now.

The answers that mattered: `_clear_previous()` deletes only vehicles whose
registration starts with `FC-DEMO-`, via `all_objects` so archived ones are
included, with records, timeline events, assignments and dismissals following
by cascade. No line in the file touches a user account. Everything from the
clear through the last write is inside one `transaction.atomic()`, so an
interruption rolls back rather than half-seeding. It also recommended a
session-scoped PowerShell variable over editing `.env`, on the grounds that a
file persists across sessions and a variable dies with the window.

It declined to guess, too: my leftover test vehicles do not match the prefix and
survive, and it said so rather than inventing a broader pattern and deleting
against production on its own initiative.

### Two problems I found by using the app, not by testing it

**A 403 on the root URL.** Session 5 made the login redirect role-aware, but `/`
still pointed everyone at the dashboard, which session 6 made manager-only. A
technician with an existing session hitting the bare URL got a 403 — exactly
what a reviewer would do after logging in with the technician credentials. It
only surfaced because incognito worked and my normal browser did not, which
isolated it to the session cookie.

**A create form that did not explain itself.** The service record form asks only
for a description, and I wondered where technician assignment had gone. It
belongs at booking, and moving it earlier would break the lifecycle — decision
#28. Fixed with helper text rather than by moving the field.

Neither was caught by 162 passing tests, because both are questions about
whether the app makes sense to use rather than whether it behaves correctly.

### A deployment failure with nothing wrong

One deploy failed after 15 minutes with a successful build, gunicorn listening
on the right port, and a worker booted — Render reported "no open ports
detected" and timed out. Redeploying the identical commit worked. The health
check had received a 302 from `/`, which redirects unauthenticated visitors to
login; whether that contributed or it was simply transient, I could not
determine. Worth recording because the instinct on a failed deploy is to change
something, and the correct action here was to read the log carefully enough to
see that the server had started fine, then retry.