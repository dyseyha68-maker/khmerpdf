"""Common interface for OCR engines."""


class OCREngine:
    """One OCR backend. `run` writes the recognised page into the Word `doc`
    and returns the number of characters added (0 = nothing usable, so the
    pipeline moves on to the next engine).

    remote:   hits an external service, so transient errors are retried.
    optional: failure is tolerated only while the admin keeps fallback enabled;
              with fallback off, the error aborts the job.
    """
    name = 'engine'
    remote = False
    optional = False

    def run(self, doc, page, gray):
        raise NotImplementedError
