#!/bin/sh
set -eu

mkdir -p \
  "${MEDIA_ROOT:-/srv/signacore/storage}" \
  "${STATIC_ROOT:-/srv/signacore/staticfiles}" \
  "${SIGNACORE_TEMP_ROOT:-/run/signacore}"
chmod 700 "${SIGNACORE_TEMP_ROOT:-/run/signacore}"

python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec "$@"
