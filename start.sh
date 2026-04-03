#!/bin/bash
python manage.py migrate --noinput
python manage.py collectstatic --noinput
gunicorn config.wsgi --log-file - --bind 0.0.0.0:$PORT
