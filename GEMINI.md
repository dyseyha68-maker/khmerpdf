# KhmerPDF Workspace Rules & Architecture Guidelines

## 1. OCR Architecture & Multi-Engine Hierarchy
The OCR pipeline in [`apps/pdf/tasks.py`](apps/pdf/tasks.py) operates on a tiered fallback model:
1. **Claude AI Vision (Primary High-Fidelity Multimodal)**:
   - Uses Anthropic Vision (`claude-3-5-sonnet-20241022` or `claude-3-5-haiku-20241022`) via `_claude_ocr_page()`.
   - Transcribes text and reconstructs full document structures into a strict JSON schema: `heading`, `paragraph`, `list_item`, `table` (grid or borderless).
   - Reconstructs tables, side-by-side letterheads, signature sections, and dates with high fidelity.
2. **Google Gemini Flash Vision (Ultra Low-Cost / Free Tier AI)**:
   - Uses Google Gemini Vision (`gemini-1.5-flash` or `gemini-2.0-flash`) via `_gemini_ocr_page()`.
   - ~100x cheaper than Sonnet, very fast (1-2s/page), and free tier available (15 RPM from Google AI Studio).
   - Solves Khmer complex consonant clustering (`ជើង`) with full linguistic comprehension.
3. **Google Cloud Vision (Traditional Cloud OCR)**:
   - Uses `DOCUMENT_TEXT_DETECTION` via `_vision_ocr_page()`.
4. **KhmerOCR ONNX (Zero-Cost Local Deep Learning)**:
   - Local ONNX models (`det.onnx` + `rec.onnx` ~25MB) running on CPU via `_khmerocr_page()`.
   - Trained on 3M lines, 800+ fonts; detects Moul vs Regular font styles.
5. **Local Tesseract OCR (Legacy Offline Fallback)**:
   - Uses `pytesseract.image_to_data` for spatial layout reconstruction, falling back to `--psm 6`.

## 2. Memory-Safe Page Streaming Generator
- **Critical Rule**: NEVER load all pages of a multi-page PDF into memory simultaneously (e.g. avoid `convert_from_path(input_path)` without page limits). Fly.io/VPS VMs run with limited memory (1GB RAM) and will crash from Linux Out-Of-Memory (OOM) kills.
- **Always use `_iter_document_pages(input_path, page_range_str, max_limit, dpi)`**:
  - Renders pages one by one using PyMuPDF (`fitz`), which operates directly in C/C++ in milliseconds with < 60MB RAM footprint.
  - Automatically falls back to single-page `pdf2image` if fitz encounters rare stream issues.
  - Deletes each rendered page and forces garbage collection immediately: `del page; del gray; gc.collect()`.

## 3. Khmer Typography & Word Document (.docx) Standards
- **Font Requirement**: All Khmer text generated in Microsoft Word (`.docx`) output MUST use **`Khmer OS Battambang`**.
  - Cambodian government, business, and school laptops have `Khmer OS Battambang` pre-installed.
- **Complex Script Setting**:
  - Microsoft Word requires explicit setting of the Complex Script font (`w:cs`), along with `w:ascii`, `w:hAnsi`, and `w:eastAsia` on `rFonts`:
    ```python
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.get_or_add_rFonts()
    rFonts.set(qn('w:cs'), 'Khmer OS Battambang')
    rFonts.set(qn('w:ascii'), font_name)
    rFonts.set(qn('w:hAnsi'), font_name)
    rFonts.set(qn('w:eastAsia'), font_name)
    ```
  - The default `Normal` style Complex Script in `doc.styles['Normal']` must also be set to `Khmer OS Battambang`.

## 4. Human-in-the-Loop & UX Controls
- **Large Document Pre-Selection**: Documents with > 30 pages automatically display the `.large-doc-banner` and pre-select the first 25 pages by default to prevent accidental overload.
- **Batching & Range**: Users can select 25-page batch chips or custom ranges (`1-20, 25-30`).
- **Live Progress Updates**: Tasks update `job.files = {'current_page': page_num, 'total_pages': total_pages}` on each iteration, which `job_status` serves to frontend progress bars.

## 5. Zero-Cost Local Engine (KhmerOCR ONNX)
- **Model**: [`seanghay/KhmerOCR`](https://github.com/seanghay/KhmerOCR)
  - Trained on 3 million text lines over 800+ Khmer fonts.
  - ONNX Runtime (`det.onnx` ~10.8MB, `rec.onnx` ~13.8MB) running on CPU with minimal RAM (<30MB) and fast inference (~0.3-0.8s/page).
  - Outperforms Tesseract on complex subscript consonant clusters (`ជើង`) and stylized fonts.
  - Implemented in `_khmerocr_page()` in [`apps/pdf/tasks.py`](apps/pdf/tasks.py) with streaming compatibility.
  - Automatically identifies font styles: Moul vs Regular (`Khmer OS Muol Light` for headings, `Khmer OS Battambang` for body).
  - Configurable via `OCRSettings.local_engine` (`khmerocr` vs `tesseract`) in [`apps/pdf/models.py`](apps/pdf/models.py).


