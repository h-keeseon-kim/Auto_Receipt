#!/usr/bin/env bash
set -euo pipefail

verbosity="${DJANGO_MIGRATE_VERBOSITY:-2}"
max_attempts="${DJANGO_MIGRATE_MAX_ATTEMPTS:-3}"
retry_seconds="${DJANGO_MIGRATE_RETRY_SECONDS:-5}"

echo "Running Django migrations in Railway pre-deploy phase..."
for ((attempt=1; attempt<=max_attempts; attempt++)); do
  echo "Migration attempt ${attempt}/${max_attempts}"
  if python manage.py migrate --noinput --verbosity="${verbosity}"; then
    echo "Database migrations completed."
    break
  fi

  if (( attempt == max_attempts )); then
    echo "Database migrations failed after ${attempt} attempts." >&2
    exit 1
  fi

  echo "Migration attempt ${attempt} failed; retrying in ${retry_seconds}s..." >&2
  sleep "${retry_seconds}"
done

echo "Running Django system checks..."
python manage.py check
