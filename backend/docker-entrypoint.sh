#!/bin/sh
# Single image, several roles: api | worker | beat | migrate | manage <args>
set -e

case "$1" in
  api)
    exec gunicorn config.wsgi:application \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers "${GUNICORN_WORKERS:-3}" \
      --timeout "${GUNICORN_TIMEOUT:-60}" \
      --access-logfile - \
      --no-control-socket \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
    ;;
  worker)
    exec celery -A config worker \
      --loglevel "${LOG_LEVEL:-INFO}" \
      --queues "${CELERY_QUEUES:-default,reviews}" \
      --concurrency "${CELERY_CONCURRENCY:-4}"
    ;;
  beat)
    exec celery -A config beat --loglevel "${LOG_LEVEL:-INFO}" --schedule /tmp/celerybeat-schedule
    ;;
  migrate)
    exec python manage.py migrate --noinput
    ;;
  manage)
    shift
    exec python manage.py "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
