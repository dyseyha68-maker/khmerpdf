web: sh -c "python manage.py migrate --noinput && python manage.py populate_calendar && gunicorn config.wsgi --log-file - --bind 0.0.0.0:$PORT"
