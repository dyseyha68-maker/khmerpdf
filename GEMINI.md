# KhmerPDF Workspace Rules & Architecture Guidelines

## 1. OCR Architecture & Multi-Engine Hierarchy
`ocr_pdf` in [`apps/pdf/tasks.py`](apps/pdf/tasks.py) is a thin orchestrator. The engine code lives in
[`apps/pdf/ocr/`](apps/pdf/ocr): `pipeline.py` (engine selection, retry/backoff, per-page cascade),
`pages.py` (page streaming), `docx_writer.py` (Word output) and `engines/` (one module per engine, each
exposing an `OCREngine` subclass with `run(doc, page, gray) -> chars_written`).

Per page, the first engine that returns text wins; an engine that errors or returns nothing hands the page on:
1. **Claude AI Vision** (`engines/claude.py`): Anthropic vision, returns structured JSON (`heading`, `paragraph`, `list_item`, `table`).
2. **Google Gemini Vision** (`engines/gemini.py`): same prompt (`engines/prompt.py`), very cheap, free tier available.
3. **Google Cloud Vision** (`engines/gcv.py`): `DOCUMENT_TEXT_DETECTION`. Only used when neither Claude nor Gemini is enabled.
4. **Netra-OCR** (`engines/netra.py`): local CNN-Transformer with YOLO line detection, for `khm` / `eng+khm` when `local_engine` is `khmerocr` (legacy value; it now means Netra).
5. **Tesseract** (`engines/tesseract.py`): always last; spatial layout and table reconstruction.

Cloud engines (1-3) retry twice with 2s/4s backoff. If `OCRSettings.fallback_to_tesseract` is off, a cloud
engine failure aborts the job instead of falling through. Pages where no engine produced text are reported
as `failed_pages` in the job status. Add a new engine by writing an `OCREngine` subclass and registering it in
`pipeline.build_engines`.

## 2. Memory-Safe Page Streaming Generator
- **Critical Rule**: NEVER load all pages of a multi-page PDF into memory simultaneously (e.g. avoid `convert_from_path(input_path)` without page limits). Fly.io/VPS VMs run with limited memory (1GB RAM) and will crash from Linux Out-Of-Memory (OOM) kills.
- **Always use `_iter_document_pages(input_path, page_range_str, max_limit, dpi)`** (in [`apps/pdf/ocr/pages.py`](apps/pdf/ocr/pages.py)):
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

## 5. Zero-Cost Local Engine (Netra-OCR)
- **Model**: [`netra-ai-lab/Netra-OCR`](https://github.com/netra-ai-lab/Netra-OCR)
  - Squeeze-and-Excitation Transformer with YOLOv2.6s text-line detection and Khmer Character Cluster (KCC) tokenizer.
  - Runs on CPU (upstream uses ONNX Runtime; weights download from Hugging Face on first use and are cached). Weights/licences: see upstream `THIRD_PARTY_LICENSES.md` (default YOLO detector is AGPL-3.0).
  - Outperforms Tesseract on complex subscript consonant clusters (`ជើង`) and legal terminology with near-human transcription fidelity.
  - Implemented in `_netraocr_page()` in [`apps/pdf/ocr/engines/netra.py`](apps/pdf/ocr/engines/netra.py) with streaming compatibility.
  - Automatically formats native Word (.docx) elements using `Khmer OS Battambang` for body text and headings (headings are bold and larger).
  - Configurable via `OCRSettings.local_engine` (`khmerocr` = Netra, or `tesseract`) in [`apps/pdf/models.py`](apps/pdf/models.py).


