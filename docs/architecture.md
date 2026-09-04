# Architecture

Answer each of these, in your own words, once the system has taken real shape.

- What are the moving pieces, and how do they talk to each other?
- Where does each piece run?
- What is the request path for one representative user action, end to end?
- What did you decide *not* to build, and why?

# Architecture

## The moving pieces

Three things run, in two places.

**The application** is a single Django 5.2 project served by gunicorn on a
Render web service. Server-rendered templates, no separate frontend, no
JavaScript. One deployment.

**The database** is PostgreSQL on Neon, a separate provider. The application
reaches it over the public internet using a pooled connection string supplied
as an environment variable.

**An external scheduler** (cron-job.org) posts to a protected endpoint on a
fixed interval, which is how date-based service thresholds get noticed. See
"The scheduling gap" below.

Static files are collected at build time and served by WhiteNoise from the
same process — no CDN, no separate static host.

Nothing else runs. No worker process, no queue, no cache layer, no background
jobs. Every request is handled synchronously by one gunicorn worker.

## How the code is organised

Django apps:

- `accounts` — the custom user model, authentication, and the role-enforcement
  mixins. Deliberately holds no fleet knowledge; the mixins reach fleet models
  through string-based lookups rather than imports.
- `fleet` — everything domain.

Within `fleet`, the split that matters is between views and logic:

- `models.py` — schema, managers, querysets, and the validation that belongs
  on a row.
- `services.py` — the service lifecycle. Transition functions, due-record
  generation, assignment and unassignment. Every function that changes state
  lives here.
- `filters.py` — scoping, search, filtering and sorting for the cross-vehicle
  record list. Shared by the list view and the CSV export so the two cannot
  disagree about what "the current filters" means.
- `csv_io.py` — CSV import parsing, per-row validation, report generation, and
  export row serialisation.
- `dashboard.py` — the aggregate queries behind goal 8.
- `views.py` — thin. Collects the request, calls into the above, handles the
  exception, renders.

The rule is that a view never contains a business rule. Asked where the
lifecycle rules live, the answer is one file.

## The request path: bulk odometer import

The most instructive path through the system, because it touches almost every
layer.

A fleet manager uploads a CSV of registration numbers and odometer readings.

1. **Browser → Render.** POST with the file and a CSRF token, over HTTPS
   terminated at Render's proxy. Django sees the original scheme because
   `SECURE_PROXY_SSL_HEADER` is set.

2. **Middleware.** Session cookie resolved to a user; CSRF token validated.

3. **View.** `FleetManagerRequiredMixin` checks the role and returns 403 for a
   technician — before any file is read. The view then hands the uploaded file
   to `csv_io`.

4. **File-level validation.** Not a CSV, or beyond the row cap: rejected here,
   before the database is touched at all. These are not row-level failures and
   do not appear in the per-row report.

5. **Header detection.** The first row is treated as a header only if its
   cells textually match known column names. (An earlier version inferred this
   from whether the second cell parsed as an integer, which silently swallowed
   a single bad data row — see `ai-prompts.md`.)

6. **Per row, independently.** Each row is validated in order — malformed,
   non-integer or negative reading, duplicate registration earlier in the same
   file, registration not found, vehicle archived, reading lower than the
   vehicle's current one. Each failure produces a `RowResult` carrying the line
   number, the raw row and a distinct reason.

7. **Per-row transaction.** A valid row's update runs inside its own
   `transaction.atomic()`. Deliberately not one transaction for the file: goal
   7 requires valid rows to apply even when others are rejected.

8. **The due check.** A successful update calls `ensure_due_record` for that
   vehicle. If the new reading has crossed `next_due_odometer`, a Due record is
   created with `due_since` set and a CREATED timeline event written. This is
   the mileage half of goal 4, and a bulk upload is its most realistic trigger.

9. **Response.** The view renders the `ImportReport` — totals, and a table of
   every rejected row with its line number and reason.

A single request therefore spans authorisation, file parsing, per-row
validation, thirty independent transactions, threshold evaluation and audit
writes — with no partial-failure mode, because each row succeeds or fails
alone.

## How state changes are made safe

Every state change follows the same shape, in `services.py`:

1. Validate the transition against an explicit `ALLOWED_TRANSITIONS` map.
   Illegal moves raise with a message naming the current status, the attempted
   one, and what is permitted — goal 4 requires the server to explain the
   refusal, not merely refuse.
