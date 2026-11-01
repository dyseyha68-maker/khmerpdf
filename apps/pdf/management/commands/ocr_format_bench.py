"""Measure how well OCR recovers formatting (bold, size, line spacing, paragraph gaps, indents).

Ground truth comes from a *digital* PDF's own text layer, so no hand-typed reference is needed:
each text block is a paragraph, with its font size, bold flag, baseline spacing, indent and gap.
The page is rasterised, run through the Netra measurement code, and the two are compared.

    python manage.py ocr_format_bench --synthetic            # generated Khmer pages with known formatting
    python manage.py ocr_format_bench report.pdf --pages 1-3 # any digital PDF (e.g. a Word/Chrome export)
    ... --dump                                               # per-paragraph truth vs measured

Synthetic pages need Khmer OS Battambang (Regular + Bold) in --font-dir (default: Windows Fonts).
"""
import json
import os
import re
import shutil
import statistics
import tempfile

from django.core.management.base import BaseCommand, CommandError

KHMER_SENTENCES = [
    'ក្នុងករណីដែលកូនគ្មានពុកម្តាយ ឬគ្មានអ្នកអនុវត្តអំណាចមេបា នោះកូនត្រូវស្ថិតនៅក្រោមរបបអាណាព្យាបាលសម្រាប់អនីតិជន។',
    'អ្នកអាណាព្យាបាលមានសិទ្ធិ និងករណីយកិច្ចក្នុងការគ្រប់គ្រងទ្រព្យសម្បត្តិ និងការបីបាច់ថែរក្សារូបកាយរបស់កូន។',
    'អំណាចមេបា គឺសំដៅដល់សិទ្ធិ និងករណីយកិច្ចក្នុងការថែរក្សា និងអប់រំកូន ដែលជាអនីតិជនរហូតដល់គាត់មានវ័យគ្រប់គ្រាន់។',
    'ការប្រព្រឹត្តិទៅតាមច្បាប់ ត្រូវធ្វើឡើងដោយប្រុងប្រយ័ត្នក្នុងនាមជាអ្នកគ្រប់គ្រងដោយសុចរិត និងយុត្តិធម៌។',
    'សាលាដំបូងមានសមត្ថកិច្ចជំនុំជម្រះរឿងក្តីរដ្ឋប្បវេណី និងរឿងក្តីព្រហ្មទណ្ឌ តាមបទប្បញ្ញត្តិនៃច្បាប់។',
]
TITLES = ['សេចក្តីសម្រេចរបស់តុលាការ', 'ជំពូកទី១ បទប្បញ្ញត្តិទូទៅ', 'មាត្រា១០៦៧ សិទ្ធិអាណាព្យាបាល']

# (kind, size_pt, bold, line_pitch_pt, space_before_pt, first_line_indent_pt, align, sentences)
SYNTHETIC_PAGES = [
    [('title', 18, True, 32, 0, 0, 'center', 0), ('body', 11, False, 23, 18, 24, 'justify', 3),
     ('body', 11, False, 23, 6, 24, 'justify', 3), ('heading', 13, True, 26, 16, 0, 'left', 0),
     ('body', 11, False, 23, 4, 0, 'justify', 4), ('note', 8.5, False, 15, 22, 0, 'left', 2)],
    [('heading', 15, True, 28, 0, 0, 'center', 1), ('body', 12, False, 26, 14, 28, 'justify', 4),
     ('body', 12, False, 26, 10, 28, 'justify', 3), ('body', 12, True, 26, 10, 0, 'left', 1),
     ('note', 9, False, 16, 24, 0, 'left', 2)],
]


def _page_range(spec, total):
    if not spec:
        return list(range(1, total + 1))
    pages = []
    for part in spec.split(','):
        a, _, b = part.partition('-')
        pages.extend(range(int(a), int(b or a) + 1))
    return [p for p in pages if 1 <= p <= total]


