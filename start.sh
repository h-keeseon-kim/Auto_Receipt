#!/usr/bin/env bash
set -euo pipefail

echo "Starting Gunicorn on PORT=${PORT:-8000}..."
exec gunicorn auto_receipt.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --access-logfile - \
  --error-logfile -
