"""Netra page layout analysis (tables, headings, reading order) -> Word elements, with fallbacks.

Uses fabricated layout blocks, so no model is needed. Page is 1200 px wide at 200 dpi (0.36 pt/px).
"""
from unittest import mock

from django.test import SimpleTestCase, override_settings
from docx import Document
from docx.shared import Pt

from apps.pdf.ocr.docx_writer import _add_structured_json_to_docx
from apps.pdf.ocr.engines import netra

W, DPI = 1200, 200


def block(kind, text='', bbox=(100, 100, 1100, 160), **extra):
    return {'type': kind, 'text': text, 'bbox': list(bbox), 'page': 0, **extra}


class BlocksToElementsTests(SimpleTestCase):
    def convert(self, blocks):
        return netra._blocks_to_elements(blocks, W, DPI)

    def test_types_and_headings(self):
        els = self.convert([
            block('heading', 'ចំណងជើង', level=1, align='center'),
            block('paragraph', 'ខ្លឹមសារ', (100, 200, 1100, 260)),
            block('caption', 'ចំណាំ', (100, 300, 600, 340)),
            block('page_footer', 'ជើងទំព័រ', (100, 400, 1100, 440), align='center'),
            block('figure', '', (100, 450, 300, 600)),
            block('formula', '', (100, 620, 300, 700)),
        ])
        self.assertEqual([e['type'] for e in els], ['heading', 'paragraph', 'paragraph', 'paragraph'])   # figure/formula skipped
        self.assertEqual((els[0]['level'], els[0]['align']), (1, 'center'))
        self.assertEqual(els[3].get('align'), 'center')

    def test_soft_wrapped_paragraph_is_joined_but_short_blocks_keep_line_breaks(self):
        wide = self.convert([block('paragraph', 'line one\nline two\nline three\nline four', (100, 100, 1100, 400))])
        self.assertNotIn('\n', wide[0]['text'])
        letterhead = self.convert([block('paragraph', 'ក្រសួងយុត្តិធម៌\nលេខ ១២៣', (50, 100, 400, 200))])   # narrow, 2 lines
        self.assertEqual(letterhead[0]['text'], 'ក្រសួងយុត្តិធម៌\nលេខ ១២៣')

    def test_left_alignment_is_left_to_the_writer_so_long_paragraphs_justify(self):
        els = self.convert([block('paragraph', 'x' * 100, align='left'), block('paragraph', 'y' * 100, (100, 300, 1100, 360), align='right')])
        self.assertNotIn('align', els[0])
        self.assertEqual(els[1]['align'], 'right')

    def test_gap_between_blocks_becomes_space_before_in_points(self):
        els = self.convert([block('paragraph', 'a', (100, 100, 1100, 160)), block('paragraph', 'b', (100, 210, 1100, 270)),
                            block('paragraph', 'c', (100, 272, 1100, 330)), block('paragraph', 'd', (100, 900, 1100, 960))])
        self.assertNotIn('space_before', els[0])
        self.assertEqual(els[1]['space_before'], 18.0)            # 50 px * 0.36
        self.assertNotIn('space_before', els[2])                  # 2 px gap: none
        self.assertEqual(els[3]['space_before'], 36.0)            # capped

    def test_empty_blocks_and_tables_are_skipped(self):
        self.assertEqual(self.convert([block('paragraph', '  \n '), {'type': 'table', 'rows': [], 'bbox': [0, 0, 5, 5]}]), [])


class TableTests(SimpleTestCase):
    def test_header_row_is_bold_and_centred_and_numbers_are_centred(self):
        cells = netra._table_cells([['ល.រ', 'ឈ្មោះ', 'អាយុ'], ['១', 'សុខ សុវណ្ណ', '៣០'], ['២', 'ចាន់ ដារ៉ា', None]])
        self.assertTrue(all(c['bold'] and c['align'] == 'center' for c in cells[0]))
        self.assertEqual([c['align'] for c in cells[1]], ['center', 'left', 'center'])     # Khmer digits are numbers
        self.assertFalse(any(c['bold'] for c in cells[1]))
        self.assertEqual(cells[2][2]['text'], '')

    def test_table_block_becomes_a_grid_table_that_the_writer_formats_like_the_rest(self):
        els = netra._blocks_to_elements([block('table', '', (50, 300, 1150, 700), rows=[['ក', 'ខ'], ['១', 'គ']])], W, DPI)
        self.assertEqual((els[0]['type'], els[0]['layout']), ('table', 'grid'))
        doc = Document()
        _add_structured_json_to_docx(doc, els)
        table = doc.tables[0]
        self.assertEqual((len(table.rows), len(table.columns)), (2, 2))
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    self.assertEqual(p.paragraph_format.line_spacing, 1.5)
                    for run in p.runs:
                        self.assertEqual(run.font.size, Pt(11))
        self.assertTrue(table.rows[0].cells[0].paragraphs[0].runs[0].bold)


class FallbackTests(SimpleTestCase):
    img = mock.Mock(size=(W, 1600), info={'dpi': (DPI, DPI)})

    def test_layout_blocks_are_used_when_enabled(self):
        with override_settings(NETRA_LAYOUT_ANALYSIS=True), \
                mock.patch.object(netra, '_layout_blocks', return_value=[block('heading', 'ក', level=1)]) as lb, \
                mock.patch.object(netra, '_netra_segments') as seg:
            els = netra.netra_page_elements(self.img)
        self.assertEqual(els[0]['type'], 'heading')
        lb.assert_called_once()
        seg.assert_not_called()

    def test_a_layout_error_falls_back_to_the_line_path(self):
        with override_settings(NETRA_LAYOUT_ANALYSIS=True), \
                mock.patch.object(netra, '_layout_blocks', side_effect=TypeError('analyze_page() got an unexpected keyword')), \
                mock.patch.object(netra, '_netra_segments', return_value=[]) as seg:
            self.assertEqual(netra.netra_page_elements(self.img), [])
        seg.assert_called_once()

    def test_no_blocks_falls_back_to_the_line_path(self):
        with override_settings(NETRA_LAYOUT_ANALYSIS=True), \
                mock.patch.object(netra, '_layout_blocks', return_value=[block('figure')]), \
                mock.patch.object(netra, '_netra_segments', return_value=[]) as seg:
            netra.netra_page_elements(self.img)
        seg.assert_called_once()

    def test_layout_is_not_touched_when_switched_off(self):
        with override_settings(NETRA_LAYOUT_ANALYSIS=False), \
                mock.patch.object(netra, '_layout_blocks') as lb, \
                mock.patch.object(netra, '_netra_segments', return_value=[]):
            netra.netra_page_elements(self.img)
        lb.assert_not_called()


class PipelineSharingTests(SimpleTestCase):
    def test_line_pipeline_shares_the_loaded_models_but_has_no_layout_analyzer(self):
        class FakePipeline:
            detector = object()
            layout = True
            layout_analyzer = object()

        fake = FakePipeline()
        with override_settings(NETRA_LAYOUT_ANALYSIS=True), mock.patch.object(netra, '_PIPELINES', {'layout': fake}):
            lines = netra._get_pipeline()
        self.assertIsNot(lines, fake)
        self.assertIs(lines.detector, fake.detector)          # the models are shared, not loaded twice
        self.assertIsNone(lines.layout_analyzer)              # lines, not paragraph blocks
        self.assertIsNotNone(fake.layout_analyzer)            # the layout pipeline is untouched
