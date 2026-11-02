"""Memory guards for OCR: render DPI by language and the per-job page limit."""
import shutil
import tempfile
from types import SimpleNamespace

import fitz
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from apps.pdf.models import Job
from apps.pdf.ocr.pages import _iter_document_pages
from apps.pdf.ocr.pipeline import build_engines
from apps.pdf.tasks import ocr_pdf


def pdf_bytes(pages):
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f'page {i + 1}')
    data = doc.tobytes()
    doc.close()
    return data


def settings_with_gemini():
    return SimpleNamespace(
        use_claude=False, claude_api_key='', claude_model='', use_gemini=True, gemini_api_key='k', gemini_model='',
        use_google_vision=False, google_credentials_json='', local_engine='khmerocr', fallback_to_tesseract=True)


class DpiTests(SimpleTestCase):
    def test_khmer_files_render_at_200_dpi_even_with_gemini_on(self):
        for lang in ('khm', 'eng+khm'):
            self.assertEqual(build_engines(settings_with_gemini(), lang)[1], 200, lang)

    def test_english_files_use_250_dpi_for_the_cloud_engine(self):
        self.assertEqual(build_engines(settings_with_gemini(), 'eng')[1], 250)


class PageLimitTests(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = f'{self.dir}/three.pdf'
        with open(self.path, 'wb') as fh:
            fh.write(pdf_bytes(3))

    def pages(self, **kw):
        return [seq for seq, _, _ in _iter_document_pages(self.path, dpi=50, **kw)]

    def test_within_the_limit_all_pages_are_read(self):
        self.assertEqual(self.pages(max_limit=3), [1, 2, 3])
        self.assertEqual(self.pages(max_limit=None), [1, 2, 3])

    def test_over_the_limit_fails_clearly_instead_of_cutting_pages_off(self):
        with self.assertRaisesMessage(RuntimeError, 'the limit is 2 pages per OCR job'):
            self.pages(max_limit=2)

    def test_the_limit_applies_to_an_explicit_page_range_too(self):
        self.assertEqual(self.pages(page_range_str='1-2', max_limit=2), [1, 2])
        with self.assertRaisesMessage(RuntimeError, 'the limit is 2'):
            self.pages(page_range_str='1-3', max_limit=2)


class OcrJobLimitTests(TransactionTestCase):
    def test_a_job_over_the_limit_fails_with_the_message_for_the_user(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media, OCR_MAX_PAGES=1):
            job = Job.objects.create(tool='ocr', compression_level='khm', original_filename='big.pdf', files={})
            job.file.save('big.pdf', ContentFile(pdf_bytes(2)))
            with self.assertRaises(RuntimeError):
                ocr_pdf(str(job.id))
            job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertIn('the limit is 1 pages per OCR job', job.error_message)
        self.assertIn('select fewer pages', job.error_message.lower())
