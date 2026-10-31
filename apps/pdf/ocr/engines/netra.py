"""Netra-OCR local engine (YOLO line detection + CNN-Transformer recognizer)."""
import json
import logging
import os

logger = logging.getLogger(__name__)


from apps.pdf.ocr.docx_writer import _add_structured_json_to_docx
from apps.pdf.ocr.engines.base import OCREngine


def _reconstruct_netra_spatial_elements(segments, w_img, h_img):
    """
    High-fidelity spatial layout analysis for Netra-OCR detections.
    Groups YOLO bounding boxes into structured document elements:
    1. Multi-column letterhead & top headers -> Borderless Word tables.
    2. National mottos and decree titles -> centered bold headings.
    3. Multi-line body paragraphs -> Coalesced justified paragraphs.
    4. Bullet lists and legal labels -> Hanging indents.
    5. Signature & Carbon Copy (ចម្លងជូន) blocks -> Multi-column borderless tables.
    """
    import re

    valid_segs = []
    for s in segments:
        bbox = s.get('bbox')
        if not bbox or len(bbox) != 4:
            continue
        x1, y1, x2, y2 = bbox
        stype = s.get('type')
        text = (s.get('text') or '').strip()
        if stype != 'logo' and not text:
            continue

        # Clean label punctuation (e.g. "កម្មវត្ថុ ៖", "យោង ៖", "ចម្លងជូន ៖")
        if text:
            text = re.sub(r'^(កម្មវត្ថុ|យោង|ចម្លងជូន)\s*[\]\[\+\-\:\s]*([៖:])?', r'**\1 ៖** ', text).strip()
            if text.startswith('-') and len(text) > 1 and text[1] != ' ':
                text = '- ' + text[1:]

        valid_segs.append({
            'type': stype,
            'text': text,
            'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
            'cx': (x1 + x2) / 2.0,
            'cy': (y1 + y2) / 2.0,
            'w': x2 - x1,
            'h': y2 - y1,
            'crop': s.get('crop')
        })

    if not valid_segs:
        return []

    valid_segs.sort(key=lambda s: s['cy'])
    elements = []

    # ── 1. Top Letterhead Zone (cy < 0.22 * h_img) ──
    HEADER_KW = ('ព្រះរាជាណាចក្រ', 'ជាតិ សាសនា', 'គណៈមេធាវី', 'ក្រសួង', 'ការិយាល័យ', 'លេខ', 'រាជរដ្ឋាភិបាល')
    header_segs = []
    rest_segs = []

    for s in valid_segs:
        if s['cy'] < (0.22 * h_img) and (any(k in s['text'] for k in HEADER_KW) or s['type'] == 'logo' or s['x1'] > (0.65 * w_img)):
            header_segs.append(s)
        else:
            rest_segs.append(s)

    if header_segs:
        left_h = [s for s in header_segs if s['cx'] < (0.42 * w_img) and s['text']]
        center_h = [s for s in header_segs if (0.42 * w_img) <= s['cx'] <= (0.68 * w_img) and s['text']]
        right_h = [s for s in header_segs if s['cx'] > (0.68 * w_img) and s['text']]

        # If 2 or 3 distinct horizontal columns exist at top
        if left_h and (center_h or right_h):
            left_txt = '\n'.join(s['text'] for s in left_h)
            right_parts = []
            if center_h:
                right_parts.append('\n'.join(s['text'] for s in center_h))
            if right_h:
                right_parts.append('\n'.join(s['text'] for s in right_h))
            right_txt = '\n\n'.join(right_parts)

            elements.append({
                'type': 'table',
                'layout': 'borderless',
                'rows': [[
                    {'text': left_txt, 'align': 'center', 'bold': False},
                    {'text': right_txt, 'align': 'center', 'bold': False}
                ]]
            })
        else:
            for s in header_segs:
                if s['text']:
                    is_muol = any(k in s['text'] for k in ('ព្រះរាជាណាចក្រ', 'ជាតិ សាសនា'))
                    elements.append({
                        'type': 'heading' if is_muol else 'paragraph',
                        'level': 1 if is_muol else None,
                        'align': 'center' if is_muol else 'left',
                        'text': s['text']
                    })

    # ── 2. Split Tail Zone (Date, Signatures, CC in bottom half) vs Body Zone ──
    body_segs = []
    tail_segs = []
    in_tail = False

    for s in rest_segs:
        txt = s['text']
        is_date = (re.search(r'(?:រាជធានី|ភ្នំពេញ|ធ្វើនៅ|ថ្ងៃទី)\s*,?\s*ថ្ងៃ', txt) or re.search(r'ថ្ងៃ[^\s]+\s+[0-9\u17E0-\u17E9]+[រោចកើត]', txt)) and s['cy'] > (0.48 * h_img)
        is_cc = 'ចម្លងជូន' in txt and s['cy'] > (0.55 * h_img)

        if is_date or is_cc:
            in_tail = True

        if in_tail:
            tail_segs.append(s)
        else:
            body_segs.append(s)

    # ── 3. Process Body Segments with Smart Multi-line Paragraph Flow ──
    coalesced_body = []
    idx = 0
    while idx < len(body_segs):
        curr = body_segs[idx]
        txt = curr['text']
        if not txt:
            idx += 1
            continue

        is_bullet = txt.startswith(('-', '–', '—', '•', '*', '+')) or bool(re.match(r'^(?:[0-9\u17E0-\u17E9]+|[a-zA-Z\u1780-\u17B3])[\.\)]\s+', txt))
        is_label = bool(re.match(r'^\*\*(?:កម្មវត្ថុ|យោង|ចម្លងជូន)\s*៖\*\*', txt))
        is_salutation = any(k in txt for k in ('សូមគោរពជូន', 'គោរពជូន', 'ឯកឧត្តម', 'លោកជំទាវ', 'លោកប្រធាន')) and len(txt) < 80
        is_marginalia = (curr['x1'] > 0.68 * w_img) and (curr['w'] < 0.32 * w_img) and (not is_bullet) and (len(txt) < 40)

        # Lookahead: coalesce continuation lines of same paragraph
        j = idx + 1
        full_text = txt
        while j < len(body_segs) and not is_salutation and not is_marginalia:
            nxt = body_segs[j]
            nxt_txt = nxt['text']
            nxt_bullet = nxt_txt.startswith(('-', '–', '—', '•', '*', '+')) or bool(re.match(r'^(?:[0-9\u17E0-\u17E9]+|[a-zA-Z\u1780-\u17B3])[\.\)]\s+', nxt_txt))
            nxt_label = bool(re.match(r'^\*\*(?:កម្មវត្ថុ|យោង|ចម្លងជូន)\s*៖\*\*', nxt_txt))
            nxt_salutation = any(k in nxt_txt for k in ('សូមគោរពជូន', 'គោរពជូន', 'ឯកឧត្តម', 'លោកជំទាវ', 'លោកប្រធាន')) and len(nxt_txt) < 80
            nxt_marginalia = (nxt['x1'] > 0.68 * w_img) and (nxt['w'] < 0.32 * w_img) and (not nxt_bullet) and (len(nxt_txt) < 40)

            # If current ends with Khmer terminal '។' or '៖', do not merge
            if full_text.rstrip().endswith(('។', '៖', ':', '.')):
                break

            if nxt_bullet or nxt_label or nxt_salutation or nxt_marginalia:
                break

            full_text = full_text + ' ' + nxt_txt
            j += 1

        coalesced_body.append({
            'text': full_text,
            'is_bullet': is_bullet,
            'is_label': is_label,
            'is_salutation': is_salutation,
            'is_marginalia': is_marginalia,
            'cx': curr['cx'],
            'w': curr['w']
        })
        idx = j

    # Output body elements
    for b in coalesced_body:
        txt = b['text']
        if b['is_salutation']:
            clean_s = txt.strip().strip('*').strip()
            elements.append({
                'type': 'paragraph',
                'align': 'center',
                'text': f"**{clean_s}**"
            })
        elif b['is_bullet']:
            elements.append({
                'type': 'list_item',
                'text': txt
            })
        elif b['is_label']:
            elements.append({
                'type': 'paragraph',
                'align': 'left',
                'text': txt
            })
        elif b['is_marginalia']:
            elements.append({
                'type': 'paragraph',
                'align': 'right',
                'text': txt
            })
        elif len(txt) > 75 or b['w'] > (0.55 * w_img):
            elements.append({
                'type': 'paragraph',
                'align': 'justify',
                'text': txt
            })
        else:
            elements.append({
                'type': 'paragraph',
                'align': 'left',
                'text': txt
            })

    # ── 4. Process Tail Zone (CC on left, Signature on right) ──
    if tail_segs:
        left_tail = [s for s in tail_segs if s['cx'] < (0.45 * w_img) and s['text']]
        right_tail = [s for s in tail_segs if s['cx'] >= (0.45 * w_img) and s['text']]

        if left_tail and right_tail:
            left_lines = [s['text'] for s in left_tail]
            right_lines = [s['text'] for s in right_tail]

            elements.append({
                'type': 'table',
                'layout': 'borderless',
                'rows': [[
                    {'text': '\n'.join(left_lines), 'align': 'left', 'bold': False},
                    {'text': '\n'.join(right_lines), 'align': 'center', 'bold': False}
                ]]
            })
        else:
            for s in tail_segs:
                if s['text']:
                    elements.append({
                        'type': 'paragraph',
                        'align': 'right' if s['x1'] > (0.45 * w_img) else 'left',
                        'text': s['text']
                    })

    return elements


