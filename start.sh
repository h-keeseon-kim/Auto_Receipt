#!/usr/bin/env bash
set -euo pipefail

echo "Applying database migrations..."
for attempt in 1 2 3 4 5; do
  if python manage.py migrate --noinput; then
    break
  fi

  if [[ "$attempt" == "5" ]]; then
    echo "Database migrations failed after ${attempt} attempts." >&2
    exit 1
  fi

  echo "Migration attempt ${attempt} failed; retrying..." >&2
  sleep 5
done

echo "Starting Gunicorn on PORT=${PORT:-8000}..."
exec gunicorn auto_receipt.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --access-logfile - \
  --error-logfile -
