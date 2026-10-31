"""Google Cloud Vision OCR engine."""
import io
import json

from apps.pdf.ocr.engines.base import OCREngine


_VISION_LANG_HINTS = {
    'eng': ['en'],
    'khm': ['km'],
    'eng+khm': ['en', 'km'],
}


def _build_vision_client(ocr_settings):
    """Build an authenticated Vision client from the admin-pasted service
    account JSON. Raises if the JSON is missing/malformed so the caller can
    log a clear error and fall back to Tesseract."""
    from google.cloud import vision
    from google.oauth2 import service_account

    info = json.loads(ocr_settings.google_credentials_json)
    credentials = service_account.Credentials.from_service_account_info(info)
    return vision.ImageAnnotatorClient(credentials=credentials)


def _vision_ocr_page(client, pil_image, lang_hints=None):
    """Run Google Vision DOCUMENT_TEXT_DETECTION on one page image, return
    the extracted text (same shape as pytesseract.image_to_string output)."""
    from google.cloud import vision

    buf = io.BytesIO()
    pil_image.convert('RGB').save(buf, format='PNG')
    image = vision.Image(content=buf.getvalue())
    image_context = vision.ImageContext(language_hints=lang_hints) if lang_hints else None

    response = client.document_text_detection(image=image, image_context=image_context)
    if response.error and response.error.message:
        raise RuntimeError(response.error.message)
    return response.full_text_annotation.text


def _clean_text(text):
    if not text:
        return ""
    cleaned = []
    for line in text.split('\n'):
        line = line.strip()
        if len(line) < 2:
            continue
        line = line.rstrip('|_»¿¡')
        if line:
            cleaned.append(line)
    return '\n'.join(cleaned)


class GoogleVisionEngine(OCREngine):
    name = 'Google Vision'
    remote = True
    optional = True

    def __init__(self, client, lang_hints):
        self.client, self.lang_hints = client, lang_hints

    def run(self, doc, page, gray):
        from docx.shared import Pt
        from docx.oxml.ns import qn

        total = 0
        for line in _clean_text(_vision_ocr_page(self.client, page, lang_hints=self.lang_hints)).split('\n'):
            line = line.strip()
            if not line:
                continue
            p = doc.add_paragraph(line)
            total += len(line)
            has_khmer = any('ក' <= c <= '៿' for c in line)
            font_name = 'Khmer OS Battambang' if has_khmer else 'Calibri'
            for run in p.runs:
                run.font.name = font_name
                run.font.size = Pt(11)
                if has_khmer:
                    rFonts = run._element.get_or_add_rPr().get_or_add_rFonts()
                    for attr in ('w:cs', 'w:ascii', 'w:hAnsi', 'w:eastAsia'):
                        rFonts.set(qn(attr), font_name)
        return total
