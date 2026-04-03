FROM python:3.11-slim

WORKDIR /app

# Install Ghostscript, Tesseract OCR, Poppler, OpenCV dependencies and language packs
RUN apt-get update && apt-get install -y \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-khm \
    poppler-utils \
    libomp-dev \
    libgcc-s1 \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput || true

# Increase timeout for large PDF processing (15 minutes)
CMD ["gunicorn", "config.wsgi", "--bind", "0.0.0.0:8000", "--timeout=900", "--workers=2"]
