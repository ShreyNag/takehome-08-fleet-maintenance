#!/usr/bin/env bash
# Render build step: install deps, gather static files, apply migrations.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
