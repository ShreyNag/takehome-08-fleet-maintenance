#!/usr/bin/env bash
# Render build step: install deps, gather static files, apply migrations,
# then seed demo accounts (idempotent — safe to re-run every deploy).
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
python manage.py seed_users
