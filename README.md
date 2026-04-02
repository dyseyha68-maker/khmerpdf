# PDF SaaS - Local Development Environment

A Django-based PDF processing SaaS application with background job processing using Celery and Redis.

## Project Structure

```
project_root/
├── manage.py
├── config/
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   ├── celery.py
├── apps/
│   ├── pdf/
│   │   ├── models.py
│   │   ├── views.py
│   │   ├── tasks.py
│   │   ├── urls.py
│   ├── users/
├── templates/
├── static/
├── media/
├── requirements.txt
├── docker-compose.yml
├── Dockerfile
├── README.md
```

## Tech Stack

- Django 4.x (Web Framework)
- Django REST Framework (API)
- Celery (Background Processing)
- Redis (Message Broker)
- SQLite (Database - dev)
- pikepdf (PDF Processing)

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Run Redis

Using Docker:
```bash
docker run -d -p 6379:6379 redis
```

Or install Redis locally and run:
```bash
redis-server
```

### 3. Run Migrations

```bash
python manage.py migrate
```

### 4. Start Django Server

```bash
python manage.py runserver
```

### 5. Start Celery Worker (in new terminal)

```bash
celery -A config worker --loglevel=info
```

## Usage

1. Open browser at http://localhost:8000
2. Upload a PDF file
3. Wait for processing (Celery handles it in background)
4. Download the compressed PDF
5. View the KHQR tip page

## API Endpoints

- `POST /api/compress/` - Upload PDF for compression
- `GET /api/job/<job_id>/` - Check job status

## Using Docker

```bash
docker-compose up --build
```

This will start:
- Django web server on port 8000
- Redis on port 6379
- Celery worker

## Features

- PDF compression using pikepdf
- Background processing with Celery
- Job status tracking (pending, processing, done, failed)
- File validation (PDF only, 50MB max)
- Clean, modern UI