FONT_PAIRS = {  # name -> (regular file, bold file): the first is what the size/bold calibration was fitted on
    'battambang': ('KhmerOSBattambang-Regular.ttf', 'KhmerOSBattambang-Bold.ttf'),
    'content': ('KhmerOSContent-Regular.ttf', 'KhmerOSContent-Bold.ttf'),
    'noto': ('NotoSansKhmer-Regular.ttf', 'NotoSansKhmer-Bold.ttf'),
}


def build_synthetic_pdf(path, font_dir, font='battambang'):
    import fitz
    work = tempfile.mkdtemp()
    try:
        for src, dst in zip(FONT_PAIRS[font], ('kh.ttf', 'kh-bold.ttf')):
            shutil.copy(os.path.join(font_dir, src), os.path.join(work, dst))
        doc = fitz.open()
        rects = []
        for spec in SYNTHETIC_PAGES:
            page = doc.new_page(width=595, height=842)
            page_rects = []
            rects.append(page_rects)
            y, n = 56.0, 0
            for kind, size, bold, pitch, before, indent, align, count in spec:
                y += before
                text = TITLES[n % len(TITLES)] if count == 0 or kind in ('title', 'heading') and count <= 1 else ' '.join(
                    KHMER_SENTENCES[(n + i) % len(KHMER_SENTENCES)] for i in range(count))
                n += 1
                css = ('@font-face{font-family:kh;src:url(kh.ttf);} '
                       '@font-face{font-family:kh;font-weight:bold;src:url(kh-bold.ttf);} '
                       'body{font-family:kh;font-size:%spt;line-height:%spt;text-align:%s;margin:0;%s}'
                       % (size, pitch, align, 'font-weight:bold;' if bold else ''))
                css += ' p{margin:0;text-indent:%spt;}' % indent
                rect = fitz.Rect(56, y, 539, min(y + 260, 800))
                spare, _ = page.insert_htmlbox(rect, f'<p>{text}</p>', css=css, archive=fitz.Archive(work))
                page_rects.append([rect.x0, rect.y0, rect.x1, rect.y1 - spare])
                y = rect.y1 - spare
        doc.save(path)
        doc.close()
        with open(path + '.json', 'w', encoding='utf-8') as fh:
            json.dump(rects, fh)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _text_lines(page):
    """All text lines of a digital page: baseline, extent, font size, bold share."""
    out = []
    for b in page.get_text('dict')['blocks']:
        if b.get('type') != 0:
            continue
        lines = []
        for ln in b['lines']:
            spans = [s for s in ln['spans'] if s['text'].strip()]
            if not spans:
                continue
            chars = sum(len(s['text']) for s in spans)
            bold = sum(len(s['text']) for s in spans if s['flags'] & 16 or 'bold' in s['font'].lower()) / chars
            lines.append({'base': spans[0]['origin'][1], 'x0': ln['bbox'][0], 'x1': ln['bbox'][2],
                          'size': statistics.median(s['size'] for s in spans), 'bold': bold,
                          'top': ln['bbox'][1], 'bottom': ln['bbox'][3], 'text': ''.join(s['text'] for s in spans)})
        out.append(lines)
    return out


def _merge_visual_lines(lines, tol=2.0):
    """The text layer splits one printed line into several pieces: merge pieces sharing a baseline."""
    merged = []
    for l in sorted(lines, key=lambda l: (round(l['base'] / tol), l['x0'])):
        if merged and abs(merged[-1]['base'] - l['base']) <= tol:
            m = merged[-1]
            w_m, w_l = max(1, len(m['text'])), max(1, len(l['text']))
            m['bold'] = (m['bold'] * w_m + l['bold'] * w_l) / (w_m + w_l)
            m.update(x0=min(m['x0'], l['x0']), x1=max(m['x1'], l['x1']), top=min(m['top'], l['top']),
                     bottom=max(m['bottom'], l['bottom']), size=max(m['size'], l['size']), text=m['text'] + l['text'])
        else:
            merged.append(dict(l))
    return sorted(merged, key=lambda l: l['base'])