2. Open `transaction.atomic()`.
3. Make the change.
4. Write the `TimelineEvent` **inside the same transaction**.

Point 4 is the load-bearing one. Django signals were deliberately not used: an
audit trail that can silently miss an entry when a handler fails is not an
audit trail. A test forces the event write to fail and asserts the transition
rolls back with it.

## Derived state

`Vehicle.next_due_date` and `Vehicle.next_due_odometer` are denormalised — both
are derivable from the vehicle's last completed service plus its intervals, and
both are stored anyway.

They are stored because goals 6 and 8 need due-ness filterable, sortable and
countable in SQL. As a Python property, "which vehicles are due" becomes a full
scan and a loop, and `Paginator` cannot produce a total without materialising
every row — which is goal 6's forbidden pattern moved one layer down.

The invariant: exactly one code path writes them — `complete_service()`, in
`services.py`. An odometer update calls `ensure_due_record()`, which reads
these fields to check whether a threshold has just been crossed, but never
writes them: the thresholds are anchored to the last completed service, not
to odometer edits in between, so an edit changes whether a threshold has been
crossed, never where it sits. Neither field appears on any form.

Overdue is the opposite choice: never stored, always derived as
`status == DUE AND due_since + grace period < now`. A stored flag would need
something to maintain it and would go stale the moment nothing ran.

## The scheduling gap

Render's free tier has no worker process and no cron. `ensure_due_record` is
therefore called at the two moments a vehicle's state can change — odometer
update and service completion — which covers the mileage threshold completely.

The date threshold does not fire on its own. A vehicle sitting untouched past
its date interval would never be noticed. Closed with a token-authenticated
POST endpoint that runs the same fleet-wide check, hit on a schedule by an
external cron service, with the token supplied as an environment variable.

This is a workaround for a hosting constraint, not a design preference. On a
paid tier it would be a scheduled management command, which is why
`check_due_vehicles` exists as a command and the endpoint is a thin wrapper
around it rather than a reimplementation.

## Authorisation

Enforced on the server in `accounts/mixins.py`, never in templates. Templates
hide controls a user cannot use, but that is cosmetic — the tests assert 403
responses from the server, since goal 1 requires the difference to hold there.

Four mixins, split into two pairs. Queryset-scoping mixins restrict what a
technician sees; object-level mixins produce 403 rather than 404 on an
unauthorised record or vehicle. Vehicles and records need separate
implementations because scoping records is one M2M hop while scoping vehicles
is a reverse-FK-then-M2M traversal needing `.distinct()` — decision #13.

Authorisation is enforced per capability, not per endpoint name — a lesson
learned the hard way. Booking creates a technician assignment as a side effect
of a status transition, so it needs the same manager-only gate as the endpoint
actually called "assign". Every path that can create or delete a
`ServiceAssignment` row is manager-gated: the assign view, the unassign view,
and booking. See decision #23.

The Django admin sits outside this role model by design. It is gated on
`is_staff`, which `UserManager.create_user()` hard-codes to `False`, so no
fleet manager or technician account can reach it — only a superuser credential
provisioned through environment variables. This is the same boundary as the
timeline's immutability: the application's own rules hold for every code path
within it, and an operator with admin or direct SQL access sits outside them.

## What I decided not to build

**A REST API.** Server-rendered templates only. A mobile client would require
splitting the views into a serialised layer. For one web client that cost is
hypothetical and the saving was immediate.

**A background worker or queue.** Nothing in the ten goals needs asynchronous
work. The CSV import runs synchronously, which is acceptable at the row cap
enforced and would need rethinking at ten times the volume.

**Postgres full-text search.** Goal 6's search is `icontains`, which cannot use
an index. Correct at this scale; a `tsvector` column with a GIN index is the
fix, and was not worth the migration and maintenance cost here.

**A charting library.** Goal 8's eight-week chart is CSS-scaled divs. Pulling
in Chart.js and a CDN dependency for one view was not worth it.

**Any CSS framework or JavaScript.** Decision #11. The assignment scores the
ten goals, and every hour on presentation is an hour not on them.

**Database-level immutability.** The append-only rule on `TimelineEvent` is
enforced in Python — `save()` refuses an update, `delete()` refuses on both
model and queryset, and the admin returns `False` from the change and delete
permissions. No code path in the application can rewrite history, including as
a superuser. But the rule is not a database constraint, so an operator with
direct SQL access still could. Making it a real constraint would be the next
step if this were production.