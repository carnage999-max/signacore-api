#!/bin/sh
set -eu

# Every service in the compose file - api, worker and beat - is built from this image and so runs
# this script. When all three ran `migrate`, they raced: each read django_migrations, each saw the
# same migration unapplied, and each ran the DDL. PostgreSQL serialises that behind an exclusive
# lock, so one commits and the others fail against the change that already landed:
#
#   psycopg.errors.DuplicateColumn: column "signed_pdf" of relation "signing_signingrequest"
#   already exists
#
# Django takes no lock of its own around a migration run, so the only fix is to have one container
# migrate and the rest wait for it. `restart: unless-stopped` hid this: the loser crashed, came back
# to find nothing left to apply, and started. That is luck, not a design - a migration that is not
# a single statement can leave the database half-changed when the loser dies partway through.

mkdir -p \
  "${MEDIA_ROOT:-/srv/signacore/storage}" \
  "${STATIC_ROOT:-/srv/signacore/staticfiles}" \
  "${SIGNACORE_TEMP_ROOT:-/run/signacore}"
chmod 700 "${SIGNACORE_TEMP_ROOT:-/run/signacore}"

# Seconds to keep waiting for the migrating container before giving up. A failed deploy that says
# so is better than a worker serving a schema it was not built for.
MIGRATION_WAIT_SECONDS="${SIGNACORE_MIGRATION_WAIT_SECONDS:-180}"
MIGRATION_POLL_SECONDS="${SIGNACORE_MIGRATION_POLL_SECONDS:-3}"

wait_for_migrations() {
  waited=0
  # `migrate --check` exits non-zero both when migrations are outstanding and when the database
  # cannot be reached, and waiting is the right answer to either during a deploy.
  while ! python manage.py migrate --check >/dev/null 2>&1; do
    if [ "${waited}" -ge "${MIGRATION_WAIT_SECONDS}" ]; then
      echo "entrypoint: migrations were still outstanding after ${waited}s; refusing to start." >&2
      return 1
    fi
    sleep "${MIGRATION_POLL_SECONDS}"
    waited=$((waited + MIGRATION_POLL_SECONDS))
  done
  if [ "${waited}" -gt 0 ]; then
    echo "entrypoint: migrations completed after ${waited}s."
  fi
}

if [ "${SIGNACORE_RUN_MIGRATIONS:-0}" = "1" ]; then
  python manage.py migrate --noinput
  # Collecting static files writes into one shared volume, so it races for the same reason and is
  # the same container's job.
  python manage.py collectstatic --noinput
else
  wait_for_migrations
fi

exec "$@"
