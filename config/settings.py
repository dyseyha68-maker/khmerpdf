import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-dev-key-change-in-production')

DEBUG = os.environ.get('DEBUG', 'True') == 'True'  # default True for dev; set DEBUG=False in production

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get(
        'ALLOWED_HOSTS',
        '18.143.195.44,127.0.0.1,localhost,damspey.com,www.damspey.com,.fly.dev,.railway.app'
    ).split(',')
    if h.strip()
]

CSRF_TRUSTED_ORIGINS = [
    'https://*.fly.dev',
    'https://*.railway.app',
    'https://damspey.com',
    'https://www.damspey.com',
    'http://damspey.com',
    'http://www.damspey.com',
    'http://66.29.139.34',
    'https://66.29.139.34',
    'http://127.0.0.1:8000',
    'http://localhost:8000',
]

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True


DATA_DIR = Path(os.environ.get('DATA_DIR', BASE_DIR))

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'apps.pdf',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',   # ← i18n language detection
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.pdf.middleware.VisitorTrackingMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates', BASE_DIR / 'templates' / 'admin'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.template.context_processors.i18n',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.pdf.context_processors.ad_snippets',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': DATA_DIR / 'db.sqlite3',
        'OPTIONS': {
            'timeout': 30,               # wait up to 30s for SQLite lock
            'check_same_thread': False,  # allow background threads to write
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

LANGUAGE_CODE = 'km'
TIME_ZONE = 'Asia/Phnom_Penh'
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ('en', 'English'),
    ('km', 'ខ្មែរ'),
]

LOCALE_PATHS = [BASE_DIR / 'locale']

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Increase timeout for large file uploads
DATA_UPLOAD_MAX_MEMORY_SIZE = 400 * 1024 * 1024  # 400MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 400 * 1024 * 1024  # 400MB

# File upload settings
MAX_UPLOAD_SIZE = 350 * 1024 * 1024  # 350MB - allow large PDF files

# Poppler path for pdf2image (Windows). Set to the bin folder inside your poppler download.
# e.g. r"D:\poppler\Library\bin"  or  r"C:\poppler\bin"
# Leave as None if poppler is already in system PATH (e.g. installed via apt on Linux/Docker).
POPPLER_PATH = os.environ.get('POPPLER_PATH') or (
    r"D:\08. Saas\poppler\Library\bin" if os.name == 'nt' else None
)

ALLOWED_EXTENSIONS = ['pdf']

# Celery settings
# Local dev default: run tasks inline (no Redis/worker needed). Production sets
# CELERY_ALWAYS_EAGER=False so jobs go through the Redis queue to the celery workers.
CELERY_TASK_ALWAYS_EAGER = os.environ.get('CELERY_ALWAYS_EAGER', 'True') == 'True'
CELERY_TASK_EAGER_PROPAGATES = False  # tasks record failures on the Job row; the API reports it via /api/job/
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_TASK_IGNORE_RESULT = True  # job state lives in the Job table, not the result backend
CELERY_TASK_ROUTES = {'apps.pdf.tasks.ocr_pdf': {'queue': 'ocr'}}  # heavy Netra worker, concurrency 1
CELERY_BROKER_TRANSPORT_OPTIONS = {'visibility_timeout': 4 * 3600}  # long OCR jobs must not be redelivered

import sys
if 'test' in sys.argv:
    # `manage.py test`: real queue semantics (jobs are queued, a test worker runs them) without Redis
    CELERY_TASK_ALWAYS_EAGER = False
    CELERY_BROKER_URL = 'memory://'
    CELERY_RESULT_BACKEND = 'cache+memory://'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = False

# Django REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],   # no session auth → no DRF CSRF check
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
}

# Static and Media files
import os

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]

if not DEBUG:
    STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
else:
    STATICFILES_STORAGE = 'django.contrib.staticfiles.storage.StaticFilesStorage'

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(DATA_DIR, 'media')

# Create media directories if they don't exist
os.makedirs(os.path.join(MEDIA_ROOT, 'uploads'), exist_ok=True)
os.makedirs(os.path.join(MEDIA_ROOT, 'processed'), exist_ok=True)

# Google Cloud Vision API (for OCR)
GOOGLE_CLOUD_API_KEY = os.environ.get('GOOGLE_CLOUD_API_KEY', '')
