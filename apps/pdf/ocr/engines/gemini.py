"""Google Gemini vision OCR engine."""
from apps.pdf.ocr.docx_writer import _add_structured_content_to_docx
from apps.pdf.ocr.engines.base import OCREngine
from apps.pdf.ocr.engines.prompt import _get_vision_prompt


def _gemini_ocr_page(api_key, model, pil_image, ocr_lang='eng'):
    """Run Google Gemini Vision OCR on one page image, returning layout-reconstructed JSON or markdown text."""
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model_name = (model or '').strip()
    if not model_name or model_name in ('gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-3.5-flash-lite'):
        # gemini-flash-lite-latest: fastest (~1s/page), free tier, best output completeness for Khmer
        chosen_model = 'gemini-flash-lite-latest'
    else:
        chosen_model = model_name

    prompt = _get_vision_prompt(ocr_lang=ocr_lang)

    # Convert to RGB mode
    rgb_img = pil_image.convert('RGB') if pil_image.mode != 'RGB' else pil_image

    gen_model = genai.GenerativeModel(chosen_model)
    response = gen_model.generate_content(
        [rgb_img, prompt],
        generation_config={
            'temperature': 0.0,
            'max_output_tokens': 8192,
            'response_mime_type': 'application/json',
        }
    )
    return (response.text or "").strip()


class GeminiEngine(OCREngine):
    name = 'Gemini'
    remote = True
    optional = True

    def __init__(self, api_key, model, ocr_lang):
        self.api_key, self.model, self.ocr_lang = api_key, model, ocr_lang

    def run(self, doc, page, gray):
        text = _gemini_ocr_page(self.api_key, self.model, page, ocr_lang=self.ocr_lang)
        return _add_structured_content_to_docx(doc, text) if text and text.strip() else 0
