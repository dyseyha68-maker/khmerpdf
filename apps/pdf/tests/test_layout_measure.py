"""Formatting measured from detector boxes: line rebuilding, paragraph breaks, spacing, indent.

Uses fabricated Netra segments (no model needed). Page is 1200x3000px at 200 dpi (0.36 pt/px).
"""
from django.test import SimpleTestCase

from apps.pdf.ocr.engines.netra import (
    _khmer_share, _merge_line_pieces, _reconstruct_netra_spatial_elements,
)

W, H, DPI = 1200, 3000, 200
LEFT, RIGHT, LINE_H, PITCH = 100, 1100, 60, 64


def seg(x1, x2, cy, text, h=LINE_H):
    return {'type': 'text', 'text': text, 'bbox': [x1, cy - h // 2, x2, cy + h // 2]}


def line(cy, text='ក្នុងករណីដែលកូន', indent=0, pieces=1, duplicate=False):
    """One printed line, optionally detected as several boxes (+ an overlapping duplicate)."""
    x1 = LEFT + indent
    width = RIGHT - x1
    out = []
    if pieces == 1:
        out.append(seg(x1, RIGHT, cy, text))
    else:
        cut = x1 + width // 2
        out.append(seg(x1, cut, cy, text[:len(text) // 2]))
        out.append(seg(cut + 40, RIGHT, cy + 2, text[len(text) // 2:]))
    if duplicate:
        out.append(seg(x1 + 8, x1 + width // 3, cy + 1, text[:3]))
    return out


def page(paragraphs, pieces=1, duplicate=False):
    """paragraphs: list of (n_lines, indent_px, gap_px_before). Returns the detector segments."""
    segs, cy = [], 900
    for n, indent, gap in paragraphs:
        cy += gap
        for i in range(n):
            segs += line(cy, indent=indent if i == 0 else 0, pieces=pieces, duplicate=duplicate)
            cy += PITCH
    return segs


def elements(paragraphs, **kw):
    return _reconstruct_netra_spatial_elements(page(paragraphs, **kw), W, H, gray=None, dpi=DPI)


class MergeLinePiecesTests(SimpleTestCase):
    def test_split_line_and_duplicate_become_one_line(self):
        segs = line(900, pieces=2, duplicate=True) + line(964)
        merged = _merge_line_pieces([dict(s, x1=s['bbox'][0], y1=s['bbox'][1], x2=s['bbox'][2], y2=s['bbox'][3],
                                          cx=(s['bbox'][0] + s['bbox'][2]) / 2, cy=(s['bbox'][1] + s['bbox'][3]) / 2,
                                          w=s['bbox'][2] - s['bbox'][0], h=s['bbox'][3] - s['bbox'][1]) for s in segs],
                                    LINE_H, W)
        self.assertEqual(len(merged), 2)
        self.assertEqual(round(merged[0]['x1']), LEFT)
        self.assertEqual(round(merged[0]['x2']), RIGHT)


class ParagraphMeasureTests(SimpleTestCase):
    PARAS = [(4, 60, 0), (3, 0, 30)]   # indented first paragraph; second after an extra 30px gap

    def test_two_paragraphs_with_measured_indent_pitch_and_gap(self):
        els = elements(self.PARAS)
        self.assertEqual(len(els), 2)
        first, second = els
        self.assertAlmostEqual(first['first_line_indent'], 60 * 0.36, delta=1)    # ~22pt
        self.assertNotIn('first_line_indent', second)
        self.assertAlmostEqual(first['line_pitch'], PITCH * 0.36, delta=1)        # ~23pt
        self.assertAlmostEqual(second['space_before'], 30 * 0.36, delta=1.5)      # ~11pt

    def test_lines_detected_in_pieces_give_the_same_result(self):
        plain = elements(self.PARAS)
        split = elements(self.PARAS, pieces=2, duplicate=True)
        self.assertEqual(len(split), len(plain))
        for a, b in zip(plain, split):
            self.assertAlmostEqual(a['line_pitch'], b['line_pitch'], delta=1)
            self.assertAlmostEqual(a.get('space_before', 0), b.get('space_before', 0), delta=1.5)
            self.assertEqual(a.get('first_line_indent'), b.get('first_line_indent'))

    def test_full_width_line_ending_with_sentence_mark_does_not_end_paragraph(self):
        segs = page([(4, 0, 0)])
        segs[1]['text'] += '។'                       # a full-width line that happens to end a sentence
        els = _reconstruct_netra_spatial_elements(segs, W, H, gray=None, dpi=DPI)
        self.assertEqual(len(els), 1)

    def test_short_last_line_with_sentence_mark_ends_the_paragraph(self):
        segs = page([(3, 0, 0), (3, 0, 0)])
        segs[2] = seg(LEFT, LEFT + 300, segs[2]['bbox'][1] + LINE_H // 2, 'ចប់ហើយ។')   # short last line
        els = _reconstruct_netra_spatial_elements(segs, W, H, gray=None, dpi=DPI)
        self.assertEqual(len(els), 2)


class HelperTests(SimpleTestCase):
    def test_khmer_share(self):
        self.assertGreater(_khmer_share('ក្នុងករណី'), 0.9)
        self.assertEqual(_khmer_share('Consumer protection'), 0.0)
        self.assertEqual(_khmer_share(''), 0.0)
