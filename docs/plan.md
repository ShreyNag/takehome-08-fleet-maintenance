# Plan

Answer each of these, in your own words.

- How did you break the work into sessions?
- What order did you build in, and why that order?
- What did you estimate versus what it actually took?
- What did you cut when you ran short?


## How I split the work

Six sessions of roughly two hours, ordered so that anything expensive to
change later gets settled early and anything cheap to cut sits at the end.

1. **Foundation and deployment** — project scaffold, custom user model, auth,
   live on Render + Neon.
2. **Schema and roles** — every model in one pass, server-side role
   enforcement.
3. **Vehicles and service records** — CRUD, archive/restore, vehicle history.
4. **Service lifecycle and timeline** — state machine, interval reset on
   completion, immutable audit trail.
5. **Search, assignment, CSV** — server-side filtering and pagination, bulk
   odometer import with per-row reporting, history export.
6. **Dashboard, alerts, seed data, docs** — aggregate views, overdue alerts,
   demo data.

## Why this order

Two things drove it.

**Deploy first, not last.** Hosting is where a submission dies silently, and
free-tier problems are slow to diagnose. Getting a trivial app live on day one
meant every later session pushed through a pipeline already known to work.

**The expensive-to-reverse things go early.** Django cannot swap its user
model after the first migration without resetting the database, so the custom
user model was the first thing built. The full schema lands in session 2 for
the same reason — repeatedly migrating a half-designed schema costs more than
designing it once.

The read-only views (dashboard, alerts) sit last deliberately. They are
aggregates over data the earlier sessions produce, which makes them the
cheapest things to trim if time runs short.

## Estimated vs actual

### Session 1 — Foundation and deployment
- **Estimated:** 2h
- **Actual:** 2h 45m
- **Where it went:** The custom user model took longer than planned.
  Subclassing `AbstractBaseUser` rather than `AbstractUser` means writing the
  manager by hand, but it avoids a username field the app has no use for.
  Deployment cost more than the code: a `DisallowedHost` 400 because Render's
  assigned hostname didn't match the placeholder in `render.yaml`, then a
  build-order bug where `seed_users` ran before `migrate` and silently
  created nothing. Both are written up in `decisions.md`.
- **Cut or deferred:** Nothing cut. Styling deliberately left at bare
  minimum — visual polish is worth almost nothing here and can be done last
  if time allows.

  ### Session 2 — Schema and role enforcement
- **Estimated:** 2h
- **Actual:** 1h 15m
- **Where it went:** Modelling itself was quick because the ten goals were
  read together rather than one at a time — the timeline's need for an actor
  is what forced the explicit assignment through-model, and that only becomes
  obvious when both are designed at once. Time went instead on two things that
  were not code: no seeded account had `is_staff`, so the Django admin was
  unreachable and the new models could not be inspected at all; and I lost
  time to committing without pushing, then reading an unchanged front page as
  evidence the migration had failed.
- **Cut or deferred:** Due-date calculation and status transition logic
  deliberately not written — the columns exist but nothing populates them
  until session 4. Keeping schema and behaviour in separate sessions meant the
  schema could be reviewed on its own terms.
- **Working practice changed:** Sessions 3 onward are developed and verified
  locally against SQLite, with a deploy check at the end of each session
  rather than a round trip to Render per change.

  ### Session 3 — Vehicles, service records, role enforcement
- **Estimated:** 2h
- **Actual:** 1h
- **Where it went:** Mostly straightforward CRUD, which is what this session
  was meant to be. The work that wasn't boilerplate was deciding what *not* to
  put on the forms — the derived next-due fields and every lifecycle field are
  deliberately absent, so the invariant from session 2 can't be broken through
  the UI before the service layer that owns them exists.
- **Cut or deferred:** No assignment UI — assignments were created through the
  admin to test the technician permission path. The permission check itself was
  built properly rather than stubbed, so session 5 adds screens rather than
  logic. Styling deliberately left at plain CSS with no framework and no
  JavaScript.
- **First session with something demonstrable.** Two sessions of schema and
  deployment with nothing clickable was uncomfortable but correct — the
  expensive-to-reverse work was front-loaded, and this session was fast
  because the models didn't need touching.

  ### Session 4 — Service lifecycle, due calculation, timeline
- **Estimated:** 2h
- **Actual:** 3 h
- **Where it went:** The state machine and transition functions were quick
  because the shape was settled in advance — one function per transition
  rather than a generic `transition(record, status)`, since each takes
  different arguments and has different side effects. Most of the time went on
  tests (56 total now, up from 13) and on two additions that weren't in the
  original session plan: scoping vehicles to technicians, and surfacing the
  next-due fields, which had been populated since this session but were
  visible only in the Django admin.
- **Cut or deferred:** No scheduler for date-based due checks. See below.
- **Known gap:** `ensure_due_record` is called on odometer update and on
  service completion, which covers the mileage threshold fully. The date
  threshold only fires if something else touches the vehicle, so a vehicle
  sitting untouched past its date interval is not currently flagged. A
  `check_due_vehicles` management command exists to run the check across the
  fleet; nothing schedules it, because Render's free tier has no worker
  process. Closing this in session 6.
- **Lost time to:** looking for data in the wrong database. Everything had been
  created through the deployed app, so it lived in Neon, while `manage.py
  shell` was reading the local SQLite fallback and reporting zero vehicles —
  which read as broken lifecycle logic for a while. Settled by checking
  `settings.DATABASES["default"]["ENGINE"]`. Sessions 5 and 6 are developed
  locally so that the shell and the browser agree.

  ### Session 5 — Assignment, search, CSV import and export
- **Estimated:** 2h
- **Actual:** 4h 20m
- **Where it went:** Badly over. This session carried three goals, two new
  modules and roughly seventy new tests (56 → 127), which is more than double
  any other session. The CSV import alone has six rejection paths, each needing
  its own test. Splitting it — assignment and search in one sitting, CSV in
  another — would have been the right call, and the fact that I planned it as
  one session is the clearest estimation error in this project.
- **Cut or deferred:** Nothing cut. Full-text search deliberately not used —
  `icontains` is correct at this scale and the Postgres GIN index setup was not
  worth the time; noted in a comment as the fix if it ever gets slow.
- **What I got right:** insisting on a plan before implementation. The
  three-module split (services / filters / csv_io) was proposed by the tool and
  is better than the single-file structure I had specified, and sharing
  `filters.py` between the list view and the export removed a whole class of
  drift bug before it existed.