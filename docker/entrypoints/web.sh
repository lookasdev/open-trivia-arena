#!/bin/sh
set -eu

/app/docker/entrypoints/run_with_backoff.sh python manage.py migrate --noinput

gunicorn config.wsgi:application \
  --bind 0.0.0.0:${GUNICORN_PORT:-8000} \
  --workers 3 &

exec daphne -b 0.0.0.0 -p ${DAPHNE_PORT:-8001} config.asgi:application