def _netra_segments(pil_image):
    """Run Netra on one page image and return its detected text-line segments
    (dicts with 'type', 'bbox', 'text'); [] if nothing was detected."""
    import os
    import tempfile

    # Convert to RGB for YOLO detection
    if pil_image.mode != 'RGB':
        pil_image = pil_image.convert('RGB')

    # Lazy-init singleton pipeline to avoid reloading models on every page
    if not hasattr(_netraocr_page, '_pipeline'):
        from netra_ocr.ocr_engine import KhmerOCRPipeline
        _netraocr_page._pipeline = KhmerOCRPipeline(detector='yolo', conf=0.4)

    pipeline = _netraocr_page._pipeline

    # Save to temporary image file for pipeline processing
    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
        tmp_path = tmp.name
        pil_image.save(tmp_path, format='JPEG', quality=95)

    # Errors propagate: the pipeline logs them under the engine name and falls through.
    try:
        _, meta = pipeline.process_image(
            image_path=tmp_path,
            batch_size=16,
            save_debug=False,
            return_segments=True
        )
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return meta.get('segments', []) if isinstance(meta, dict) else []


def netra_page_elements(pil_image):
    """Netra segments grouped into structured document elements (same schema
    the Claude/Gemini prompt returns). Used by the layout scorer."""
    segments = _netra_segments(pil_image)
    if not segments:
        return []
    w_img, h_img = pil_image.size
    return _reconstruct_netra_spatial_elements(segments, w_img, h_img)


