"""Tesseract local engine with spatial layout reconstruction."""
import io
import json
import logging
import os

logger = logging.getLogger(__name__)


import numpy as np
import pytesseract
from django.conf import settings
from PIL import Image

from apps.pdf.ocr.docx_writer import _add_markdown_to_docx, _add_structured_json_to_docx
from apps.pdf.ocr.engines.base import OCREngine


def _tesseract_ocr_page_structured(doc, gray_image, tess_lang='eng'):
    """
    Spatial layout and table reconstruction engine for Tesseract OCR.
    Extracts word bounding boxes, groups words into lines and horizontal column cells,
    detects multi-column grid tables, side-by-side letterheads, centered headings,
    and right-aligned dates, then formats native Microsoft Word elements with
    Khmer OS Battambang and OpenXML Complex Script tags.
    Returns: total_chars_extracted (int)
    """
    import re
    import pytesseract
    import logging
    logger = logging.getLogger(__name__)

    w_img, h_img = gray_image.size
    data = None
    try:
        data = pytesseract.image_to_data(gray_image, lang=tess_lang, output_type=pytesseract.Output.DICT)
    except Exception as e:
        logger.warning(f'_tesseract_ocr_page_structured: image_to_data failed: {e}', exc_info=True)
        data = None

    elements = []
    if data and len(data.get('text', [])) > 0:
        n = len(data['text'])
        words = []
        for i in range(n):
            txt = str(data['text'][i]).strip()
            if not txt:
                continue
            try:
                conf = int(data['conf'][i])
            except (ValueError, TypeError):
                conf = 50
            if conf < 25:
                continue

            left = int(data['left'][i])
            top = int(data['top'][i])
            w = int(data['width'][i])
            h = int(data['height'][i])
            words.append({
                'text': txt,
                'left': left,
                'top': top,
                'right': left + w,
                'bottom': top + h,
                'width': w,
                'height': h,
                'cy': top + h / 2.0
            })

        if words:
            # Sort words vertically by center y
            words.sort(key=lambda w: w['cy'])

            # Cluster words into visual text lines
            lines = []
            curr_line = [words[0]]
            for w in words[1:]:
                prev_w = curr_line[-1]
                line_h = max(cw['height'] for cw in curr_line)
                v_diff = abs(w['cy'] - prev_w['cy'])
                if v_diff < max(8, line_h * 0.45):
                    curr_line.append(w)
                else:
                    lines.append(curr_line)
                    curr_line = [w]
            if curr_line:
                lines.append(curr_line)

            # Median word height across the page
            all_heights = [w['height'] for w in words]
            med_height = sorted(all_heights)[len(all_heights) // 2] if all_heights else 16

            # In each line, detect horizontal column gaps
            processed_lines = []
            for line in lines:
                line.sort(key=lambda w: w['left'])
                cells = []
                curr_cell_words = [line[0]]
                for w in line[1:]:
                    prev_w = curr_cell_words[-1]
                    gap = w['left'] - prev_w['right']

                    # Check if curr_cell_words is merely a bullet or list marker prefix
                    curr_prefix = " ".join(cw['text'] for cw in curr_cell_words).strip()
                    is_prefix_marker = bool(re.match(
                        r'^(?:[\u2022\u25CB\u25CF\u25AA\u25AB\u2013\u2014\-\*\+\~]|'
                        r'[0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]{1,2}[\.\)]|'
                        r'\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]{1,2}\))$',
                        curr_prefix
                    ))

                    # High gap threshold to separate true table columns (> 75px, ~3.5x line height)
                    cell_split_threshold = max(75, int(med_height * 3.5), int(w_img * 0.08))
                    if gap > cell_split_threshold and not is_prefix_marker:
                        cell_text = curr_prefix
                        if cell_text:
                            cells.append({
                                'text': cell_text,
                                'x1': curr_cell_words[0]['left'],
                                'x2': curr_cell_words[-1]['right'],
                                'y': min(cw['top'] for cw in curr_cell_words),
                                'h': max(cw['height'] for cw in curr_cell_words),
                            })
                        curr_cell_words = [w]
                    else:
                        curr_cell_words.append(w)

                if curr_cell_words:
                    cell_text = " ".join(cw['text'] for cw in curr_cell_words).strip()
                    if cell_text:
                        cells.append({
                            'text': cell_text,
                            'x1': curr_cell_words[0]['left'],
                            'x2': curr_cell_words[-1]['right'],
                            'y': min(cw['top'] for cw in curr_cell_words),
                            'h': max(cw['height'] for cw in curr_cell_words),
                        })

                if cells:
                    processed_lines.append(cells)

            # Group multi-line tables vs letterheads vs single paragraphs
            idx = 0
            num_lines = len(processed_lines)
            while idx < num_lines:
                line_cells = processed_lines[idx]

                # Multi-column line (2+ columns)
                if len(line_cells) >= 2:
                    table_rows = [line_cells]
                    j = idx + 1
                    while j < num_lines and len(processed_lines[j]) == len(line_cells):
                        match = True
                        for c_idx in range(len(line_cells)):
                            diff = abs(processed_lines[j][c_idx]['x1'] - line_cells[c_idx]['x1'])
                            if diff > max(60, int(w_img * 0.08)):
                                match = False
                                break
                        if match:
                            table_rows.append(processed_lines[j])
                            j += 1
                        else:
                            break

                    # 2+ matching rows -> Table Grid
                    if len(table_rows) >= 2:
                        elements.append({
                            'type': 'table',
                            'layout': 'grid',
                            'rows': [[c['text'] for c in r] for r in table_rows]
                        })
                        idx = j
                        continue

                    # Single multi-column row (Letterhead or Signature row) -> Borderless Table
                    elements.append({
                        'type': 'table',
                        'layout': 'borderless',
                        'rows': [[c['text'] for c in line_cells]]
                    })
                    idx += 1
                    continue

                # Single cell line
                cell = line_cells[0]
                text = cell['text'].strip()
                if not text:
                    idx += 1
                    continue

                x1, x2, h = cell['x1'], cell['x2'], cell['h']
                cx = (x1 + x2) / 2.0
                line_w = x2 - x1

                page_center = w_img / 2.0
                is_centered = abs(cx - page_center) < (w_img * 0.12) and (line_w < w_img * 0.70)
                is_right = (x2 > w_img * 0.82) and (x1 > w_img * 0.42) and (line_w < w_img * 0.55)
                is_wide = line_w > w_img * 0.68

                # ── 1. Check for List Items (Bullets, Numbers, Legal decree dashes) ──
                # Matches: •, -, *, 1., 1), (1), ១., ១), (១), ក., ក), 1.1., ១.១., - បានឃើញ
                LIST_MARKER_RE = re.compile(
                    r'^(?:'
                    r'[\u2022\u25CB\u25CF\u25AA\u25AB\u2013\u2014\-\*\+\~]\s*'     # Bullets
                    r'|[0-9\u17E0-\u17E9]+(?:\.[0-9\u17E0-\u17E9]+)+[\.\)]?\s+'   # Sub-clauses (1.1, ១.១)
                    r'|(?:[0-9\u17E0-\u17E9]+|[a-zA-Z\u1780-\u17B3])[\.\)]\s+'    # 1. or 1) or ក. or a.
                    r'|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\)\s+'             # (1) or (ក)
                    r')'
                )

                TOP_TITLE_KEYWORDS = (
                    'ព្រះរាជាណាចក្រកម្ពុជា', 'ជាតិ សាសនា ព្រះមហាក្សត្រ',
                    'KINGDOM OF CAMBODIA', 'NATION RELIGION KING', 'រាជរដ្ឋាភិបាល'
                )
                DECREE_TITLE_KEYWORDS = (
                    'សេចក្តីសម្រេច', 'សាលក្រម', 'សាលដីកា', 'ដីកាសម្រេច', 'ដីកា', 'ប្រកាស',
                    'អនុក្រឹត្យ', 'ព្រះរាជក្រម', 'សារាចរ', 'សេចក្តីជូនដំណឹង', 'សេចក្តីណែនាំ',
                    'សេចក្តីថ្លែងការណ៍', 'លិខិត', 'ពាក្យស្នើសុំ', 'ពាក្យបណ្តឹង', 'កំណត់ហេតុ',
                    'កិច្ចសន្យា', 'កិច្ចព្រមព្រៀង', 'លក្ខន្តិកៈ', 'សេចក្តីព្រាង', 'តាងនាមប្រជារាស្ត្រខ្មែរ',
                    'សម្រេច', 'បង្គាប់', 'DECISION', 'JUDGMENT', 'PRAKAS', 'SUB-DECREE',
                    'CONTRACT', 'AGREEMENT', 'CERTIFICATE', 'NOTICE', 'ORDER'
                )
                SECTION_TITLE_RE = re.compile(
                    r'^(?:មាត្រា\s*[0-9\u17E0-\u17E9]+|ជំពូក(?:\s*ទី)?\s*[0-9\u17E0-\u17E9IVXLCDM]+|'
                    r'គន្ថី\s*[0-9\u17E0-\u17E9IVXLCDM]+|ផ្នែក(?:\s*ទី)?\s*[0-9\u17E0-\u17E9]+|'
                    r'កថាភាគ\s*[0-9\u17E0-\u17E9]+|ចំណុច(?:\s*ទី)?\s*[0-9\u17E0-\u17E9]+|'
                    r'Article\s*[0-9]+|Chapter\s*[0-9IVXLCDM]+|Section\s*[0-9]+)',
                    re.IGNORECASE
                )

                if LIST_MARKER_RE.match(text):
                    is_sub = bool(re.match(r'^(?:[0-9\u17E0-\u17E9]+\.[0-9\u17E0-\u17E9]+|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\))', text))
                    is_num = bool(re.match(r'^(?:[0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+[\.\)]|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\))', text))
                    elements.append({
                        'type': 'list_item',
                        'list_type': 'number' if is_num else 'bullet',
                        'sub': is_sub,
                        'text': text
                    })
                elif any(k in text for k in TOP_TITLE_KEYWORDS):
                    elements.append({
                        'type': 'heading',
                        'level': 1,
                        'align': 'center',
                        'text': text
                    })
                elif any(k in text for k in DECREE_TITLE_KEYWORDS) and len(text) < 80:
                    elements.append({
                        'type': 'heading',
                        'level': 2,
                        'align': 'center' if (is_centered or len(text) < 45) else 'left',
                        'text': text
                    })
                elif SECTION_TITLE_RE.match(text):
                    elements.append({
                        'type': 'heading',
                        'level': 2,
                        'align': 'center' if is_centered else 'left',
                        'text': text
                    })
                elif (h >= med_height * 1.25) and (line_w < w_img * 0.75) and len(text) < 85:
                    elements.append({
                        'type': 'heading',
                        'level': 2,
                        'align': 'center' if is_centered else 'left',
                        'text': text
                    })
                elif is_centered and len(text) < 65:
                    elements.append({
                        'type': 'paragraph',
                        'align': 'center',
                        'text': text
                    })
                elif is_right or (re.search(r'(?:រាជធានីភ្នំពេញ|ភ្នំពេញ|ធ្វើនៅ|ថ្ងៃទី)\s*,?\s*ថ្ងៃទី\s*[0-9\u17E0-\u17E9]+\s*ខែ', text) and line_w < w_img * 0.65):
                    elements.append({
                        'type': 'paragraph',
                        'align': 'right',
                        'text': text
                    })
                elif is_wide:
                    elements.append({
                        'type': 'paragraph',
                        'align': 'justify',
                        'text': text
                    })
                else:
                    elements.append({
                        'type': 'paragraph',
                        'align': 'left',
                        'text': text
                    })
                idx += 1

    if elements:
        c_count = _add_structured_json_to_docx(doc, elements)
        if c_count > 0:
            return c_count

    # Fallback to image_to_string if spatial clustering found no words
    try:
        raw_text = pytesseract.image_to_string(gray_image, lang=tess_lang, config='--psm 6')
        clean_raw = raw_text.strip()
        if clean_raw:
            return _add_markdown_to_docx(doc, clean_raw)
    except Exception as e:
        logger.error(f'_tesseract_ocr_page_structured: image_to_string fallback failed: {e}', exc_info=True)

    return 0


class TesseractEngine(OCREngine):
    name = 'Tesseract'

    def __init__(self, tess_lang):
        self.tess_lang = tess_lang

    def run(self, doc, page, gray):
        return _tesseract_ocr_page_structured(doc, gray, tess_lang=self.tess_lang)
