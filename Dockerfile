FROM python:3.11-slim

WORKDIR /app

# Install Ghostscript, Tesseract OCR, Poppler, PyTorch and dependencies
RUN apt-get update && apt-get install -y \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-khm \
    poppler-utils \
    libomp-dev \
    libgcc-s1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch for EasyOCR (CPU version)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput || true

# Increase timeout and workers for large file processing
CMD ["gunicorn", "config.wsgi", "--bind", "0.0.0.0:8000", "--timeout=1200", "--workers=4", "--graceful-timeout=30"]
