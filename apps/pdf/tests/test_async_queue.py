"""Jobs go through the real Celery queue: the API answers 'pending' at once, a worker runs the
task, and the client polls /api/job/<id>/ until it is done (this is what production does).

A Celery test worker with an in-memory broker runs inside the test process, so no Redis is needed.
"""
import io
import shutil
import tempfile
import time
from datetime import timedelta
from unittest import mock

import fitz
from celery.contrib.testing.worker import start_worker
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.pdf.models import Job
from config.celery import app


def make_pdf(pages=2, text='page'):
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f'{text} {i + 1}')
    data = doc.tobytes()
    doc.close()
    return data


def pdf_upload(name='a.pdf', pages=2, text='page'):
    return SimpleUploadedFile(name, make_pdf(pages, text), content_type='application/pdf')


class QueueTestCase(TransactionTestCase):
    """Starts a worker serving both queues (default 'celery' and 'ocr') on an in-memory broker."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media = tempfile.mkdtemp()
        cls._media_override = override_settings(MEDIA_ROOT=cls.media, CELERY_TASK_ALWAYS_EAGER=False)
        cls._media_override.enable()
        cls._worker_cm = start_worker(app, perform_ping_check=False, queues=['celery', 'ocr'], pool='solo')
        cls._worker_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._worker_cm.__exit__(None, None, None)
        cls._media_override.disable()
        shutil.rmtree(cls.media, ignore_errors=True)
        super().tearDownClass()

    def post(self, url, data):
        resp = Client().post(url, data)
        return resp, resp.json()

    def wait_for(self, job_id, timeout=90):   # generous: a loaded machine once exceeded 30s
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = Client().get(f'/api/job/{job_id}/').json()
            if data['status'] in ('done', 'failed'):
                return data
            time.sleep(0.3)
        self.fail(f'job {job_id} did not finish in {timeout}s')


class PdfToolsViaQueueTests(QueueTestCase):
    def test_merge_returns_pending_then_completes(self):
        resp, body = self.post('/api/merge/', {'files': [pdf_upload('a.pdf', 1), pdf_upload('b.pdf', 2)]})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(body['status'], 'pending')          # not run inline
        done = self.wait_for(body['job_id'])
        self.assertEqual(done['status'], 'done', done)
        job = Job.objects.get(id=body['job_id'])
        self.assertEqual(len(fitz.open(job.result.path)), 3)

    def test_compress_is_queued(self):
        resp, body = self.post('/api/compress/', {'files': [pdf_upload('c.pdf', 2)], 'compression_level': 'recommended'})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(body['status'], 'pending')
        self.assertEqual(self.wait_for(body['job_id'])['status'], 'done')

    def test_split_and_protect_are_queued(self):
        resp, body = self.post('/api/split/', {'file': pdf_upload('s.pdf', 3), 'page_range': '1-2'})
        self.assertEqual(resp.status_code, 201, body)
        self.assertEqual(body['status'], 'pending')
        self.assertEqual(self.wait_for(body['job_id'])['status'], 'done')

        resp, body = self.post('/api/protect/', {'files': [pdf_upload('p.pdf', 1)], 'password': 'secret1'})
        self.assertEqual(resp.status_code, 201, body)
        self.assertEqual(self.wait_for(body['job_id'])['status'], 'done')

    def test_organize_replacement_file_crosses_the_queue(self):
        """Uploaded replacement files cannot be JSON-serialised; the view must pass saved paths."""
        import json
        order = [{'original': 1, 'rotation': 0}, {'isInserted': True, 'fileIndex': 0, 'pageNum': 1, 'rotation': 0}]
        resp, body = self.post('/api/organize/', {
            'file': pdf_upload('o.pdf', 2, text='orig'),
            'page_order': json.dumps(order),
            'replace_file_0': pdf_upload('extra.pdf', 1, text='extra'),
        })
        self.assertEqual(resp.status_code, 201, body)
        done = self.wait_for(body['job_id'])
        self.assertEqual(done['status'], 'done', done)
        text = ' '.join(p.get_text() for p in fitz.open(Job.objects.get(id=body['job_id']).result.path))
        self.assertIn('orig 1', text)
        self.assertIn('extra 1', text)


class OcrViaQueueTests(QueueTestCase):
    def test_ocr_runs_on_the_ocr_queue_and_updates_progress(self):
        from apps.pdf.ocr.engines.base import OCREngine

        class FakeEngine(OCREngine):
            name = 'fake'

            def run(self, doc, page, gray):
                doc.add_paragraph('fake ocr text')
                return 13

        with mock.patch('apps.pdf.ocr.pipeline.build_engines', return_value=([FakeEngine()], 200, True)):
            resp, body = self.post('/api/ocr/', {'file': pdf_upload('scan.pdf', 2), 'ocr_lang': 'khm'})
            self.assertEqual(resp.status_code, 201, body)
            done = self.wait_for(body['job_id'])
        self.assertEqual(done['status'], 'done', done)
        self.assertEqual((done['current_page'], done['total_pages']), (2, 2))

    def test_english_without_cloud_engine_fails_with_clear_message(self):
        resp, body = self.post('/api/ocr/', {'file': pdf_upload('en.pdf', 1), 'ocr_lang': 'eng'})
        self.assertEqual(resp.status_code, 201)
        done = self.wait_for(body['job_id'])
        self.assertEqual(done['status'], 'failed')
        self.assertIn('cloud engine', done['error'])


class StaleJobTests(TransactionTestCase):
    def test_job_stuck_in_processing_is_reported_failed(self):
        job = Job.objects.create(tool='compress', status='processing')
        Job.objects.filter(id=job.id).update(created_at=timezone.now() - timedelta(hours=4))
        data = Client().get(f'/api/job/{job.id}/').json()
        self.assertEqual(data['status'], 'failed')
        self.assertIn('did not finish', data['error'])

    def test_recent_processing_job_is_left_alone(self):
        job = Job.objects.create(tool='compress', status='processing')
        self.assertEqual(Client().get(f'/api/job/{job.id}/').json()['status'], 'processing')


class TaskRoutingTests(SimpleTestCase):
    def test_ocr_task_is_routed_to_the_ocr_queue_in_a_fresh_process(self):
        """Regression: with an empty config/__init__.py, tasks used an unconfigured default app and
        ocr_pdf went to the general queue (so Netra loaded in the tools worker). Checked in a fresh
        process, because other test modules import the Celery app by hand and would mask the bug."""
        import os
        import subprocess
        import sys
        from django.conf import settings

        script = (
            "import os, django\n"
            "os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'\n"
            "django.setup()\n"
            "from apps.pdf.tasks import compress_pdf, ocr_pdf\n"
            "r = lambda t: t.app.amqp.router.route({}, t.name)['queue'].name\n"
            "print('ROUTES', r(ocr_pdf), r(compress_pdf))\n"
        )
        out = subprocess.run([sys.executable, '-c', script], cwd=str(settings.BASE_DIR), capture_output=True,
                             text=True, env={**os.environ, 'PYTHONPATH': str(settings.BASE_DIR)}, timeout=120)
        self.assertIn('ROUTES ocr celery', out.stdout, out.stdout + out.stderr)
