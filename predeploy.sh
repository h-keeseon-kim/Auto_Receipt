#!/usr/bin/env bash
set -euo pipefail

verbosity="${DJANGO_MIGRATE_VERBOSITY:-2}"
max_attempts="${DJANGO_MIGRATE_MAX_ATTEMPTS:-3}"
retry_seconds="${DJANGO_MIGRATE_RETRY_SECONDS:-5}"
service_name="${RAILWAY_SERVICE_NAME:-}"
migration_mode="${RECEIPTHUB_PREDEPLOY_MIGRATIONS:-auto}"

# Fail immediately for deterministic application errors (for example a URL
# that points to a missing view). Retrying those errors only hides the real
# traceback and never reaches the database migration phase.
echo "Running Django system checks before migrations..."
python manage.py check

case "${migration_mode,,}" in
  0|false|no|off)
    run_migrations=false
    ;;
  1|true|yes|on)
    run_migrations=true
    ;;
  auto|"")
    # The same Git commit deploys the web service and scheduled reminder
    # services. Only the web service should own schema migrations by default;
    # otherwise three containers can attempt the same DDL concurrently.
    if [[ "${service_name}" == receipt-reminder-* ]]; then
      run_migrations=false
    else
      run_migrations=true
    fi
    ;;
  *)
    echo "Invalid RECEIPTHUB_PREDEPLOY_MIGRATIONS=${migration_mode}; use auto/true/false." >&2
    exit 2
    ;;
esac

if [[ "${run_migrations}" != true ]]; then
  echo "Skipping database migrations for Railway service '${service_name:-unknown}' (mode=${migration_mode})."
  exit 0
fi

echo "Running Django migrations in Railway pre-deploy phase..."
for ((attempt=1; attempt<=max_attempts; attempt++)); do
  echo "Migration attempt ${attempt}/${max_attempts}"
  if python manage.py migrate --noinput --skip-checks --verbosity="${verbosity}"; then
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

echo "Running Django system checks after migrations..."
python manage.py check