def truth_paragraphs(page, rects=None):
    """Paragraphs of a digital page with their measured formatting, in points.

    `rects` (known paragraph boxes, for generated pages) group the text lines; otherwise each
    PDF text block is taken as a paragraph."""
    if rects:
        flat = sorted((l for blk in _text_lines(page) for l in blk), key=lambda l: l['base'])
        blocks = []
        for r in rects:
            inside = [l for l in flat if r[1] - 1 <= (l['top'] + l['bottom']) / 2 <= r[3] + 1]
            if inside:
                blocks.append(_merge_visual_lines(inside))
    else:
        blocks = [_merge_visual_lines(lines) for lines in _text_lines(page) if lines]
    out, prev_base = [], None
    for lines in blocks:
        bases = [l['base'] for l in lines]
        steps = [b - a for a, b in zip(bases, bases[1:])]
        size = statistics.median(l['size'] for l in lines)
        # single-line paragraphs have no pitch of their own: use the previous paragraph's, else 1.5x size
        pitch = statistics.median(steps) if steps else (out[-1]['pitch'] if out else size * 1.5)
        indent = 0.0
        if len(lines) >= 2:
            rest = statistics.median(l['x0'] for l in lines[1:])
            indent = lines[0]['x0'] - rest
        out.append({
            'text': '',
            'size': size, 'bold': statistics.mean(l['bold'] for l in lines) > 0.5, 'pitch': pitch,
            'indent': indent if indent > 4 else 0.0, 'lines': len(lines),
            'before': None if prev_base is None else max(0.0, bases[0] - prev_base - pitch),
            'bbox': (min(l['x0'] for l in lines), min(l['top'] for l in lines),
                     max(l['x1'] for l in lines), max(l['bottom'] for l in lines)),
        })
        prev_base = bases[-1]
    return out


def _overlap(a, b):
    """Vertical-overlap ratio of two (x0, y0, x1, y1) boxes (intersection / smaller height)."""
    top, bottom = max(a[1], b[1]), min(a[3], b[3])
    return max(0.0, bottom - top) / max(1e-6, min(a[3] - a[1], b[3] - b[1]))


def match(truth, hyp, scale):
    """Greedy one-to-one matching of truth paragraphs (pt) to measured elements (px) by vertical overlap."""
    pairs, used = [], set()
    for i, t in enumerate(truth):
        tb = [v * scale for v in t['bbox']]
        best, best_j = 0.4, None
        for j, h in enumerate(hyp):
            if j in used or 'bbox' not in h:
                continue
            ov = _overlap(tb, h['bbox'])
            if ov > best:
                best, best_j = ov, j
        if best_j is not None:
            used.add(best_j)
            pairs.append((i, best_j))
    return pairs


