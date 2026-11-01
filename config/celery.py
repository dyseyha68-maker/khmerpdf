import logging
import os
from celery import Celery
from celery.signals import worker_process_init

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('pdf_saas')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


@worker_process_init.connect
def warm_up_netra(**kwargs):
    """Load the Netra model when the OCR worker starts (WARM_NETRA=1), so the first
    user does not wait ~100s for it. A failure here only means a cold first job."""
    if os.environ.get('WARM_NETRA') != '1':
        return
    log = logging.getLogger(__name__)
    try:
        import django
        django.setup()
        from apps.pdf.ocr.engines.netra import warm_up
        warm_up()
        log.info('Netra-OCR model loaded and ready')
    except Exception:
        log.warning('Netra-OCR warm-up failed; first OCR job will load it', exc_info=True)


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