def _netraocr_page(doc, pil_image, ocr_lang='khm'):
    """
    Run Netra-OCR (CNN-Transformer) on a single page PIL image.
    Netra-OCR is a Squeeze-and-Excitation Transformer trained on Khmer script with
    YOLO-based text-line detection + Khmer Character Cluster tokenizer recognition.
    Automatically reconstructs spatial layout and formats native Microsoft Word (.docx)
    elements using Khmer OS Battambang and OpenXML tags.
    Returns: total_chars_extracted (int)
    """
    import re
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn

    segments = _netra_segments(pil_image)
    if not segments:
        return 0

    # ── High-Fidelity Spatial Layout Reconstruction ──
    w_img, h_img = pil_image.size
    elements = _reconstruct_netra_spatial_elements(segments, w_img, h_img)
    if elements:
        c_count = _add_structured_json_to_docx(doc, elements)
        if c_count > 0:
            return c_count

    # Fallback to sequential paragraph generation if spatial clustering yielded no elements
    total_chars = 0

    for seg in segments:
        if seg.get('type') != 'text':
            continue

        text = (seg.get('text') or '').strip()
        if not text:
            continue

        has_khmer = any('\u1780' <= c <= '\u17FF' or '\u19E0' <= c <= '\u19FF' for c in text)
        font_name = 'Khmer OS Battambang' if has_khmer else 'Calibri'
        font_size = 11.0
        is_bold = False

        # Detect motto/heading lines heuristically (all-caps Khmer or short centered text)
        is_moul = False
        if has_khmer and len(text) < 50:
            moul_keywords = ('ព្រះរាជាណាចក្រ', 'ជាតិ សាសនា', 'Kingdom of Cambodia', 'AVOCAT', 'ATTORNEY')
            if any(kw in text for kw in moul_keywords):
                is_moul = True

        p = doc.add_paragraph()
        if is_moul:
            font_name = 'Khmer OS Battambang' if has_khmer else 'Calibri'
            font_size = 13.0
            is_bold = True
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.2
        else:
            # Smart alignment heuristics
            if re.search(r'ថ្ងៃទី\s*[0-9\u17E0-\u17E9]+\s*ខែ', text) and len(text) < 70:
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif len(text) > 75:
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            else:
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.15

        run = p.add_run(text)
        run.bold = is_bold
        run.font.name = font_name
        run.font.size = Pt(font_size)

        if has_khmer:
            rPr = run._element.get_or_add_rPr()
            rFonts = rPr.get_or_add_rFonts()
            rFonts.set(qn('w:cs'), font_name)
            rFonts.set(qn('w:ascii'), font_name)
            rFonts.set(qn('w:hAnsi'), font_name)
            rFonts.set(qn('w:eastAsia'), font_name)

        total_chars += len(text)

    return total_chars


class NetraEngine(OCREngine):
    name = 'Netra-OCR'

    def __init__(self, ocr_lang):
        self.ocr_lang = ocr_lang

    def run(self, doc, page, gray):
        return _netraocr_page(doc, page, ocr_lang=self.ocr_lang)
