# Decisions

Log the decisions that actually shaped this codebase — the ones where a real alternative existed and
you picked one. At least five entries. For each: what you chose, what you rejected, and why. At least
one entry must be a decision you later reversed — say what changed your mind. It can be any entry
below, not necessarily the last one; add a **Later reversed:** line to whichever one it is.

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

## 3. django-environ alone, dropping dj-database-url and python-dotenv

**Chose:** `django-environ` for all configuration.
**Rejected:** The conventional trio of `python-dotenv` for `.env` files,
`dj-database-url` for parsing `DATABASE_URL`, and `os.environ` for the rest.

`django-environ` already reads `.env` files, parses a Postgres connection URL
into Django's nested `DATABASES` dict via `env.db()`, and casts booleans and
lists. The other two libraries would have added nothing but two more
dependencies to pin and explain.

Settings fall back to local SQLite when `DATABASE_URL` is unset, so the
project runs with no configuration at all on a fresh clone.

## 4. Neon for Postgres rather than Render's own free database

**Chose:** Web service on Render, database on Neon.
**Rejected:** Both on Render.

Render's free Postgres instances expire 30 days after creation. A reviewer
opening this app five weeks after submission would find the data gone. Neon's
free tier has no such expiry. The cost is a second provider and a connection
string to manage; the benefit is that the live URL still works whenever it
gets clicked.

## 5. Seeding from build.sh — reversed

**Originally chose:** Run `python manage.py seed_users` manually on Render
after the first deploy.
**Reversed to:** Run it as the final step of `build.sh`, on every deploy.

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

## 6. Committing demo credentials to a public repository

**Chose:** Demo passwords in `SUBMISSION.md` and in `seed_users.py`.
**Rejected:** Credentials supplied privately.

The brief requires demo credentials for every role recorded in
`SUBMISSION.md`, so this is a deliberate exception to keeping secrets out of
the repository, not an oversight. These passwords exist only on a demo
deployment holding fictional data and are used nowhere else. Everything with
real consequence — `SECRET_KEY`, the Neon connection string — lives in Render
environment variables and appears in the repo only as named placeholders in
`.env.example`.

## 7. Storing next-due values on the vehicle rather than computing them

**Session 2.**

**Chose:** `next_due_date` and `next_due_odometer` as indexed columns on
`Vehicle`, recalculated whenever a service completes or an odometer reading
changes.
**Rejected:** Computing due-ness on read from the last completed service plus
the intervals.

Computing is simpler and cannot drift. But goal 6 requires server-side
filtering, sorting and pagination with a total count, and goal 8 requires
aggregate counts of vehicles due and overdue. Both need due-ness to exist in
SQL. As a Python property it becomes a full table scan and a loop, and
Django's `Paginator` cannot produce a total without materialising every row —
which is exactly the "load everything into the browser" pattern goal 6
forbids, moved one layer down.

The accepted cost is that these columns can drift from the truth. The
mitigation is confining writes to two code paths in the service layer, stated
as an invariant in the model docstring.

This is the decision I would most expect to be challenged on, and the one I
would revisit first if the app were read-light and write-heavy rather than the
reverse.

## 8. The whole schema in one pass, before any views

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

## 9. Assignment as an explicit through-model

**Session 2.**

**Chose:** A `ServiceAssignment` model with `assigned_at` and `assigned_by`.
**Rejected:** A plain `ManyToManyField` between `ServiceRecord` and users.

A bare M2M gives Django a hidden join table with nowhere to record who
assigned whom or when. Goal 9 requires every assignment and unassignment in
the timeline with an actor, so the metadata has to live somewhere. Declaring
the through-model up front avoids a later migration converting the implicit
table into an explicit one.

## 10. Enforcing immutability in code, not by convention

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

## 11. A separate env-driven admin account rather than promoting the demo manager

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

## 12. Verifying against the right surface

**Sessions 1 and 2.** Not a design decision, but a working practice I changed
after making the same mistake twice.

