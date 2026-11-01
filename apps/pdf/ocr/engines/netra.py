"""Netra-OCR local engine (YOLO line detection + CNN-Transformer recognizer)."""
import json
import logging
import os

logger = logging.getLogger(__name__)


from apps.pdf.ocr.docx_writer import _add_structured_json_to_docx
from apps.pdf.ocr.engines.base import OCREngine


def _line_stroke(gray, bbox):
    """Ink stroke thickness (px) inside a line box: 2*area/perimeter of the binarised ink.
    Bold text has thicker strokes. None if it can't be measured."""
    try:
        import cv2
        x1, y1, x2, y2 = [int(v) for v in bbox]
        crop = gray[max(0, y1):y2, max(0, x1):x2]
        if crop.size == 0:
            return None
        _, bw = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        area = int((bw > 0).sum())
        contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        perimeter = sum(cv2.arcLength(c, True) for c in contours)
        return 2.0 * area / perimeter if perimeter else None
    except Exception:
        return None


# Detector boxes include roughly constant padding, so box height is not proportional to font size.
# Calibrated on Khmer OS Battambang renders: box_pt ~= BOX_SLOPE * font_pt + BOX_PAD_PT.
BOX_SLOPE, BOX_PAD_PT = 1.48, 6.9
BOLD_RATIO = 1.45        # stroke thickness vs the page's body text
MIN_LINE_RATIO = 1.55    # exact line pitch never below this multiple of the font size (Khmer subscripts)


def _median(values, default=None):
    values = sorted(v for v in values if v is not None)
    if not values:
        return default
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def _page_metrics(valid_segs, gray, w_img, dpi):
    """Measure body-text size, line pitch, left margin and stroke weight for a page.
    Returns None when there are too few lines to trust."""
    text_segs = [s for s in valid_segs if s['type'] != 'logo' and s['text']]
    if len(text_segs) < 4:
        return None
    ptpp = 72.0 / dpi if dpi else 595.3 / w_img          # points per pixel
    for s in text_segs:
        s['stroke_rel'] = None
        if gray is not None:
            st = _line_stroke(gray, (s['x1'], s['y1'], s['x2'], s['y2']))
            s['stroke_rel'] = (st / s['h']) if st and s['h'] else None

    body_h = _median([s['h'] for s in text_segs])
    ordered = sorted(text_segs, key=lambda s: s['cy'])
    diffs = [b['cy'] - a['cy'] for a, b in zip(ordered, ordered[1:]) if 0.5 * body_h < b['cy'] - a['cy'] < 2.2 * body_h]
    pitch = _median(diffs)
    if not pitch:
        return None
    body_pt = _median([((body_h * ptpp) - BOX_PAD_PT) / BOX_SLOPE], 11.0)
    return {
        'ptpp': ptpp,
        'body_h': body_h,
        'pitch': pitch,
        'left': _median([s['x1'] for s in text_segs]),
        'body_pt': min(13.0, max(9.0, round(body_pt * 2) / 2.0)),
        'body_stroke': _median([s['stroke_rel'] for s in text_segs]),
    }


def _line_font_pt(h_px, m):
    """Font size of a line relative to the body text (padding-corrected), snapped to 0.5pt."""
    body_box = m['body_h'] * m['ptpp'] - BOX_PAD_PT
    if body_box <= 0:
        return m['body_pt']
    size = m['body_pt'] * (h_px * m['ptpp'] - BOX_PAD_PT) / body_box
    return min(24.0, max(7.0, round(size * 2) / 2.0))


def _paragraph_format(lines, m, w_img, prev_last_cy):
    """Formatting measured from the original lines of one paragraph (see docx_writer)."""
    fmt = {}
    pt = lambda px: px * m['ptpp']
    line_h = _median([l['h'] for l in lines])
    size = _line_font_pt(line_h, m)
    if abs(size - m['body_pt']) < 1.6:      # box heights jitter with subscripts: snap near-body sizes to body
        size = m['body_pt']
    size = round(max(size, 0.72 * m['body_pt']) * 2) / 2.0   # footnotes shrink, but not below readable
    fmt['font_size'] = size

    rels = [l.get('stroke_rel') for l in lines]
    if m['body_stroke'] and _median(rels) and _median(rels) / m['body_stroke'] >= BOLD_RATIO:
        fmt['bold'] = True

    cys = sorted(l['cy'] for l in lines)
    steps = [b - a for a, b in zip(cys, cys[1:]) if b - a > 0.5 * m['body_h']]
    # single-line paragraphs have no measured pitch: scale the page's by their own line height
    pitch_px = _median(steps, m['pitch'] * min(1.6, max(0.6, line_h / m['body_h'])))
    fmt['line_pitch'] = round(max(pt(pitch_px), MIN_LINE_RATIO * size) * 2) / 2.0

    if prev_last_cy is not None:
        gap = pt(cys[0] - prev_last_cy - pitch_px)
        fmt['space_before'] = round(min(36.0, gap) * 2) / 2.0 if gap >= 2.0 else 0.0

    if len(lines) >= 2:
        first = min(lines, key=lambda l: l['cy'])
        rest = _median([l['x1'] for l in lines if l is not first], first['x1'])
        indent = pt(first['x1'] - rest)
        if 8.0 <= indent <= 72.0:
            fmt['first_line_indent'] = round(indent)
    return fmt


