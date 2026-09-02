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