In session 1, Render reported a successful deploy while `seed_users` had
failed silently, and the only symptom was a login rejecting valid credentials.
In session 2, I loaded the live URL expecting to see changes, saw none, and
assumed the migration had failed — I had committed locally but not pushed, so
Render had nothing new to deploy. Session 2 also shipped no UI at all, so even
after pushing, the front page was never going to look different.

Both are the same error: trusting a proxy signal instead of the thing itself.
A green deploy badge is not a working app, and an unchanged page is not
evidence of an unchanged database. From session 3 onward I verify by querying
Neon or opening the admin, and I develop against a local SQLite server rather
than round-tripping through Render.

## 13. Keeping derived and lifecycle fields off every form

**Session 3.**

**Chose:** Vehicle forms expose registration, make, model, odometer and the two
intervals — never `next_due_date` or `next_due_odometer`. Service record forms
expose description only — never `status`, `due_since`, `scheduled_date`,
`completed_at` or `completed_odometer`.
**Rejected:** Including them and relying on validation, or on managers simply
not editing them.

Decision #7 established that the next-due columns are denormalised and safe
only because exactly two code paths write them. A form field is a third code
path. Leaving these off the form isn't a UI choice, it's how that invariant
survives contact with a UI built two sessions before the service layer that
owns it.

The same reasoning covers the lifecycle fields. Goal 4 requires illegal status
moves to be rejected by the server with an explanation; a form that lets a
manager set `status` directly would route around the state machine entirely
before it's even written.

`status` and `due_since` are set in `form_valid`, not by the user.

## 14. Archive and restore as POST, not GET

**Session 3.**

**Chose:** Archiving and restoring are POST-only views behind a small form with
a button.
**Rejected:** A link to `/vehicles/3/archive/`, which is less code.

A GET request that changes state is wrong in a way that has practical
consequences rather than only theoretical ones: it bypasses CSRF protection, it
can be triggered by anything that prefetches or crawls links, and it's
replayable from browser history. The codebase was already consistent about this
— logout was built as a POST form in session 1 for the same reason, since
Django 4.1 rejects GET logout outright.

## 15. Role gating by action, not by page

**Session 3.**

**Chose:** Both roles can see the vehicle list and detail pages. Only managers
get create, edit, archive and restore. Technicians see records only where they
are assigned.
**Rejected:** Blanket-gating the whole vehicles area to managers.

Goal 1 restricts what technicians can *do* — create vehicles, change intervals,
reassign records — not what they can see of the fleet. A technician who can't
look up a vehicle can't sensibly work on it. Gating per action rather than per
page keeps the rule aligned with the brief rather than with whatever was
simplest to implement.

Enforcement is in the view via the session 2 mixins. Templates also hide
controls the user can't use, but that is cosmetic — the tests assert a 403 from
the server, since goal 1 says the difference must hold there and not only in
the interface.

## 16. 403 rather than redirect on unauthorised access

**Session 3.**

**Chose:** An authenticated user attempting an action their role forbids gets
403 Forbidden.
**Rejected:** Redirecting to the login page or silently back to the list.

Redirecting conflates two different failures. Not being logged in is 401-shaped
and a redirect is the right response. Being logged in as the wrong role is a
permission failure, and bouncing the user to a login form they're already past
is confusing and makes the tests weaker — a redirect assertion doesn't
distinguish "correctly refused" from "quietly did nothing".

## 17. No CSS framework, no JavaScript

**Session 3.**

**Chose:** Plain hand-written CSS, server-rendered forms, no client-side
framework of any kind.
**Rejected:** Bootstrap or Tailwind, and HTMX for partial updates.

The brief is explicit that no stack scores better than another and that time
spent on things outside the ten goals will show. Goals 4, 7, 8 and 10 are the
hard ones and they're all server-side. Styling buys nothing here.

The visible cost is that the app is plain. The accepted risk is that the
dashboard in goal 8 needs a chart, which will require one small JavaScript
dependency — that will be a deliberate, single exception rather than a
framework adopted up front.