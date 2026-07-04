#!/bin/sh
set -eu

mkdir -p "${MEDIA_ROOT:-/srv/signacore/storage}" "${STATIC_ROOT:-/srv/signacore/staticfiles}"

python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec "$@"

