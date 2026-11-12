#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

STATIC_BUILD_ONLY=1 python manage.py collectstatic --no-input