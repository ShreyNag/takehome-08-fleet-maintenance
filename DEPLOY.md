# Deploying (Render + Neon)

## 1. Database — Neon

1. Create a project at neon.tech, then a database (default `neondb` is fine, or rename it).
2. Copy the pooled connection string from the Neon dashboard. It looks like:
   `postgres://user:password@ep-xxxx-pooler.region.aws.neon.tech/dbname?sslmode=require`
3. Keep this value handy for step 2 below — never commit it.

## 2. Web service — Render

Using the blueprint (`render.yaml` in this repo):

1. In the Render dashboard: New → Blueprint → connect this repo.
2. Render reads `render.yaml` and creates the `fleetcare` web service, generating `SECRET_KEY`
   automatically.
3. Set `DATABASE_URL` manually (dashboard → the service → Environment) to the Neon connection
   string from step 1 — it's marked `sync: false` in `render.yaml` so Render won't ask for it
   during blueprint creation, but the app won't boot without it.
4. Render assigns the service a hostname (e.g. `fleetcare-ab12.onrender.com`) which may not match
   the `fleetcare-ufam.onrender.com` placeholder in `render.yaml` (this repo's actual deployed
   hostname). Update `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` to the real hostname (with
   `https://` for the latter) once you know it.

Without the blueprint, create the web service by hand with these settings:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Build command | `./build.sh` |
| Start command | `gunicorn fleetcare.wsgi:application` |
| Env vars | `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL` — same as above |

## 3. First deploy

`build.sh` runs migrations and seeds the demo accounts automatically on every deploy (`seed_users`
uses `get_or_create`, so re-running it on later deploys is a no-op for existing accounts). Check
the build logs for the `Created ...` lines, or open the Render dashboard → Shell and run
`python manage.py seed_users` again to print them.

Record the emails/passwords in `SUBMISSION.md`.

## Free-tier note

Render's free web services spin down after inactivity; the first request after idling can take
about a minute to wake up. Mention this in `SUBMISSION.md` so a slow first load isn't mistaken for
a broken deploy.

## Seeding a demo fleet (`seed_demo`)

Not run by `build.sh` — deliberately run by hand, not on every deploy. It clears its own
previously-seeded vehicles first (matched by the `FC-DEMO-` registration-number prefix) and never
touches the `seed_users` accounts, so it's safe to re-run.

Render's free tier has no Shell (that's a paid feature — see decision #4 in `docs/decisions.md`),
so run it locally against the deployed database instead of on Render itself:

1. Make sure `python manage.py seed_users` has already been run against that database (`seed_demo`
   looks up `manager@fleetcare.demo` as the actor for every write it makes, and errors out if that
   account doesn't exist yet — `build.sh` already does this on every deploy, so it normally has).
2. Point `DATABASE_URL` at the deployed database — either export it in your shell for one command,
   or copy it into your local `.env` temporarily:
   ```
   DATABASE_URL="<the Neon connection string from the Render dashboard>" python manage.py seed_demo
   ```
3. Re-running it is safe and gives back the same fleet — do this to clear stray test vehicles: it
   deletes every `FC-DEMO-*` vehicle and its records before recreating them.

If you're on a paid Render plan with Shell access, `python manage.py seed_demo` in the Render Shell
works exactly the same way.

## Scheduling the due-vehicle sweep (goal 4)

`POST /internal/check-due-vehicles/` needs `DUE_CHECK_TOKEN` set (Render dashboard → Environment;
generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`). With that set,
point a free external scheduler at it every few hours, for example:

- **cron-job.org** (free): create a job with URL
  `https://<your-app>.onrender.com/internal/check-due-vehicles/?token=<DUE_CHECK_TOKEN>`, method
  POST, schedule every 6 hours (or whatever cadence you like — the endpoint itself won't actually
  re-run inside 5 minutes of its last run either way).
- **A scheduled GitHub Actions workflow**, if you'd rather not use a third party:
  ```yaml
  on:
    schedule:
      - cron: "0 */6 * * *"
  jobs:
    check-due:
      runs-on: ubuntu-latest
      steps:
        - run: |
            curl -fsS -X POST \
              -H "Authorization: Bearer ${{ secrets.DUE_CHECK_TOKEN }}" \
              https://<your-app>.onrender.com/internal/check-due-vehicles/
  ```
  with `DUE_CHECK_TOKEN` added as a repo secret matching the Render env var.

Without either configured, the date threshold only gets re-checked when someone edits an odometer
or completes a service (goal 4's mileage path still works on its own) — nothing is broken by
leaving this unset, it just means a vehicle that's overdue purely by calendar date won't be flagged
until the next time it's touched some other way.
