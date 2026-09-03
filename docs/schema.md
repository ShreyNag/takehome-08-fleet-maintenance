# Schema

Answer each of these, in your own words.

- Table by table: what columns and types does each one have?
- Which relationships are one-to-many, and which are many-to-many?
- Which constraints are enforced by the database, and which by application code — and why did you draw the line there?
- What did you deliberately denormalise?
- What would break first if this had 100x the data?


Postgres (Neon) in production, SQLite locally. Django ORM, migrations checked
into the repo.

## accounts_user

Custom user model, `AbstractBaseUser` + `PermissionsMixin`. Email is the login
field; there is no username column.

| Column | Type | Notes |
|--------|------|-------|
| id | bigint PK | |
| email | varchar, unique | `USERNAME_FIELD` |
| password | varchar | Django's hashed format |
| role | varchar | `FLEET_MANAGER` \| `TECHNICIAN` |
| is_active, is_staff, is_superuser | boolean | `is_staff` gates Django admin only, not application roles |
| date_joined, last_login | timestamp | |

`role` is application authorisation. `is_staff` / `is_superuser` are Django
admin permissions and deliberately separate — the seeded demo manager is not a
staff user, so the role enforcement can be demonstrated honestly rather than
bypassed by a superuser flag.

## fleet_vehicle

| Column | Type | Notes |
|--------|------|-------|
| id | bigint PK | |
| registration_number | varchar, unique, indexed | Natural key; also what the CSV import matches on (goal 7) |
| make, model | varchar | |
| current_odometer | integer ≥ 0 | |
| service_interval_days | integer ≥ 0 | |
| service_interval_km | integer ≥ 0 | |
| next_due_date | date, null, indexed | Denormalised — see below |
| next_due_odometer | integer, null, indexed | Denormalised — see below |
| is_archived | boolean, indexed | |
| created_at, updated_at | timestamp | |

Two managers. The default excludes archived vehicles, so the fleet view is
correct without every call site remembering to filter. `all_objects` includes
them, for the restore view and the admin. Declaration order matters: Django
uses the first declared manager for related-object access.

## fleet_servicerecord

| Column | Type | Notes |
|--------|------|-------|
| id | bigint PK | |
| vehicle_id | FK → fleet_vehicle, PROTECT | |
| description | text | Searched by goal 6 |
| status | varchar, indexed | `DUE` \| `BOOKED` \| `IN_SERVICE` \| `COMPLETED` |
| due_since | timestamp, null | When the record entered DUE |
| scheduled_date | date, null | Set on booking |
| completed_at | timestamp, null | |
| completed_odometer | integer, null | |
| created_by_id | FK → accounts_user, PROTECT | |
| created_at, updated_at | timestamp | |

Composite index on `(status, due_since)` — the overdue alerts query (goal 10)
and the dashboard status counts (goal 8) both filter on exactly that pair.

`PROTECT` rather than `CASCADE` on the vehicle FK: deleting a vehicle with
service history should fail loudly. Archiving is the supported way to retire
one (goal 2 requires history to survive).

## fleet_serviceassignment

Join table between service records and technicians, modelled explicitly rather
than as a bare `ManyToManyField`.

| Column | Type | Notes |
|--------|------|-------|
| id | bigint PK | |
| service_record_id | FK, CASCADE | |
| technician_id | FK → accounts_user, PROTECT | |
| assigned_at | timestamp | |
| assigned_by_id | FK → accounts_user, PROTECT | |

Unique on `(service_record, technician)`.

Explicit because goal 9 needs `assigned_at` and `assigned_by` in the timeline,
and a bare M2M has nowhere to put them.

## fleet_timelineevent

Append-only audit trail.

