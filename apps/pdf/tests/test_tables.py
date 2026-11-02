"""Ruled-table detection for the line-based OCR path: grid -> cells -> Word table.

Everything is synthetic (grids drawn with OpenCV, fabricated text boxes), so no model is needed.
"""
import cv2
import numpy as np
from django.test import SimpleTestCase
from docx import Document

from apps.pdf.ocr.docx_writer import _add_structured_json_to_docx
from apps.pdf.ocr.engines import netra
from apps.pdf.ocr.tables import detect_grid_tables, fill_table

W, H = 1000, 1400
GRID_X = [100, 300, 600, 900]          # 3 columns
GRID_Y = [300, 400, 500, 600, 700]     # 4 rows


def page_with_grid(xs=GRID_X, ys=GRID_Y):
    img = np.full((H, W), 255, dtype=np.uint8)
    for y in ys:
        cv2.line(img, (xs[0], y), (xs[-1], y), 0, 3)
    for x in xs:
        cv2.line(img, (x, ys[0]), (x, ys[-1]), 0, 3)
    return img


def box(x1, y1, x2, y2, text):
    return {'type': 'text', 'text': text, 'bbox': [x1, y1, x2, y2]}


def cell_box(col, row, text, dx=0, dy=0):
    return box(GRID_X[col] + 20 + dx, GRID_Y[row] + 30 + dy, GRID_X[col + 1] - 20, GRID_Y[row + 1] - 30, text)


class DetectTests(SimpleTestCase):
    def test_finds_the_grid_and_its_lines(self):
        tables = detect_grid_tables(page_with_grid())
        self.assertEqual(len(tables), 1)
        t = tables[0]
        self.assertEqual((len(t['xs']) - 1, len(t['ys']) - 1), (3, 4))          # 3 columns x 4 rows
        for found, expected in zip(t['xs'], GRID_X):
            self.assertAlmostEqual(found, expected, delta=3)
        for found, expected in zip(t['ys'], GRID_Y):
            self.assertAlmostEqual(found, expected, delta=3)

    def test_a_plain_box_around_text_is_not_a_table(self):
        self.assertEqual(detect_grid_tables(page_with_grid(xs=[100, 900], ys=[300, 700])), [])

    def test_a_blank_page_has_no_table(self):
        self.assertEqual(detect_grid_tables(np.full((H, W), 255, dtype=np.uint8)), [])

    def test_a_grid_with_a_gap_in_a_column_line_is_still_found(self):
        img = page_with_grid()
        cv2.rectangle(img, (595, 450), (605, 520), 255, -1)       # a stamp / smudge breaking one vertical line
        self.assertEqual(len(detect_grid_tables(img)), 1)


class FillTests(SimpleTestCase):
    def table(self):
        return detect_grid_tables(page_with_grid())[0]

    def test_text_goes_into_the_cell_that_contains_its_centre(self):
        items = [cell_box(0, 0, 'No'), cell_box(1, 0, 'Name'), cell_box(2, 0, 'Age'),
                 cell_box(0, 1, '1'), cell_box(1, 1, 'Sok'), cell_box(2, 1, '30'), cell_box(1, 2, 'Dara')]
        rows, used, widths = fill_table(self.table(), items)
        self.assertEqual(rows[0], ['No', 'Name', 'Age'])
        self.assertEqual(rows[1], ['1', 'Sok', '30'])
        self.assertEqual(rows[2], ['', 'Dara', ''])
        self.assertEqual(len(used), 7)
        self.assertEqual([round(w) for w in widths], [200, 300, 300])

    def test_empty_rows_and_columns_are_dropped(self):
        rows, _, widths = fill_table(self.table(), [cell_box(0, 0, 'a'), cell_box(0, 2, 'b')])
        self.assertEqual(rows, [['a'], ['b']])
        self.assertEqual(len(widths), 1)

    def test_duplicate_overlapping_boxes_are_read_once(self):
        items = [cell_box(1, 1, 'Kim Leng'), cell_box(1, 1, 'Kim', dx=4, dy=2)]
        rows, _, _ = fill_table(self.table(), items)
        self.assertEqual(rows[0], ['Kim Leng'])

    def test_a_box_spanning_two_columns_does_not_repeat_the_neighbour_text(self):
        straddling = box(GRID_X[0] + 10, GRID_Y[1] + 30, GRID_X[2] - 20, GRID_Y[2] - 30, '12 Sok Dara')   # centre in column 2
        rows, _, _ = fill_table(self.table(), [cell_box(0, 1, '12'), straddling])
        self.assertEqual(rows[0], ['12', 'Sok Dara'])

    def test_bracket_noise_from_the_ruling_is_stripped(self):
        rows, _, _ = fill_table(self.table(), [cell_box(1, 0, ' ] Sok [ '), cell_box(2, 0, '| 30 |')])
        self.assertEqual(rows[0], ['Sok', '30'])

    def test_two_lines_in_one_cell_stay_two_lines(self):
        items = [box(GRID_X[1] + 20, GRID_Y[0] + 10, GRID_X[2] - 20, GRID_Y[0] + 45, 'first'),
                 box(GRID_X[1] + 20, GRID_Y[0] + 55, GRID_X[2] - 20, GRID_Y[0] + 90, 'second')]
        rows, _, _ = fill_table(self.table(), items)
        self.assertEqual(rows[0][0], 'first\nsecond')


