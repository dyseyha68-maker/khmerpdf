FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ghostscript \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-khm \
    wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput || true

CMD ["gunicorn", "config.wsgi", "--bind", "0.0.0.0:8000", "--timeout=1200", "--workers=4", "--graceful-timeout=30"]