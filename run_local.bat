@echo off
title PDF SaaS - Dev Server
cd /d "%~dp0"

echo Starting PDF SaaS locally...
echo.

REM Set DEBUG mode
set DEBUG=True

REM Run migrations
echo Running migrations...
python manage.py migrate

echo.
echo Starting server at http://127.0.0.1:8000
echo Press Ctrl+C to stop.
echo.

python manage.py runserver

pause
