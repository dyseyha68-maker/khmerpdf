"""Claude (Anthropic) vision OCR engine."""
import io

from apps.pdf.ocr.docx_writer import _add_structured_content_to_docx
from apps.pdf.ocr.engines.base import OCREngine
from apps.pdf.ocr.engines.prompt import _get_vision_prompt


def _claude_ocr_page(api_key, model, pil_image, ocr_lang='eng'):
    """Run Claude Vision OCR on one page image, returning layout-reconstructed markdown or JSON text."""
    import base64
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    buf = io.BytesIO()
    # JPEG format at quality 92 provides crisp clarity for OCR with low payload size
    pil_image.convert('RGB').save(buf, format='JPEG', quality=92)
    image_base64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    prompt = _get_vision_prompt(ocr_lang=ocr_lang)

    # Normalize model string to ensure valid Anthropic model identifier
    chosen_model = (model or '').strip()
    if not chosen_model or 'sonnet' in chosen_model.lower():
        chosen_model = 'claude-3-5-sonnet-20241022'
    elif 'haiku' in chosen_model.lower():
        chosen_model = 'claude-3-5-haiku-20241022'

    create_kwargs = {
        'model': chosen_model,
        'max_tokens': 8192,
        'messages': [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_base64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    }

    response = client.messages.create(**create_kwargs)

    text_parts = []
    for block in response.content:
        if getattr(block, 'text', None):
            text_parts.append(block.text)

    return "\n".join(text_parts).strip()


class ClaudeEngine(OCREngine):
    name = 'Claude'
    remote = True
    optional = True

    def __init__(self, api_key, model, ocr_lang):
        self.api_key, self.model, self.ocr_lang = api_key, model, ocr_lang

    def run(self, doc, page, gray):
        text = _claude_ocr_page(self.api_key, self.model, page, ocr_lang=self.ocr_lang)
        return _add_structured_content_to_docx(doc, text) if text and text.strip() else 0
