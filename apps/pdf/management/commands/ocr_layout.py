"""Check OCR layout/structure (headings, alignment, tables) for one page.

    python manage.py ocr_layout scan.pdf --page 1                       # show Netra's structure
    python manage.py ocr_layout scan.pdf --page 1 --save-json ref.json  # write a draft reference to hand-correct
    python manage.py ocr_layout scan.pdf --page 1 --reference ref.json  # score against the reference
    python manage.py ocr_layout scan.pdf --engine gemini --cloud        # Gemini's structure (uses API credit)

The reference is a JSON array in the same schema the Claude/Gemini prompt returns
(see apps/pdf/ocr/engines/prompt.py). Cloud engines never run without --cloud.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from apps.pdf.ocr import layout_score


class Command(BaseCommand):
    help = 'Show OCR document structure for one page and optionally score it against a reference.'

    def add_arguments(self, parser):
        parser.add_argument('file', help='PDF or image to OCR')
        parser.add_argument('--page', type=int, default=1)
        parser.add_argument('--dpi', type=int, default=200)
        parser.add_argument('--lang', default='khm', choices=['eng', 'khm', 'eng+khm'])
        parser.add_argument('--engine', default='netra', choices=['netra', 'gemini', 'claude'])
        parser.add_argument('--cloud', action='store_true', help='Required for gemini/claude (uses API credit)')
        parser.add_argument('--reference', help='JSON file with the correct elements')
        parser.add_argument('--save-json', dest='save_json', help='Write the produced elements to this file')

    def handle(self, *args, **o):
        from apps.pdf.ocr.pages import _iter_document_pages

        page = None
        for _, _, img in _iter_document_pages(o['file'], page_range_str=str(o['page']), max_limit=1, dpi=o['dpi']):
            page = img
        if page is None:
            raise CommandError('Could not render the requested page.')

        elements = self._run_engine(o, page)
        self.stdout.write(f'\n{o["engine"]}: {len(elements)} elements')
        for i, el in enumerate(elements):
            self.stdout.write(f'  {i:>2}  {layout_score.describe(el)}')

        if o['save_json']:
            with open(o['save_json'], 'w', encoding='utf-8') as fh:
                json.dump(elements, fh, ensure_ascii=False, indent=2)
            self.stdout.write(f'\nSaved draft reference to {o["save_json"]} (correct it by hand, then use --reference).')

        if o['reference']:
            with open(o['reference'], encoding='utf-8') as fh:
                reference = layout_score.load_elements(fh.read())
            self._report(layout_score.score(reference, elements), reference)

    def _run_engine(self, o, page):
        if o['engine'] == 'netra':
            from apps.pdf.ocr.engines.netra import netra_page_elements
            return netra_page_elements(page)

        if not o['cloud']:
            raise CommandError(f'--engine {o["engine"]} calls a paid API; add --cloud to confirm.')
        from apps.pdf.tasks import _get_ocr_settings, _claude_ocr_page, _gemini_ocr_page
        s = _get_ocr_settings()
        if o['engine'] == 'gemini':
            key = (s.gemini_api_key or '').strip() if s else ''
            if not key:
                raise CommandError('No Gemini API key saved in OCR Settings.')
            raw = _gemini_ocr_page(key, (s.gemini_model or '').strip(), page, ocr_lang=o['lang'])
        else:
            key = (s.claude_api_key or '').strip() if s else ''
            if not key:
                raise CommandError('No Claude API key saved in OCR Settings.')
            raw = _claude_ocr_page(key, (s.claude_model or '').strip(), page, ocr_lang=o['lang'])
        try:
            return layout_score.load_elements(raw)
        except ValueError as e:
            raise CommandError(f'{o["engine"]} did not return a JSON element list: {e}')

    def _report(self, result, reference):
        m = result['metrics']
        pct = lambda v: '   n/a' if v is None else f'{v * 100:5.1f}%'
        self.stdout.write('\nLayout score vs reference')
        self.stdout.write('-' * 34)
        for key, label in [('recall', 'Elements found'), ('precision', 'No extra/split elements'),
                           ('type_accuracy', 'Type correct'), ('align_accuracy', 'Alignment correct'),
                           ('tables_found', 'Tables detected'), ('table_shape', 'Table rows x cols'),
                           ('table_cells', 'Table cell text')]:
            self.stdout.write(f'{label:<26}{pct(m[key])}')
        self.stdout.write('-' * 34)
        self.stdout.write(f'{"OVERALL":<26}{pct(m["overall"])}')
        self.stdout.write(f'Fragmentation {m["fragmentation"]:.2f} (1.0 = same element count; >1 split, <1 merged)')
        problems = [(i, s) for i, s in result['details'] if s != 'OK']
        if problems:
            self.stdout.write('\nProblems by reference element:')
            for i, status in problems:
                self.stdout.write(f'  {i:>2}  {layout_score.describe(reference[i], 50)}  ->  {status}')
        else:
            self.stdout.write('\nAll reference elements matched.')
