"""Memory-safe page streaming for OCR (one rendered page at a time)."""
import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)


def _iter_document_pages(input_path, page_range_str=None, max_limit=150, dpi=200):
    """
    Memory-efficient document page generator.
    Streams one page image at a time, never loading all pages into memory at once.
    Yields: (current_page_number_in_sequence, total_pages_to_process, PIL_Image)
    """
    import os
    import gc
    import logging
    from PIL import Image
    logger = logging.getLogger(__name__)

    ext = os.path.splitext(input_path)[1].lower()
    if ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'):
        img = Image.open(input_path)
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        yield 1, 1, img
        return

    # PDF Processing: Determine total pages without rendering
    total_pages = 0
    fitz_doc = None
    poppler_path = getattr(settings, 'POPPLER_PATH', None)

    try:
        import fitz
        fitz_doc = fitz.open(input_path)
        total_pages = len(fitz_doc)
    except Exception as e:
        logger.warning(f'_iter_document_pages: fitz.open failed: {e}')
        fitz_doc = None

    if total_pages == 0:
        try:
            from pdf2image import pdfinfo_from_path
            info = pdfinfo_from_path(input_path, poppler_path=poppler_path)
            total_pages = int(info.get('Pages', 0))
        except Exception as e:
            logger.error(f'_iter_document_pages: pdfinfo_from_path failed: {e}')

    if total_pages <= 0:
        raise RuntimeError('PDF file is empty or cannot be opened.')

    # Determine target page indices (0-indexed)
    target_indices = []
    if page_range_str and str(page_range_str).strip():
        for part in str(page_range_str).replace(';', ',').split(','):
            part = part.strip()
            if '-' in part:
                sub = part.split('-')
                if len(sub) == 2 and sub[0].strip().isdigit() and sub[1].strip().isdigit():
                    s, e = int(sub[0]), int(sub[1])
                    for p in range(max(1, s), min(total_pages, e) + 1):
                        target_indices.append(p - 1)
            elif part.isdigit():
                p = int(part)
                if 1 <= p <= total_pages:
                    target_indices.append(p - 1)
        # Deduplicate preserving order
        target_indices = list(dict.fromkeys(target_indices))

    if not target_indices:
        limit = min(total_pages, max_limit) if max_limit else total_pages
        target_indices = list(range(limit))

    total_to_process = len(target_indices)

    try:
        for seq_idx, p_idx in enumerate(target_indices):
            pil_img = None
            # 1. High-speed, low-memory PyMuPDF (fitz)
            if fitz_doc is not None:
                try:
                    fpage = fitz_doc.load_page(p_idx)
                    zoom = dpi / 72.0
                    mat = fitz.Matrix(zoom, zoom)
                    pix = fpage.get_pixmap(matrix=mat, alpha=False)
                    pil_img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    del pix
                except Exception as e:
                    logger.warning(f'fitz page {p_idx+1} render failed: {e}, falling back to pdf2image')
                    pil_img = None

            # 2. Fallback to single-page pdf2image
            if pil_img is None:
                try:
                    from pdf2image import convert_from_path
                    rendered = convert_from_path(
                        input_path,
                        dpi=dpi,
                        first_page=p_idx + 1,
                        last_page=p_idx + 1,
                        poppler_path=poppler_path
                    )
                    if rendered:
                        pil_img = rendered[0]
                except Exception as e:
                    logger.error(f'pdf2image single page {p_idx+1} render failed: {e}')

            if pil_img is None:
                raise RuntimeError(f'Failed to render page {p_idx+1} for OCR.')

            pil_img.info['dpi'] = (dpi, dpi)  # lets OCR convert pixel measurements to points
            yield (seq_idx + 1, total_to_process, pil_img)
            del pil_img
            gc.collect()
    finally:
        if fitz_doc is not None:
            try:
                fitz_doc.close()
            except Exception:
                pass
