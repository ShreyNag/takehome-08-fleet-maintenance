# Submission

Fill this in and commit it. This is the first file we open.

## Links

- **GitHub repository:** https://github.com/ShreyNag/takehome-08-fleet-maintenance
- **Live application:** https://fleetcare-ufam.onrender.com

## Notes for the reviewer

Cold Starts

The app runs on Render's free tier, which spins the service down after 15
minutes of inactivity. The first request after an idle period takes roughly a
minute while the instance wakes. A slow first load is expected, not a broken
deployment. Subsequent requests are fast.

The database is Neon (Postgres), which is separate from Render and does not
sleep.

## Demo credentials

| Role | Email | Password |
|------|-------|----------|
| Fleet manager | manager@fleetcare.demo | demo-manager-pass1 |
| Technician | tech@fleetcare.demo | demo-tech-pass1 |

Seeded on every deploy by `accounts/management/commands/seed_users.py`, run as
the last step of `build.sh`. Demo-only credentials, deliberately committed so a
reviewer can sign in without setup.

## Stack

| Layer | What you used | Why |
|-------|---------------|-----|
| Frontend | Django templates, minimal hand-written CSS | No separate SPA. Goal 6 requires search, filtering and pagination to happen server-side; with server-rendered templates that is structurally true rather than a claim to defend. Also removes CORS, token handling and a second deployment. |

| Backend | Django 5.2 (LTS), Python 3.12 | Ships email/password auth, migrations, an admin for inspecting data, and pagination. On a lighter framework all four are hand-built, and none of that plumbing scores against the ten goals. |

| Database | PostgreSQL on Neon | Postgres for real constraints and aggregate queries. Neon over Render's own free Postgres because Render's free databases expire 30 days after creation — a reviewer opening this five weeks from now would find the data gone. |

| Hosting | Render web service, auto-deploying from `main` | Free tier, no card required, native Python support with a persistent process. Serverless hosts fit Django poorly: no long-lived process and no straightforward way to run `migrate` or seed commands. |


## Goal checklist

Mark each honestly. Partial is fine — say what is partial.

| # | Goal | Status | Notes |
|---|------|--------|-------|
| 1 | | Done / Partial / Not done | |

| # | Goal | Status | Notes |
|---|------|--------|-------|
| 1 | Accounts and roles | Done | Enforced server-side, asserted by tests (403, not hidden controls). Technicians scoped to their own records and the vehicles behind them. |
| 2 | Vehicles | Done | Create, edit, archive, restore. Fleet list shows next-due date, next-due odometer and a service status computed as a SQL annotation. |
| 3 | Service records | Done | Manager creates; assignee edits description only. Vehicle detail shows service history. |
| 4 | Service lifecycle with rules | Partial | Full state machine, server-side rejection with explanatory messages, both counters reset from the completion date and reading. Mileage threshold fires on odometer update and on CSV import. The date threshold needs `check_due_vehicles`, which nothing schedules yet — closing in session 6. |
| 5 | Assignment | Done | Managers add and remove technicians at any point; both write timeline events. Technicians land on a cross-vehicle list of their own records after login. |
| 6 | Finding service records | Done | Server-side search, filters, sorting and pagination with total match count. Sort parameter allowlisted. Query count asserted flat with result size. |
| 7 | Bulk odometer CSV + history export | Done | Per-row report with six distinct rejection reasons; valid rows apply when others fail; each row in its own transaction. Successful updates trigger the due check. Streaming export sharing filter logic with the list view. |
| 8 | Dashboard | Not done | Session 6. |
| 9 | Immutable history | Done | Every transition, assignment and note writes an event in the same transaction as the change. Append-only at save, delete and admin level. |
| 10 | Overdue service alerts | Not done | Overdue available as a queryset filter; no alerts area or badge yet — session 6. |

## How much time did you actually spend?

## What would you do next, with another 12 hours?

## What are you least happy with in this codebase, and why?
