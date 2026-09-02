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
   the `fleetcare.onrender.com` placeholder in `render.yaml`. Update `ALLOWED_HOSTS` and
   `CSRF_TRUSTED_ORIGINS` to the real hostname (with `https://` for the latter) once you know it.

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
