"""Engine selection and the per-page fallback cascade for OCR.

Order (first engine that yields text wins): Claude → Gemini → Google Vision
(only when neither AI engine is enabled) → Netra-OCR (Khmer, local) → Tesseract.
"""
import logging
import time

from apps.pdf.ocr.engines.claude import ClaudeEngine
from apps.pdf.ocr.engines.gcv import GoogleVisionEngine, _VISION_LANG_HINTS, _build_vision_client
from apps.pdf.ocr.engines.gemini import GeminiEngine
from apps.pdf.ocr.engines.netra import NetraEngine
from apps.pdf.ocr.engines.tesseract import TesseractEngine

logger = logging.getLogger(__name__)

RETRIES = 2          # extra attempts for remote engines
BACKOFF_SECONDS = 2  # doubles each retry (2s, 4s) — helps with free-tier rate limits

_LEGACY_GEMINI = ('gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-flash', 'gemini-flash-latest')


def build_engines(ocr_settings, ocr_lang, tess_lang):
    """Return (engines, dpi, fallback_ok) for this job from the admin settings."""
    s = ocr_settings
    fallback_ok = s.fallback_to_tesseract if s else True
    engines = []

    claude_key = (s.claude_api_key or '').strip() if s else ''
    use_claude = bool(s and s.use_claude and claude_key)
    gemini_key = (s.gemini_api_key or '').strip() if s else ''
    use_gemini = bool(s and s.use_gemini and gemini_key)

    if use_claude:
        model = (s.claude_model or 'claude-3-5-sonnet-20241022').strip()
        logger.info(f'OCR: using Claude AI Vision ({model})')
        engines.append(ClaudeEngine(claude_key, model, ocr_lang))
    if use_gemini:
        model = (s.gemini_model or 'gemini-flash-lite-latest').strip()
        if model in _LEGACY_GEMINI:
            model = 'gemini-flash-lite-latest'
        logger.info(f'OCR: using Google Gemini Flash Vision ({model})')
        engines.append(GeminiEngine(gemini_key, model, ocr_lang))

    if not use_claude and not use_gemini and s and s.use_google_vision and s.google_credentials_json.strip():
        try:
            client = _build_vision_client(s)
            logger.info('OCR: using Google Cloud Vision (DOCUMENT_TEXT_DETECTION)')
            engines.append(GoogleVisionEngine(client, _VISION_LANG_HINTS.get(ocr_lang, ['en'])))
        except Exception as e:
            logger.error(f'OCR: Google Vision client init failed: {e}', exc_info=True)
            if not fallback_ok:
                raise RuntimeError(f'Google Vision is enabled but could not be configured: {e}')

    local_engine = (s.local_engine or 'khmerocr') if s else 'khmerocr'
    if local_engine == 'khmerocr' and ocr_lang in ('khm', 'eng+khm'):
        engines.append(NetraEngine(ocr_lang))
    engines.append(TesseractEngine(tess_lang))

    dpi = 250 if (use_claude or use_gemini) else 200
    return engines, dpi, fallback_ok


def _run_with_retry(engine, doc, page, gray):
    attempts = 1 + (RETRIES if engine.remote else 0)
    for attempt in range(attempts):
        try:
            return engine.run(doc, page, gray)
        except Exception:
            if attempt == attempts - 1:
                raise
            delay = BACKOFF_SECONDS * (2 ** attempt)
            logger.warning(f'{engine.name} failed, retrying in {delay}s', exc_info=True)
            time.sleep(delay)


def process_page(engines, doc, page, gray, page_num, fallback_ok=True):
    """Run the cascade on one page. Returns (chars_written, engine_name or None).

    An engine that errors or returns no text hands the page to the next one.
    With fallback disabled, an error from an optional (cloud) engine aborts
    the job instead.
    """
    for engine in engines:
        try:
            chars = _run_with_retry(engine, doc, page, gray)
        except Exception as e:
            logger.error(f'{engine.name} failed on page {page_num}: {e}', exc_info=True)
            if engine.optional and not fallback_ok:
                raise RuntimeError(f'{engine.name} OCR failed on page {page_num}: {e}')
            continue
        if chars and chars > 0:
            logger.info(f'{engine.name} extracted {chars} chars on page {page_num}')
            return chars, engine.name
        logger.info(f'{engine.name} returned no text on page {page_num}')
    return 0, None
