#!/usr/bin/env bash
set -o errexit

export DJANGO_SETTINGS_MODULE=django_ims.settings

python -c 'import django; django.setup(); from django.db import connection; connection.ensure_connection()'
python manage.py migrate --no-input
exec gunicorn django_ims.wsgi:application --bind "0.0.0.0:${PORT:-10000}" --workers 1 --threads 1 --timeout 120
