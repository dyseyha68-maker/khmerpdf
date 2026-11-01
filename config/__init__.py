# Load the Celery app when Django starts so @shared_task uses it (broker, routes, settings).
# Without this, tasks fall back to an unconfigured default app and ignore CELERY_TASK_ROUTES.
from .celery import app as celery_app

__all__ = ('celery_app',)
