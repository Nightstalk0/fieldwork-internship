#!/usr/bin/env bash
set -o errexit
export DJANGO_SETTINGS_MODULE=django_ims.settings
python -m pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input