def _apply_measured_format(el, b, m, w_img, prev_last_cy):
    """Copy measured size/bold/spacing/indent onto an element; promote a short bold
    single line (title / section heading) to a heading."""
    fmt = _paragraph_format(b['lines'], m, w_img, prev_last_cy)
    if el['type'] == 'paragraph' and fmt.get('bold') and len(b['lines']) == 1 \
            and not b['is_label'] and not b['is_marginalia'] and len(el['text']) < 120:
        line = b['lines'][0]
        centered = abs(line['cx'] - w_img / 2.0) < 0.05 * w_img
        el['type'] = 'heading'
        el['level'] = 1 if fmt.get('font_size', m['body_pt']) >= 1.4 * m['body_pt'] else 2
        el['align'] = 'center' if centered else 'left'
    if el['type'] == 'list_item':
        fmt.pop('first_line_indent', None)  # list items keep their hanging indent
    el.update(fmt)


def _reconstruct_netra_spatial_elements(segments, w_img, h_img, gray=None, dpi=None):
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
    metrics = _page_metrics(valid_segs, gray, w_img, dpi)
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
        para_lines = [curr]
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

            # Original layout: a larger vertical gap or an indented first line starts a new paragraph
            if metrics:
                if nxt['cy'] - para_lines[-1]['cy'] > 1.35 * metrics['pitch']:
                    break
                if nxt['x1'] - metrics['left'] > 0.03 * w_img:
                    break

            full_text = full_text + ' ' + nxt_txt
            para_lines.append(nxt)
            j += 1

        coalesced_body.append({
            'text': full_text,
            'is_bullet': is_bullet,
            'is_label': is_label,
            'is_salutation': is_salutation,
            'is_marginalia': is_marginalia,
            'cx': curr['cx'],
            'w': curr['w'],
            'lines': para_lines,
        })
        idx = j

    # Output body elements
    prev_last_cy = None
    for b in coalesced_body:
        txt = b['text']
        n_before = len(elements)
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

        if metrics and len(elements) > n_before:
            _apply_measured_format(elements[-1], b, metrics, w_img, prev_last_cy)
        prev_last_cy = max(l['cy'] for l in b['lines'])

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


def _get_pipeline():
    """Lazy singleton so the models load once per process, not once per page."""
    if not hasattr(_netraocr_page, '_pipeline'):
        from netra_ocr.ocr_engine import KhmerOCRPipeline
        _netraocr_page._pipeline = KhmerOCRPipeline(detector='yolo', conf=0.4)
    return _netraocr_page._pipeline


def warm_up():
    """Load the Netra models now (used by the OCR worker at startup)."""
    _get_pipeline()


def _netra_segments(pil_image):
    """Run Netra on one page image and return its detected text-line segments
    (dicts with 'type', 'bbox', 'text'); [] if nothing was detected."""
    import os
    import tempfile

    # Convert to RGB for YOLO detection
    if pil_image.mode != 'RGB':
        pil_image = pil_image.convert('RGB')

    pipeline = _get_pipeline()

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


def _measure_kwargs(pil_image):
    """Pixels + DPI for measuring formatting (DPI is set by _iter_document_pages)."""
    import numpy as np
    dpi = (pil_image.info.get('dpi') or (None,))[0]
    return {'gray': np.array(pil_image.convert('L')), 'dpi': dpi if dpi and dpi > 0 else None}


def netra_page_elements(pil_image):
    """Netra segments grouped into structured document elements (same schema
    the Claude/Gemini prompt returns). Used by the layout scorer."""
    segments = _netra_segments(pil_image)
    if not segments:
        return []
    return _reconstruct_netra_spatial_elements(segments, *pil_image.size, **_measure_kwargs(pil_image))


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
    elements = _reconstruct_netra_spatial_elements(segments, w_img, h_img, **_measure_kwargs(pil_image))
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