class ReconstructTests(SimpleTestCase):
    def segments(self):
        return [
            box(200, 120, 800, 170, 'Title above the table'),
            cell_box(0, 0, 'No'), cell_box(1, 0, 'Name'), cell_box(2, 0, 'Age'),
            cell_box(0, 1, '1'), cell_box(1, 1, 'Sok'), cell_box(2, 1, '30'),
            cell_box(0, 2, '2'), cell_box(1, 2, 'Dara'), cell_box(2, 2, '25'),
            box(200, 900, 800, 950, 'Footer below the table'),
        ]

    def test_table_is_spliced_between_the_paragraphs_above_and_below(self):
        els = netra._reconstruct_netra_spatial_elements(self.segments(), W, H, gray=page_with_grid(), dpi=200)
        kinds = [e['type'] for e in els]
        self.assertEqual(kinds.count('table'), 1)
        idx = kinds.index('table')
        self.assertGreater(idx, 0)                                       # something above it
        self.assertLess(idx, len(els) - 1)                               # something below it
        texts = ' '.join(e.get('text', '') for e in els if e['type'] != 'table')
        self.assertIn('Title above', texts)
        self.assertIn('Footer below', texts)
        self.assertNotIn('Sok', texts)                                   # cell text is not repeated as a paragraph

    def test_table_cells_header_and_column_widths(self):
        els = netra._reconstruct_netra_spatial_elements(self.segments(), W, H, gray=page_with_grid(), dpi=200)
        table = next(e for e in els if e['type'] == 'table')
        self.assertEqual([c['text'] for c in table['rows'][0]], ['No', 'Name', 'Age'])
        self.assertTrue(all(c['bold'] for c in table['rows'][0]))        # header row
        self.assertEqual([c['text'] for c in table['rows'][2]], ['2', 'Dara', '25'])
        self.assertEqual(table['col_widths'], [200, 300, 300])

    def test_without_a_grid_nothing_changes(self):
        plain = np.full((H, W), 255, dtype=np.uint8)
        els = netra._reconstruct_netra_spatial_elements(
            [box(100, 500, 900, 560, 'just text'), box(100, 570, 900, 630, 'more text')], W, H, gray=plain, dpi=200)
        self.assertNotIn('table', [e['type'] for e in els])


class WriterTests(SimpleTestCase):
    def test_column_widths_follow_the_source_table(self):
        item = {'type': 'table', 'layout': 'grid', 'col_widths': [100, 300],
                'rows': [[{'text': 'a'}, {'text': 'b'}], [{'text': 'c'}, {'text': 'd'}]]}
        doc = Document()
        _add_structured_json_to_docx(doc, [item])
        cells = doc.tables[0].rows[0].cells
        self.assertAlmostEqual(cells[1].width / cells[0].width, 3.0, delta=0.05)

    def test_without_widths_columns_are_equal(self):
        item = {'type': 'table', 'layout': 'grid', 'rows': [[{'text': 'a'}, {'text': 'b'}]]}
        doc = Document()
        _add_structured_json_to_docx(doc, [item])
        cells = doc.tables[0].rows[0].cells
        self.assertEqual(cells[0].width, cells[1].width)


class TextCleaningTests(SimpleTestCase):
    def test_unk_placeholder_is_removed(self):
        self.assertEqual(netra._clean_text('<unk> )'), ')')
        self.assertEqual(netra._clean_text('ក  <unk>  ខ'), 'ក ខ')

    def test_boxes_with_nothing_readable_are_dropped(self):
        self.assertFalse(netra._has_content(')'))
        self.assertFalse(netra._has_content(''))
        self.assertTrue(netra._has_content('ក'))
        self.assertTrue(netra._has_content('១២'))
