FROM python:3.11-alpine

WORKDIR /app

RUN apk add --no-cache \
    ghostscript \
    poppler-utils \
    libgcc \
    libstdc++

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput || true

CMD ["gunicorn", "config.wsgi", "--bind", "0.0.0.0:8000", "--timeout=1200", "--workers=4", "--graceful-timeout=30"]