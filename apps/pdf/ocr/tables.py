"""Ruled-table detection for the line-based OCR path (no model, no extra memory).

Netra's line detector finds text but not tables. Tables with drawn borders can be found from the
ruling itself: long horizontal and vertical lines are isolated with OpenCV, their positions give a
grid of cells, and each recognised text line goes into the cell that contains its centre.
Tables without borders are not detected (the layout-analysis mode of Netra can handle those).
"""

MIN_LINES = 3            # a grid needs at least this many row lines and column lines (2 would be a box)
MIN_WIDTH_FRACTION = 0.30   # table at least this wide ...
MIN_HEIGHT_FRACTION = 0.06  # ... and this tall, relative to the page
ROW_LINE_COVERAGE = 0.60    # a row line must span this much of the table width
COL_LINE_COVERAGE = 0.50    # a column line this much of its height (stamps/handwriting can break it)


def _cluster(indices, gap=4):
    """Group neighbouring pixel indices (a ruled line is several pixels thick) into line centres."""
    centres, group = [], []
    for i in indices:
        if group and i - group[-1] > gap:
            centres.append(sum(group) / len(group))
            group = []
        group.append(i)
    if group:
        centres.append(sum(group) / len(group))
    return centres


def detect_grid_tables(gray):
    """Find ruled tables on a grayscale page image (numpy uint8).

    Returns a list of dicts: {'bbox': (x1, y1, x2, y2), 'xs': [column line x...], 'ys': [row line y...]},
    ordered top to bottom. Returns [] if OpenCV is unavailable or nothing looks like a table."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return []

    h, w = gray.shape[:2]
    if h < 200 or w < 200:
        return []
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 10)
    horiz = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(25, w // 25), 1)))
    vert = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(25, h // 25))))
    joined = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    tables = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < MIN_WIDTH_FRACTION * w or ch < MIN_HEIGHT_FRACTION * h:
            continue
        sub_h = horiz[y:y + ch, x:x + cw] > 0
        sub_v = vert[y:y + ch, x:x + cw] > 0
        row_hits = np.where(sub_h.sum(axis=1) >= ROW_LINE_COVERAGE * cw)[0]
        col_hits = np.where(sub_v.sum(axis=0) >= COL_LINE_COVERAGE * ch)[0]
        ys = [y + v for v in _cluster(row_hits.tolist())]
        xs = [x + v for v in _cluster(col_hits.tolist())]
        if len(ys) >= MIN_LINES and len(xs) >= MIN_LINES:
            tables.append({'bbox': (int(xs[0]), int(ys[0]), int(xs[-1]), int(ys[-1])),
                           'xs': [float(v) for v in xs], 'ys': [float(v) for v in ys]})
    return sorted(tables, key=lambda t: t['bbox'][1])


def _cell_index(value, lines):
    """Index of the interval between two neighbouring lines that contains `value`, or None."""
    for i in range(len(lines) - 1):
        if lines[i] <= value < lines[i + 1]:
            return i
    return None


def inside(table, seg_box, margin=0.0):
    """True when the centre of a text box lies inside the table's outer border."""
    x1, y1, x2, y2 = seg_box
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    tx1, ty1, tx2, ty2 = table['bbox']
    return tx1 - margin <= cx <= tx2 + margin and ty1 - margin <= cy <= ty2 + margin


_BORDER_NOISE = ' []|{}'      # the ruling itself is sometimes read as a bracket or bar at a cell's edge


def _overlap(a_lo, a_hi, b_lo, b_hi):
    """Overlap of two intervals as a fraction of the shorter one."""
    shorter = max(1e-6, min(a_hi - a_lo, b_hi - b_lo))
    return max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo)) / shorter


def _dedupe(parts):
    """The detector often returns the same text twice as overlapping boxes: keep the longer reading."""
    kept = []
    for p in sorted(parts, key=lambda p: -len(p['text'])):
        b = p['bbox']
        if not any(_overlap(b[0], b[2], k['bbox'][0], k['bbox'][2]) > 0.6 and
                   _overlap(b[1], b[3], k['bbox'][1], k['bbox'][3]) > 0.5 for k in kept):
            kept.append(p)
    return kept


def fill_table(table, items):
    """Put text items into the table's cells.

    items: list of {'bbox': [x1, y1, x2, y2], 'text': str}. Returns (rows, used, widths): rows is a list of
    lists of cell strings (all-empty rows and columns dropped), used the items placed, and widths the pixel
    width of each kept column (so the Word table can keep the original proportions)."""
    xs, ys = table['xs'], table['ys']
    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    cells = [[[] for _ in range(n_cols)] for _ in range(n_rows)]
    used = []
    for it in items:
        x1, y1, x2, y2 = it['bbox']
        r, c = _cell_index((y1 + y2) / 2.0, ys), _cell_index((x1 + x2) / 2.0, xs)
        if r is None or c is None or not (it.get('text') or '').strip():
            continue
        cells[r][c].append(it)
        used.append(it)

    def cell_text(parts):
        parts = sorted(_dedupe(parts), key=lambda p: ((p['bbox'][1] + p['bbox'][3]) / 2.0, p['bbox'][0]))
        lines, last_cy, last_h = [], None, None
        for p in parts:
            cy, h = (p['bbox'][1] + p['bbox'][3]) / 2.0, max(1.0, p['bbox'][3] - p['bbox'][1])
            if last_cy is not None and abs(cy - last_cy) <= 0.5 * min(h, last_h):
                lines[-1].append(p)          # same printed line
            else:
                lines.append([p])
            last_cy, last_h = cy, h
        return '\n'.join(' '.join(q['text'].strip() for q in sorted(line, key=lambda q: q['bbox'][0])) for line in lines)

    rows = [[cell_text(cell).strip(_BORDER_NOISE) for cell in row] for row in cells]
    # a box that straddles two columns also carries its neighbour's text: drop that repeated prefix
    for row in rows:
        for c in range(1, n_cols):
            prev, cur = row[c - 1], row[c]
            if prev and len(prev) <= 8 and cur.startswith(prev) and len(cur) > len(prev):
                rest = cur[len(prev):].lstrip(_BORDER_NOISE + '-.:')
                if rest:
                    row[c] = rest
    kept_rows = [i for i, row in enumerate(rows) if any(v.strip() for v in row)]
    rows = [rows[i] for i in kept_rows]
    widths = []
    if rows:
        keep = [c for c in range(n_cols) if any(row[c].strip() for row in rows)]
        rows = [[row[c] for c in keep] for row in rows]
        widths = [xs[c + 1] - xs[c] for c in keep]
    return rows, used, widths
