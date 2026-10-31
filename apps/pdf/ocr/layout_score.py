"""Score how closely OCR-produced document structure matches a reference.

Both sides use the element schema from engines/prompt.py:
heading / paragraph / list_item / table (rows of {"text", "align", "bold"} cells).

Hypothesis elements are matched to reference elements in reading order by text
similarity, then compared on type, alignment and (for tables) shape and cell text.
"""
import json
import unicodedata
from difflib import SequenceMatcher

MATCH_THRESHOLD = 0.5   # minimum text similarity to treat two elements as the same one
DEFAULT_ALIGN = {'heading': 'center', 'paragraph': 'left', 'list_item': 'left'}


def load_elements(raw):
    """Parse an element list from JSON text (tolerates code fences and truncated output)."""
    clean = raw.strip()
    if clean.startswith('```'):
        clean = clean[clean.find('\n') + 1:] if '\n' in clean else ''
        if clean.rstrip().endswith('```'):
            clean = clean.rstrip()[:-3]
    start = clean.find('[')
    if start == -1:
        raise ValueError('no JSON array found')
    candidate = clean[start:].strip()
    try:
        items = json.loads(candidate, strict=False)
    except ValueError:
        last = candidate.rfind('}')
        if last == -1:
            raise
        items = json.loads(candidate[:last + 1].rstrip().rstrip(',') + ']', strict=False)
    if not isinstance(items, list):
        raise ValueError('expected a JSON array of elements')
    return items


def _norm(text):
    return ''.join(unicodedata.normalize('NFC', (text or '').replace('**', '')).split())


def _cells(el):
    rows = []
    for row in el.get('rows') or []:
        rows.append([_norm(c.get('text', '') if isinstance(c, dict) else str(c)) for c in row])
    return rows


def _prep(el):
    kind = el.get('type', 'paragraph')
    if kind == 'table':
        rows = _cells(el)
        return {'type': 'table', 'align': None, 'text': ''.join(c for r in rows for c in r),
                'rows': rows, 'shape': (len(rows), max((len(r) for r in rows), default=0))}
    return {'type': kind, 'align': el.get('align') or DEFAULT_ALIGN.get(kind, 'left'),
            'text': _norm(el.get('text', '')), 'rows': None, 'shape': None}


def _sim(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _align(ref, hyp):
    """In-order matching that maximises total text similarity (LCS-style DP)."""
    n, m = len(ref), len(hyp)
    sims = [[_sim(r['text'], h['text']) for h in hyp] for r in ref]
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            s = sims[i - 1][j - 1]
            take = best[i - 1][j - 1] + s if s >= MATCH_THRESHOLD else -1
            best[i][j] = max(best[i - 1][j], best[i][j - 1], take)
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        s = sims[i - 1][j - 1]
        if s >= MATCH_THRESHOLD and abs(best[i][j] - (best[i - 1][j - 1] + s)) < 1e-9:
            pairs.append((i - 1, j - 1, s))
            i, j = i - 1, j - 1
        elif best[i - 1][j] >= best[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def score(reference, hypothesis):
    """Return {'metrics': {...}, 'details': [(ref_index, status), ...]}.

    Metrics are fractions in 0..1 (None when not applicable), plus 'overall'
    (mean of the applicable ones) and 'fragmentation' (hyp elements per ref element;
    >1 means paragraphs were split into pieces, <1 means they were merged).
    """
    ref = [_prep(e) for e in reference]
    hyp = [_prep(e) for e in hypothesis]
    pairs = _align(ref, hyp)
    matched_ref = {i for i, _, _ in pairs}

    type_ok = align_ok = align_n = 0
    table_n = sum(1 for r in ref if r['type'] == 'table')
    table_found = table_shape = 0
    cell_scores = []
    details = {}
    for i, j, _ in pairs:
        r, h = ref[i], hyp[j]
        problems = []
        if r['type'] == h['type']:
            type_ok += 1
        else:
            problems.append(f"type {h['type']} (expected {r['type']})")
        if r['type'] != 'table' and h['type'] != 'table':
            align_n += 1
            if r['align'] == h['align']:
                align_ok += 1
            else:
                problems.append(f"align {h['align']} (expected {r['align']})")
        if r['type'] == 'table' and h['type'] == 'table':
            table_found += 1
            if r['shape'] == h['shape']:
                table_shape += 1
                flat_r = [c for row in r['rows'] for c in row]
                flat_h = [c for row in h['rows'] for c in row]
                cell_scores.append(sum(_sim(a, b) for a, b in zip(flat_r, flat_h)) / max(len(flat_r), 1))
            else:
                problems.append(f"table {h['shape'][0]}x{h['shape'][1]} (expected {r['shape'][0]}x{r['shape'][1]})")
        details[i] = 'OK' if not problems else '; '.join(problems)
    for i in range(len(ref)):
        details.setdefault(i, 'MISSING')

    frac = lambda a, b: (a / b) if b else None
    metrics = {
        'recall': frac(len(pairs), len(ref)),
        'precision': frac(len(pairs), len(hyp)),
        'type_accuracy': frac(type_ok, len(pairs)),
        'align_accuracy': frac(align_ok, align_n),
        'tables_found': frac(table_found, table_n),
        'table_shape': frac(table_shape, table_n),
        'table_cells': (sum(cell_scores) / table_n) if table_n and cell_scores else (0.0 if table_n else None),
    }
    applicable = [v for v in metrics.values() if v is not None]
    metrics['overall'] = sum(applicable) / len(applicable) if applicable else None
    metrics['fragmentation'] = frac(len(hyp), len(ref))
    metrics['unmatched_hyp'] = len(hyp) - len(pairs)
    return {'metrics': metrics, 'details': sorted(details.items())}


def describe(el, width=70):
    """One-line summary of an element for printing."""
    p = _prep(el)
    if p['type'] == 'table':
        return f"table {p['shape'][0]}x{p['shape'][1]}: " + ' | '.join(c for r in p['rows'][:2] for c in r)[:width]
    return f"{p['type']:<9} {p['align']:<7} {el.get('text', '')[:width]!r}"