class Command(BaseCommand):
    help = 'Compare OCR-measured formatting with the formatting stored in a digital PDF.'

    def add_arguments(self, parser):
        parser.add_argument('file', nargs='?', help='Digital PDF (has a text layer)')
        parser.add_argument('--synthetic', action='store_true', help='Generate Khmer test pages with known formatting')
        parser.add_argument('--font-dir', default=r'C:\Windows\Fonts')
        parser.add_argument('--font', default='battambang', choices=list(FONT_PAIRS), help='font for --synthetic')
        parser.add_argument('--pages', default='', help='e.g. 1-3,5 (default: all)')
        parser.add_argument('--dpi', type=int, default=200)
        parser.add_argument('--dump', action='store_true', help='Print truth vs measured per paragraph')

    def handle(self, *args, **o):
        import fitz
        from apps.pdf.ocr.engines.netra import netra_page_elements
        from apps.pdf.ocr.pages import _iter_document_pages

        path = o['file']
        if o['synthetic']:
            path = os.path.join(tempfile.gettempdir(), f'khmer_format_bench_{o["font"]}.pdf')
            try:
                build_synthetic_pdf(path, o['font_dir'], o['font'])
            except OSError as e:
                raise CommandError(f'Need Khmer OS Battambang Regular+Bold in {o["font_dir"]}: {e}')
            self.stdout.write(f'Synthetic benchmark written to {path}')
        if not path:
            raise CommandError('Give a PDF or use --synthetic')

        doc = fitz.open(path)
        scale = o['dpi'] / 72.0
        rects = None
        if os.path.exists(path + '.json'):  # paragraph boxes saved by --synthetic
            with open(path + '.json', encoding='utf-8') as fh:
                rects = json.load(fh)
        err = {'pitch': [], 'size': [], 'before': [], 'indent': []}
        bold_ok = bold_n = matched = n_truth = n_hyp = 0
        for pno in _page_range(o['pages'], len(doc)):
            truth = truth_paragraphs(doc[pno - 1], rects[pno - 1] if rects and pno - 1 < len(rects) else None)
            if not truth:
                self.stdout.write(f'page {pno}: no text layer, skipped')
                continue
            for _, _, img in _iter_document_pages(path, page_range_str=str(pno), max_limit=1, dpi=o['dpi']):
                hyp = netra_page_elements(img)
            hyp = [h for h in hyp if h.get('type') != 'table']
            pairs = match(truth, hyp, scale)
            n_truth += len(truth)
            n_hyp += len(hyp)
            matched += len(pairs)
            self.stdout.write(f'page {pno}: {len(truth)} true paragraphs, {len(hyp)} measured, {len(pairs)} matched')
            for i, j in pairs:
                t, h = truth[i], hyp[j]
                bold_n += 1
                bold_ok += bool(h.get('bold')) == t['bold']
                if 'line_pitch' in h and t['lines'] >= 2:   # a 1-line paragraph has no pitch of its own
                    err['pitch'].append(h['line_pitch'] - t['pitch'])
                if 'font_size' in h:
                    err['size'].append(h['font_size'] - t['size'])
                if t['before'] is not None and 'space_before' in h:
                    err['before'].append(h['space_before'] - t['before'])
                err['indent'].append(h.get('first_line_indent', 0) - t['indent'])
                if o['dump']:
                    self.stdout.write(
                        f"  #{i:<2} size {t['size']:4.1f}/{h.get('font_size', 0):4.1f} (box {h.get('box_h_pt', 0):4.1f})  "
                        f"bold {int(t['bold'])}/{int(bool(h.get('bold')))} (x{h.get('stroke_ratio', 0):.2f})  pitch {t['pitch']:4.1f}/{h.get('line_pitch', 0):4.1f}  "
                        f"before {t['before'] if t['before'] is not None else 0:4.1f}/{h.get('space_before', 0):4.1f}  "
                        f"indent {t['indent']:4.1f}/{h.get('first_line_indent', 0):4.1f}  lines {t['lines']}")
        doc.close()
        self._report(err, bold_ok, bold_n, matched, n_truth, n_hyp)

    def _report(self, err, bold_ok, bold_n, matched, n_truth, n_hyp):
        mae = lambda v: f'{statistics.mean(abs(x) for x in v):5.1f}pt (bias {statistics.mean(v):+5.1f})' if v else '  n/a'
        recall = matched / n_truth if n_truth else 0
        precision = matched / n_hyp if n_hyp else 0
        self.stdout.write('\nOCR formatting accuracy (measured vs the PDF\'s own text layer)')
        self.stdout.write('-' * 62)
        self.stdout.write(f'Paragraphs found (recall)      {recall * 100:5.1f}%   ({matched}/{n_truth})')
        self.stdout.write(f'Not split into pieces (prec.)  {precision * 100:5.1f}%   ({matched}/{n_hyp})')
        self.stdout.write(f'Bold correct                   {(bold_ok / bold_n * 100) if bold_n else 0:5.1f}%   ({bold_ok}/{bold_n})')
        self.stdout.write(f'Font size error (mean abs)     {mae(err["size"])}')
        self.stdout.write(f'Line spacing error             {mae(err["pitch"])}')
        self.stdout.write(f'Space before paragraph error   {mae(err["before"])}')
        self.stdout.write(f'First-line indent error        {mae(err["indent"])}')
