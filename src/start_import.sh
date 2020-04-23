#!/usr/bin/env bash

set -u   # crash on missing env variables
set -e   # stop on any error
set -x

DJANGO_SETTINGS_MODULE=dso_import.settings
DJANGO_DEBUG=false

python -m venv venv
source venv/bin/activate
pip install  -r requirements.txt
python manage.py run_import bagh "$@"