| Column | Type | Notes |
|--------|------|-------|
| id | bigint PK | |
| service_record_id | FK, CASCADE | |
| event_type | varchar | `CREATED`, `STATUS_CHANGED`, `TECHNICIAN_ASSIGNED`, `TECHNICIAN_UNASSIGNED`, `NOTE_ADDED` |
| actor_id | FK → accounts_user, PROTECT, null | Null means system-generated |
| old_value, new_value | varchar, blank | |
| note | text, blank | |
| created_at | timestamp, indexed | |

Immutability is enforced in three places: `save()` raises if the instance
already has a primary key, `delete()` raises on both model and queryset, and
the admin registration returns `False` from `has_change_permission` and
`has_delete_permission`. The last one is visible in the admin index, where
Timeline events offers "View" while every other model offers "Change" — a
superuser cannot edit them either, which is what goal 9 asks for.

## fleet_alertdismissal

| Column | Type | Notes |
|--------|------|-------|
| id | bigint PK | |
| service_record_id | FK, CASCADE | |
| dismissed_by_id | FK → accounts_user, PROTECT | |
| dismissed_at | timestamp | |

Unique on `(service_record, dismissed_by)`.

Keyed to the **record**, not the vehicle. This is what makes goal 10's
reappearance rule work with no extra machinery: when a vehicle becomes due
again, a new `ServiceRecord` exists, no dismissal row references it, and the
alert returns. Keying to the vehicle would have required storing and comparing
a dismissal timestamp against each new due period.

## Relationships

One-to-many:
- Vehicle → ServiceRecord
- ServiceRecord → TimelineEvent
- ServiceRecord → AlertDismissal
- User → ServiceRecord (as creator)

Many-to-many:
- ServiceRecord ↔ User (technicians), through `ServiceAssignment`

## Constraints: database vs application

**In the database** — anything whose violation would corrupt data: uniqueness
on registration number and email, uniqueness on both join tables, foreign key
integrity with `PROTECT` where history must survive, non-negative odometer and
interval values via `PositiveIntegerField`, and a check that `completed_at` and
`completed_odometer` are either both set or both null.

**In the application** — anything requiring context the database doesn't have:
the status transition rules (goal 4), because legality depends on the current
status and the acting user's role, and the CSV rejection rule that a new
odometer reading may not be lower than the current one (goal 7), because that
comparison needs the existing row and must produce a human-readable reason per
row rather than an integrity error.

The dividing line: the database protects invariants, the application enforces
policy.

## What was deliberately denormalised

`vehicle.next_due_date` and `vehicle.next_due_odometer`. Both are derivable
from the vehicle's last completed service plus its intervals, so storing them
is redundant.

They are stored because goals 6 and 8 need due-ness to be filterable, sortable
and countable in SQL with pagination and a total count. Computed as a Python
property, "which vehicles are due" becomes a full scan plus a loop, and
`Paginator` cannot give a total without materialising every row.

The cost is a consistency risk: they can drift if written incorrectly. The
mitigation is that exactly one code path may write them — `complete_service()`,
in the service layer, not in views. Updating a vehicle's odometer reading
never writes these fields; it only calls `ensure_due_record()` to check
whether the new reading has crossed an existing threshold. The thresholds
themselves are anchored to the last completed service, so an odometer edit
changes whether one has been crossed, never where it sits.

## What breaks first at 100x

**`fleet_timelineevent`.** It grows without bound, has no archival strategy,
and is written on every status change and assignment. At 100x it dominates
storage and any query joining it becomes the slowest thing in the app. First
fix would be partitioning by `created_at` or moving events older than a year to
cold storage.

**Second, the description search.** Goal 6's text search is `icontains`, which
compiles to `ILIKE '%term%'` and cannot use a standard index. At a few thousand
records it is fine; at hundreds of thousands it degrades. Fix is Postgres
full-text search with a GIN index on a `tsvector` column, which was not worth
the setup cost at this scale.

**Third, the dashboard.** Goal 8's eight-week chart aggregates over the whole
service record table on every page load. At 100x it wants either a materialised
view refreshed periodically or a cached result, since the numbers do not need
to be real-time.