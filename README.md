# KhmerPDF

A Django web app of PDF tools built for Khmer-speaking users (English / Khmer UI).

## Tools

| Page | API | What it does |
|---|---|---|
| `/pdf-compress/` | `POST /api/compress/` | Compress (Ghostscript, PyMuPDF fallback) |
| `/pdf-merge/` | `POST /api/merge/` | Merge PDFs |
| `/pdf-split/` | `POST /api/split/` | Split by range |
| `/pdf-organize/` | `POST /api/organize/` | Reorder / delete / insert pages |
| `/pdf-protect/` | `POST /api/protect/` | Password-protect |
| `/pdf-ocr/` | `POST /api/ocr/` | OCR a PDF/image to an editable Word file (Khmer-focused) |
| `/pdf-to-image/` | `POST /api/pdf-to-image/` | PDF pages to images |
| `/image-to-pdf/` | `POST /api/image-to-pdf/` | Images to PDF |

Also: a Gregorian/Buddhist-Era calendar with public holidays (`/pdf-calendar/`, see
[CALENDAR_ADMIN.md](CALENDAR_ADMIN.md)), a KHQR donation page, and admin-managed ad snippets,
site translations and page-visit stats.

Every API call creates a `Job`; poll `GET /api/job/<uuid>/` for `status`, `current_page` /
`total_pages` and, for OCR, `failed_pages`.

## Project layout

```
config/             settings, urls, celery
apps/pdf/
  views.py          pages + API endpoints
  tasks.py          PDF tools and the ocr_pdf orchestrator
  models.py         Job, OCRSettings, Holiday, CalendarEvent, SiteTranslation, AdSnippet, PageVisit
  admin.py          admin panels (OCR keys/engines, calendar, ads, translations)
  ocr/
    pipeline.py       engine selection + per-page fallback cascade (retry/backoff)
    pages.py          memory-safe page streaming (one page in RAM at a time)
    docx_writer.py    Word (.docx) builders using Khmer OS fonts
    engines/          claude, gemini, gcv (Google Vision), netra, shared prompt
templates/  static/  locale/
```

## OCR in short

Per page, the first engine that returns text wins.

- **Khmer and Khmer + English files:** **Netra-OCR** (local, free) first, then **Claude → Gemini →
  Google Vision** as fallbacks if they are enabled.
- **English-only files:** **Claude → Gemini → Google Vision** (Vision only if neither AI engine is on).
  There is no local English engine, so **at least one cloud engine must be enabled**, otherwise the
  job fails with a clear message.

When a job finishes, the OCR page shows a result panel (pages converted, pages with no text, a preview of the
Word file, and the download button). Dev tools: `ocr_format_bench`, `ocr_layout`, `ocr_compare` (see GEMINI.md).

Engines and API keys are configured in the Django admin under *OCR Settings*. Cloud engines retry
twice with backoff; if "continue to the next engine" is switched off in settings, a cloud failure
aborts the job instead. Details and rules: [GEMINI.md](GEMINI.md).

## Local setup

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py populate_calendar     # optional: seed holidays
python manage.py runserver             # or run_local.bat on Windows
```

Tasks run in-process (`CELERY_TASK_ALWAYS_EAGER = True`), so Redis is **not** required for dev.

External binaries (not tracked in git):

- **Poppler** (PDF to image, OCR fallback rendering). Windows: run `install_poppler.bat`, which
  downloads it to `D:\poppler`, then `move_poppler.bat` to place it at `<repo>\poppler`
  (or set `POPPLER_PATH` to its `Library\bin`). Linux/Docker: `poppler-utils`.
- **Ghostscript** (optional, better compression; PyMuPDF is used if absent).

## Deploy

`Dockerfile` / `docker-compose.yml`, `Procfile`, `fly.toml`, or `setup_server.sh` + `start.sh`
for a VPS. Copy `.env.example` to `.env` and set `SECRET_KEY` and `DEBUG=False`.

### Job queue
In production every tool runs through Celery/Redis (`CELERY_ALWAYS_EAGER=False`, set in `docker-compose.yml`):
the API answers `pending` at once and the page polls `/api/job/<id>/`. `docker-compose.yml` runs
`celery` (general PDF tools, concurrency 2) and `celery-ocr` (OCR only, concurrency 1, preloads the Netra
model at start-up and keeps its model cache on the `/data` volume). Locally the default is
`CELERY_ALWAYS_EAGER=True`: tasks run inline and no Redis is needed. A job still `pending`/`processing`
after 3 hours (worker died) is reported as failed. Run the tests with `python manage.py test apps.pdf`
(they use an in-memory broker and a test worker).
