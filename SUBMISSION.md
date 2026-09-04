# Submission

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
| Technician | t.alvarez@fleetcare.demo | demo-tech-pass1 |
| Technician | t.chen@fleetcare.demo | demo-tech-pass1 |
| Technician | t.diallo@fleetcare.demo | demo-tech-pass1 |
| Technician | t.fitzgerald@fleetcare.demo | demo-tech-pass1 |
| Technician | t.nakamura@fleetcare.demo | demo-tech-pass1 |

Only the first two accounts are needed to see both roles. The remaining
technicians are created by `seed_demo` so that assignment, the by-technician
dashboard breakdown, and the technician filter have realistic spread — they
are listed here in case a reviewer wants to check a specific name appearing in
the seeded data.

`manager@fleetcare.demo` and `tech@fleetcare.demo` are seeded on every deploy
by `accounts/management/commands/seed_users.py`, run as the last step of
`build.sh`. The five `t.*` technicians are created by
`fleet/management/commands/seed_demo.py`, run by hand against the deployed
database — it is deliberately not wired into `build.sh`, since it also
rebuilds the demo fleet and history and isn't something every deploy should
redo. Demo-only credentials, deliberately committed so a reviewer can sign in
without setup.

## Stack

| Layer | What I used | Why |
|-------|---------------|-----|
| Frontend | Django templates, minimal hand-written CSS | No separate SPA. Goal 6 requires search, filtering and pagination to happen server-side; with server-rendered templates that is structurally true rather than a claim to defend. Also removes CORS, token handling and a second deployment. |
| Backend | Django 5.2 (LTS), Python 3.12 | Ships email/password auth, migrations, an admin for inspecting data, and pagination. On a lighter framework all four are hand-built, and none of that plumbing scores against the ten goals. |
| Database | PostgreSQL on Neon | Postgres for real constraints and aggregate queries. Neon over Render's own free Postgres because Render's free databases expire 30 days after creation — a reviewer opening this five weeks from now would find the data gone. |
| Hosting | Render web service, auto-deploying from `main` | Free tier, no card required, native Python support with a persistent process. Serverless hosts fit Django poorly: no long-lived process and no straightforward way to run `migrate` or seed commands. |

## Goal checklist

| # | Goal | Status | Notes |
|---|------|--------|-------|
| 1 | Accounts and roles | Done | Enforced server-side, asserted by tests (403, not hidden controls). Technicians scoped to their own records and the vehicles behind them. |
| 2 | Vehicles | Done | Create, edit, archive, restore. Fleet list shows next-due date, next-due odometer and a service status computed as a SQL annotation. |
| 3 | Service records | Done | Manager creates; assignee edits description only. Vehicle detail shows service history. |
| 4 | Service lifecycle with rules | Done | Full state machine, server-side rejection with explanatory messages, both counters reset from the completion date and reading. Mileage threshold fires on odometer update and on CSV import. |
| 5 | Assignment | Done | Managers add and remove technicians at any point; both write timeline events. Booking is manager-only, since it creates an assignment as a side effect. Technicians land on a cross-vehicle list of their own records. |
| 6 | Finding service records | Done | Server-side search, filters, sorting and pagination with total match count. Sort parameter allowlisted. Query count asserted flat with result size. |
| 7 | Bulk odometer CSV + history export | Done | Per-row report with six distinct rejection reasons; valid rows apply when others fail; each row in its own transaction. Successful updates trigger the due check. Streaming export sharing filter logic with the list view. |
| 8 | Dashboard | Done | Headline numbers (due, in service, overdue, completed this week), plus a breakdown by status and by technician and an eight-week completions chart. The chart is a div-per-week bar chart in CSS, not a charting library. The whole thing runs a fixed 5 SQL queries regardless of fleet size, asserted by a test. |
| 9 | Immutable history | Done | Every transition, assignment and note writes an event in the same transaction as the change. Append-only at save, delete and admin level. |
| 10 | Overdue service alerts | Done | An alerts area lists overdue records with a nav badge count, and a manager can dismiss one. Dismissal is keyed to the service record, not the vehicle, so once that vehicle becomes due again a new record exists that no dismissal covers — the alert reappears with no extra logic needed. |

## How much time did you actually spend?

Roughly 14 hours across six sessions, against the 12-hour guide.

The split was uneven. Session 1 went mostly on deployment rather than code — getting a skeleton live on Render and Neon on day one, which cost more than expected but meant every later session pushed through a pipeline already known to work. Session 5 was the worst estimate: I planned three goals in one sitting and it ran to five commits and took the test suite from 56 to 127. It should have been two sessions.

Session 6 lost time to things that were not code — a Render deploy that failed on transient port detection despite a successful build and a running server, and running the seed against Neon from my own machine because the free tier has no shell.

Per-session estimates against actuals are in docs/plan.md.

## What would you do next, with another 12 hours?

Move the timeline's immutability into the database. Right now TimelineEvent refuses updates and deletes in save(), delete(), the queryset and the admin — so no code path in the application can rewrite history, including a superuser. But the rule lives in Python. An operator with direct SQL access still could. A Postgres rule blocking UPDATE and DELETE on that table would make the guarantee real rather than conventional.

Replace the due-check workaround with a real scheduler. The date threshold currently fires via a token-authenticated endpoint hit by an external cron, because Render's free tier has no worker process. It works, but it is a hosting workaround dressed as a feature. On a paid tier this is a scheduled management command and the endpoint goes away.

Security settings I left off. SESSION_COOKIE_SECURE, CSRF_COOKIE_SECURE and HSTS are absent — the deployment relies entirely on Render terminating TLS, and Django never forces secure cookies itself. Fine for a demo, wrong for anything real, and I chose not to change deployment settings days before submitting on an app I had already verified.

Postgres full-text search. Goal 6's description search is icontains, which cannot use an index. Correct at this scale and the first thing to degrade at 100x.

Cost reporting per vehicle, as the stretch idea that fits the existing schema best — service records already carry dates and vehicles, so it is an aggregation rather than a new subsystem.

## What are you least happy with in this codebase, and why?

The assignment permission, and specifically the reasoning that produced it rather than the bug itself.

Goal 5 says only a fleet manager can add or remove a technician's assignment. I implemented that on the assign and unassign endpoints and wrote tests asserting a technician gets 403 on both, including the self-assignment case where they submit their own account. All of it passed while a technician could still create an assignment by booking a record, because booking assigns a technician as a side effect of a status transition and the word "assign" appears nowhere in it.

I found it by clicking around as a technician and reading a timeline that showed DUE → BOOKED performed by a technician's account. My first fix was also wrong — I allowed a technician to book provided they assigned themselves, which is the same thing by another route and also lets a technician clear a manager's overdue alert by picking up work nobody scheduled. Booking is now manager-only outright.

What bothers me is that a final review found the same shape again: the record list's technician and vehicle filter dropdowns were unscoped, while vehicles are carefully scoped everywhere else in the app. Twice in one project, and both times because I was asking "is this endpoint protected?" when the requirement was "can a technician cause this to happen, by any route?"

The structural fix is to put the role check inside assign_technician itself, so every caller is covered regardless of which view reached it. I have the permission on the views, which is why a path that assigned without being named "assign" slipped through. That is what I would change first.