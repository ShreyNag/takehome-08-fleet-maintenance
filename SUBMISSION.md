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
| 1 | Accounts and roles | Partial | Email/password auth, both roles, enforcement wired into every view and asserted by tests (403 from the server, not hidden buttons). Manager control of assignment lands in session 5. |
| 2 | Vehicles | Done | Create, edit, archive, restore. Archived vehicles leave the default list and keep their service history. Derived next-due fields deliberately absent from the form. |
| 3 | Service records | Done | Manager creates against a vehicle; assignee edits the description only. Vehicle detail shows service history. Assignment is displayed here but managed in session 5. |
| 4 | Service lifecycle with rules | Not done | Records are created as Due; nothing moves them yet. Session 4. |
| 5 | Assignment | Partial | Through-model and permission checks exist and are tested; no UI, assignments made via admin. Session 5. |
| 6 | Finding service records | Not done | |
| 7 | Bulk odometer CSV + history export | Not done | |
| 8 | Dashboard | Not done | |
| 9 | Immutable history | Partial | Model is append-only, enforced at save/delete and in the admin. Nothing writes events yet — session 4. |
| 10 | Overdue service alerts | Not done | Dismissal model exists, keyed per record. |

## How much time did you actually spend?

## What would you do next, with another 12 hours?

## What are you least happy with in this codebase, and why?
