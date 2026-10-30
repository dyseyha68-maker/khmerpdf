import os
import uuid
import zipfile
import subprocess
import logging
import json
import numpy as np
from celery import shared_task
from django.conf import settings
import fitz
from PIL import Image
import io
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

logger = logging.getLogger(__name__)


def _pdf_has_gs_incompatible_images(path):
    """
    Return True if the PDF contains images that Ghostscript cannot compress
    correctly — specifically, FlateDecode streams that embed PNG data.
    GS mis-interprets the embedded PNG bytes as raw pixels, producing
    blank or corrupted output.  PyMuPDF handles these correctly.
    """
    try:
        doc = fitz.open(path)
        for page in doc:
            for img_info in page.get_images():
                xref = img_info[0]
                xobj = doc.xref_object(xref)
                if '/FlateDecode' in xobj:
                    base = doc.extract_image(xref)
                    if base.get('ext') == 'png':
                        doc.close()
                        return True
        doc.close()
    except Exception:
        pass
    return False


def compress_with_ghostscript(input_path, output_path, compression_level='recommended'):
    """
    Professional PDF compression using Ghostscript with ilovepdf-grade parameters.

    Unlike blunt preset-only commands, we override DPI and JPEG quality independently
    via `-c setdistillerparams`.  This is the key reason ilovepdf "Extreme" still
    looks crisp: they use 96 DPI (not 72) and a moderate JPEG QFactor (not max).

    QFactor reference (Ghostscript / Distiller):
        0.10 → ~q95  (near-lossless)
        0.15 → ~q85  (high quality)
        0.40 → ~q65  (good balance)
        0.76 → ~q45  (ilovepdf "extreme" — readable images, tiny files)
        1.30 → ~q15  (very low quality)
    """

    original_size    = os.path.getsize(input_path)
    original_size_mb = original_size / (1024 * 1024)
    logger.info(f'Compressing {original_size_mb:.2f} MB  level={compression_level}')

    # ── Per-level tuning ───────────────────────────────────────────────────────
    configs = {
        # "Extreme" card — 96 DPI (vs old 72), still big savings
        'low': {
            'pdfsettings': '/screen',
            'color_dpi':    96,
            'gray_dpi':     96,
            'mono_dpi':    150,
        },
        # "Recommended" card
        'recommended': {
            'pdfsettings': '/ebook',
            'color_dpi':   150,
            'gray_dpi':    150,
            'mono_dpi':    200,
        },
        # "Less" card
        'high': {
            'pdfsettings': '/printer',
            'color_dpi':   200,
            'gray_dpi':    200,
            'mono_dpi':    300,
        },
    }

    cfg = configs.get(compression_level, configs['recommended'])

    timeout = max(120, int(original_size_mb * 6))
    timeout = min(timeout, 900)

    # On Windows, Ghostscript is gswin64c / gswin32c; on Linux/Mac it's gs
    import sys as _sys
    if _sys.platform == 'win32':
        import shutil as _shutil
        gs_exe = (
            _shutil.which('gswin64c') or
            _shutil.which('gswin32c') or
            _shutil.which('gs') or
            'gswin64c'
        )
    else:
        gs_exe = 'gs'

    cmd = [
        gs_exe,
        '-sDEVICE=pdfwrite',
        '-dCompatibilityLevel=1.4',
        # ── NO -dPDFSETTINGS preset ───────────────────────────────────────────
        # Presets (/screen, /ebook, etc.) force colour-space conversion to sRGB
        # which turns DeviceCMYK / ICCBased images BLACK on Asian scanned docs.
        # We set only what we need manually so colours are never touched.
        '-dNOPAUSE', '-dQUIET', '-dBATCH', '-dSAFER',

        # ── Image downsampling ────────────────────────────────────────────────
        '-dDownsampleColorImages=true',
        '-dDownsampleGrayImages=true',
        '-dDownsampleMonoImages=true',
        '-dColorImageDownsampleType=/Bicubic',
        '-dGrayImageDownsampleType=/Bicubic',
        '-dMonoImageDownsampleType=/Subsample',
        f'-dColorImageResolution={cfg["color_dpi"]}',
        f'-dGrayImageResolution={cfg["gray_dpi"]}',
        f'-dMonoImageResolution={cfg["mono_dpi"]}',
        '-dColorImageDownsampleThreshold=1.0',
        '-dGrayImageDownsampleThreshold=1.0',
        '-dMonoImageDownsampleThreshold=1.0',

        # ── Structural optimisations ──────────────────────────────────────────
        '-dCompressFonts=true',
        '-dSubsetFonts=true',
        '-dDetectDuplicateImages=true',
        '-dRemoveUnusedResources=true',
        '-dMaxBitmap=500000000',

        # ── Output ────────────────────────────────────────────────────────────
        f'-sOutputFile={output_path}',
        input_path,
    ]

    logger.info(f'GS: dpi={cfg["color_dpi"]}  (no preset — colour-safe)')

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise Exception(f'Compression timed out ({original_size_mb:.1f} MB file)')
        if proc.returncode != 0:
            raise Exception(f'Ghostscript error: {stderr.decode(errors="replace")[:300]}')
    except Exception:
        raise

    if not os.path.exists(output_path):
        raise Exception('Ghostscript produced no output file')

    out_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info(f'GS result: {out_mb:.2f} MB (was {original_size_mb:.2f} MB)')

    # If GS made the file larger, fall back
    if os.path.getsize(output_path) >= original_size:
        os.remove(output_path)
        raise Exception('Ghostscript did not reduce file size; trying fallback')

    # ── Blank / black-page guard ──────────────────────────────────────────────
    # GS can produce blank-white or solid-black pages on unusual PDFs.
    # We check output pages — but first check if the SOURCE also renders
    # abnormally via fitz (rotation/encoding quirks). If the source already
    # looks bad in fitz, trust GS; only fall back if source looked normal.
    try:
        def _fitz_page_stats(path, page_num=0):
            """Return (white_ratio, black_ratio) for a page rendered at 36 DPI."""
            try:
                d = fitz.open(path)
                p = d[page_num]
                pix = p.get_pixmap(dpi=36, alpha=False)
                s, n = pix.samples, len(pix.samples) // 3
                d.close()
                if n == 0:
                    return 0.0, 0.0
                w = sum(1 for i in range(0,len(s),3) if s[i]>230 and s[i+1]>230 and s[i+2]>230)
                b = sum(1 for i in range(0,len(s),3) if s[i]<25  and s[i+1]<25  and s[i+2]<25)
                return w/n, b/n
            except Exception:
                return 0.0, 0.0

        src_white, src_black = _fitz_page_stats(input_path)
        src_bad = src_white >= 0.95 or src_black >= 0.90
        if src_bad:
            logger.info(
                f'Source renders abnormally in fitz (white={src_white:.0%} '
                f'black={src_black:.0%}) — trusting GS output'
            )
        else:
            # Source looks normal; verify GS output is not blank/black
            check_doc = fitz.open(output_path)
            bad_count = 0
            total_checked = min(len(check_doc), 3)
            for page in check_doc:
                pix = page.get_pixmap(dpi=36, alpha=False)
                s, n = pix.samples, len(pix.samples) // 3
                if n == 0:
                    bad_count += 1
                else:
                    white_px = sum(1 for i in range(0,len(s),3) if s[i]>230 and s[i+1]>230 and s[i+2]>230)
                    black_px = sum(1 for i in range(0,len(s),3) if s[i]<25  and s[i+1]<25  and s[i+2]<25)
                    if (white_px/n) >= 0.95 or (black_px/n) >= 0.90:
                        bad_count += 1
                        logger.warning(f'GS bad page {page.number}: white={white_px/n:.0%} black={black_px/n:.0%}')
                if page.number + 1 >= total_checked:
                    break
            check_doc.close()
            if bad_count >= total_checked:
                logger.warning('GS produced blank/black pages — falling back to PyMuPDF')
                os.remove(output_path)
                raise Exception('Ghostscript produced blank/black output; trying fallback')
    except fitz.FileNotFoundError:
        raise Exception('Could not verify GS output; trying fallback')

    return output_path


def compress_with_pymupdf(input_path, output_path, compression_level='recommended'):
    """
    Fallback compressor using PyMuPDF.

    Strategy: extract every raster image from the PDF, re-encode it as JPEG at
    the chosen quality/DPI, then replace the stream in-place.  Vectors and text
    are untouched, so readability is preserved.
    """

    # Per-level settings: (jpeg_quality, max_dimension_px)
    # These match the Ghostscript QFactor choices above.
    level_cfg = {
        'low':         (35, 800),    # Extreme  — readable, small
        'recommended': (65, 1600),   # Balanced
        'high':        (82, 2400),   # Less compression
    }
    quality, max_dim = level_cfg.get(compression_level, (65, 1600))

    doc = fitz.open(input_path)
    replaced = 0
    seen_xrefs = set()

    for page in doc:
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                base_img  = doc.extract_image(xref)
                img_bytes = base_img['image']

                # Skip masks / tiny images — not worth re-encoding
                if len(img_bytes) < 2048:
                    continue

                pil_img = Image.open(io.BytesIO(img_bytes))

                # Normalise colour mode
                if pil_img.mode == 'CMYK':
                    pil_img = pil_img.convert('RGB')
                elif pil_img.mode in ('RGBA', 'LA', 'P'):
                    bg = Image.new('RGB', pil_img.size, (255, 255, 255))
                    if pil_img.mode == 'P':
                        pil_img = pil_img.convert('RGBA')
                    if pil_img.mode in ('RGBA', 'LA'):
                        bg.paste(pil_img, mask=pil_img.split()[-1])
                    else:
                        bg.paste(pil_img)
                    pil_img = bg
                elif pil_img.mode != 'RGB':
                    pil_img = pil_img.convert('RGB')

                # ── Inverted-image guard ──────────────────────────────────
                # Some PDFs (e.g. rotated scanner outputs) store images as
                # photographic negatives. Mean < 20 → almost all pixels are
                # near-black → invert so the output is readable.
                arr = np.array(pil_img)
                if arr.mean() < 20:
                    pil_img = Image.fromarray(255 - arr)
                    logger.info(f'Inverted near-black image xref={xref}')

                # Downsample if larger than max_dim
                if pil_img.width > max_dim or pil_img.height > max_dim:
                    pil_img.thumbnail((max_dim, max_dim), Image.LANCZOS)

                new_w, new_h = pil_img.size

                # Re-encode as JPEG
                buf = io.BytesIO()
                pil_img.save(buf, format='JPEG', quality=quality,
                             optimize=True, progressive=True)
                new_bytes = buf.getvalue()

                # Only replace if we actually saved space
                if len(new_bytes) >= len(img_bytes):
                    continue

                # ── Proper JPEG injection ──────────────────────────────────
                # update_stream(compress=True) wraps JPEG in FlateDecode and
                # leaves the original filter untouched → black/corrupted pages.
                # Correct approach:
                #   1. Store raw JPEG bytes (compress=False)
                #   2. Patch the XObject dict to reflect DCTDecode + new dims
                doc.update_stream(xref, new_bytes, compress=False)
                doc.xref_set_key(xref, 'Filter', '/DCTDecode')
                doc.xref_set_key(xref, 'Width',  str(new_w))
                doc.xref_set_key(xref, 'Height', str(new_h))
                try:
                    if 'DecodeParms' in doc.xref_object(xref):
                        doc.xref_del_key(xref, 'DecodeParms')
                except Exception:
                    pass
                replaced += 1

            except Exception:
                continue   # skip unprocessable image, leave original

    logger.info(f'PyMuPDF: replaced {replaced} image streams')

    # Save with maximum structural compression
    try:
        doc.save(output_path, deflate=True, deflate_images=True, garbage=4, clean=True)
    except Exception:
        doc.save(output_path, deflate=True, garbage=4, clean=True)
    doc.close()


@shared_task
def compress_pdf(job_id):
    from apps.pdf.models import Job
    from django.db import close_old_connections
    close_old_connections()   # ensure fresh DB connection in this thread

    Job.objects.filter(id=job_id).update(status='processing')
    job = Job.objects.get(id=job_id)
    
    compression_level = job.compression_level or 'recommended'
    
    try:
        file_ids = job.files if job.files else []
        
        if file_ids and len(file_ids) > 1:
            # Multiple files - create ZIP
            first_base_name = ''
            for idx, file_id in enumerate(file_ids):
                try:
                    job_file = Job.objects.get(id=file_id)
                    if job_file.file and os.path.exists(job_file.file.path):
                        if idx == 0:
                            first_base_name = os.path.splitext(os.path.basename(job_file.file.name))[0]
                        break
                except:
                    continue
            
            zip_filename = f'{first_base_name}_compressed.zip' if first_base_name else f'compressed_{uuid.uuid4().hex[:8]}.zip'
            zip_path = os.path.join(settings.MEDIA_ROOT, 'processed', zip_filename)
            os.makedirs(os.path.dirname(zip_path), exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for file_id in file_ids:
                    try:
                        job_file = Job.objects.get(id=file_id)
                        if job_file.file and os.path.exists(job_file.file.path):
                            input_path = job_file.file.path
                            original_size = os.path.getsize(input_path)
                            
                            base_name = os.path.splitext(os.path.basename(job_file.file.name))[0]
                            output_filename = f'{base_name}_compressed.pdf'
                            output_path = os.path.join(settings.MEDIA_ROOT, 'processed', f'{uuid.uuid4().hex[:8]}_compressed.pdf')
                            
                            # Skip GS for PDFs with FlateDecode+PNG images
                            if _pdf_has_gs_incompatible_images(input_path):
                                logger.info(f'FlateDecode+PNG — using PyMuPDF directly for {base_name}')
                                compress_with_pymupdf(input_path, output_path, compression_level)
                            else:
                                try:
                                    compress_with_ghostscript(input_path, output_path, compression_level)
                                    logger.info(f'Used Ghostscript for {base_name}')
                                except Exception as gs_err:
                                    logger.warning(f'Ghostscript failed, falling back to PyMuPDF: {gs_err}')
                                    compress_with_pymupdf(input_path, output_path, compression_level)
                            
                            compressed_size = os.path.getsize(output_path)
                            if compressed_size >= original_size:
                                # Use original if compression made it larger
                                logger.info(f'Compression did not help for {base_name}; using original')
                                os.remove(output_path)
                                zip_file.write(input_path, output_filename)
                            else:
                                zip_file.write(output_path, output_filename)
                                os.remove(output_path)
                    except Exception as e:
                        continue
            
            job.result.save(zip_filename, open(zip_path, 'rb'))
            os.remove(zip_path)
            Job.objects.filter(id=job_id).update(status='done')
            return {'status': 'done', 'job_id': str(job_id)}
        
        else:
            # Single file
            input_path = job.file.path
            original_size = os.path.getsize(input_path)
            base_name = os.path.splitext(os.path.basename(job.file.name))[0]
            output_filename = f'{base_name}_compressed.pdf'
            output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
            
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Skip GS for PDFs with FlateDecode+PNG images (GS produces blank output)
            if _pdf_has_gs_incompatible_images(input_path):
                logger.info('FlateDecode+PNG detected — using PyMuPDF directly')
                try:
                    compress_with_pymupdf(input_path, output_path, compression_level)
                except Exception as pymupdf_err:
                    logger.error(f'PyMuPDF failed: {pymupdf_err}')
                    raise Exception(f'Compression failed: {pymupdf_err}')
            else:
                try:
                    compress_with_ghostscript(input_path, output_path, compression_level)
                    logger.info('Used Ghostscript for single file')
                except Exception as gs_err:
                    logger.warning(f'Ghostscript failed, falling back to PyMuPDF: {gs_err}')
                    try:
                        compress_with_pymupdf(input_path, output_path, compression_level)
                    except Exception as pymupdf_err:
                        logger.error(f'Both compression methods failed: GS={gs_err}, PyMuPDF={pymupdf_err}')
                        raise Exception(f'Compression failed: {pymupdf_err}')
            
            if not os.path.exists(output_path):
                raise Exception(f'Compression failed: output file not created')

            compressed_size = os.path.getsize(output_path)

            if compressed_size >= original_size:
                # Compression made it larger or equal — return original unchanged
                logger.info(
                    f'Compression did not reduce size ({compressed_size} >= {original_size}); '
                    'returning original file'
                )
                os.remove(output_path)
                job.result.save(output_filename, open(input_path, 'rb'))
                compressed_size = original_size
            else:
                job.result.save(output_filename, open(output_path, 'rb'))
                os.remove(output_path)

            Job.objects.filter(id=job_id).update(status='done')

            return {'status': 'done', 'job_id': str(job_id), 'original': original_size, 'compressed': compressed_size}

    except Exception as e:
        logger.error(f'compress_pdf failed: {e}', exc_info=True)
        Job.objects.filter(id=job_id).update(status='failed', error_message=str(e)[:500])
        raise


def merge_pdf(job_id):
    from apps.pdf.models import Job
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()
    
    try:
        file_paths = job.files if job.files else []
        
        first_base_name = ''
        for idx, file_id in enumerate(file_paths):
            try:
                job_file = Job.objects.get(id=file_id)
                if job_file.file and os.path.exists(job_file.file.path):
                    if idx == 0:
                        first_base_name = os.path.splitext(os.path.basename(job_file.file.name))[0]
                    break
            except:
                continue
        
        output_filename = f'{first_base_name}_merged.pdf' if first_base_name else f'merged_{uuid.uuid4().hex[:8]}.pdf'
        output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
        
        if job.result:
            job.result.delete()
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        writer = PdfWriter()
        
        for file_id in file_paths:
            try:
                job_file = Job.objects.get(id=file_id)
                if job_file.file and os.path.exists(job_file.file.path):
                    reader = PdfReader(job_file.file.path)
                    for page in reader.pages:
                        writer.add_page(page)
            except:
                continue
        
        with open(output_path, 'wb') as f:
            writer.write(f)
        
        job.result.save(output_filename, open(output_path, 'rb'))
        
        os.remove(output_path)
        
        job.status = 'done'
        job.save()
        
        return {'status': 'done', 'job_id': str(job_id)}
        
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.save()
        raise


def split_pdf(job_id):
    from apps.pdf.models import Job
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()
    
    try:
        input_path = job.file.path
        page_range = job.page_range or '1'
        split_mode = job.compression_level or 'range'
        
        base_name = os.path.splitext(os.path.basename(job.file.name))[0]
        
        reader = PdfReader(input_path)
        total_pages = len(reader.pages)
        
        if split_mode == 'every':
            import zipfile
            n = 3
            if page_range.startswith('every:'):
                n = int(page_range.split(':')[1]) or 3
            
            zip_filename = f'{base_name}_split.zip'
            zip_path = os.path.join(settings.MEDIA_ROOT, 'processed', zip_filename)
            os.makedirs(os.path.dirname(zip_path), exist_ok=True)
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for i in range(0, total_pages, n):
                    writer = PdfWriter()
                    for j in range(i, min(i + n, total_pages)):
                        writer.add_page(reader.pages[j])
                    
                    page_path = os.path.join(settings.MEDIA_ROOT, 'processed', f'page_{i//n+1}.pdf')
                    with open(page_path, 'wb') as f:
                        writer.write(f)
                    
                    zip_file.write(page_path, f'pages_{i+1}-{min(i+n,total_pages)}.pdf')
                    os.remove(page_path)
            
            job.result.save(zip_filename, open(zip_path, 'rb'))
            os.remove(zip_path)
            
        else:
            output_filename = f'{base_name}_split.pdf'
            output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            writer = PdfWriter()
            pages = parse_page_range(page_range, total_pages)
            
            for page_num in pages:
                if 0 <= page_num < total_pages:
                    writer.add_page(reader.pages[page_num])
            
            with open(output_path, 'wb') as f:
                writer.write(f)
            
            job.result.save(output_filename, open(output_path, 'rb'))
            os.remove(output_path)
        
        job.status = 'done'
        job.save()
        
        return {'status': 'done', 'job_id': str(job_id)}
        
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.save()
        raise


@shared_task
def protect_pdf(job_id, password):
    """Lock a PDF (or batch of PDFs) with an open password using pikepdf
    (qpdf-backed), AES-256 (R=6 — the modern PDF 2.0 encryption revision).
    `password` arrives only as a task argument — it is never written to
    the Job row, so it never lands in the database or the admin panel."""
    from apps.pdf.models import Job
    from django.db import close_old_connections
    close_old_connections()
    import pikepdf

    Job.objects.filter(id=job_id).update(status='processing')
    job = Job.objects.get(id=job_id)

    def _encrypt_one(input_path, output_path):
        try:
            with pikepdf.open(input_path) as pdf:
                pdf.save(
                    output_path,
                    encryption=pikepdf.Encryption(owner=password, user=password, R=6),
                )
        except pikepdf.PasswordError:
            raise Exception('This PDF is already password-protected — remove the existing password first.')

    try:
        file_ids = job.files if job.files else []

        if file_ids and len(file_ids) > 1:
            # Multiple files - create ZIP
            first_base_name = ''
            for idx, file_id in enumerate(file_ids):
                try:
                    job_file = Job.objects.get(id=file_id)
                    if job_file.file and os.path.exists(job_file.file.path):
                        if idx == 0:
                            first_base_name = os.path.splitext(os.path.basename(job_file.file.name))[0]
                        break
                except Exception:
                    continue

            zip_filename = f'{first_base_name}_protected.zip' if first_base_name else f'protected_{uuid.uuid4().hex[:8]}.zip'
            zip_path = os.path.join(settings.MEDIA_ROOT, 'processed', zip_filename)
            os.makedirs(os.path.dirname(zip_path), exist_ok=True)

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for file_id in file_ids:
                    try:
                        job_file = Job.objects.get(id=file_id)
                        if job_file.file and os.path.exists(job_file.file.path):
                            input_path = job_file.file.path
                            base_name = os.path.splitext(os.path.basename(job_file.file.name))[0]
                            output_filename = f'{base_name}_protected.pdf'
                            output_path = os.path.join(settings.MEDIA_ROOT, 'processed', f'{uuid.uuid4().hex[:8]}_protected.pdf')

                            _encrypt_one(input_path, output_path)

                            zip_file.write(output_path, output_filename)
                            os.remove(output_path)
                    except Exception as e:
                        logger.warning(f'protect_pdf: skipping file {file_id}: {e}')
                        continue

            job.result.save(zip_filename, open(zip_path, 'rb'))
            os.remove(zip_path)
            Job.objects.filter(id=job_id).update(status='done')
            return {'status': 'done', 'job_id': str(job_id)}

        else:
            input_path = job.file.path
            base_name = os.path.splitext(os.path.basename(job.file.name))[0]
            output_filename = f'{base_name}_protected.pdf'
            output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            _encrypt_one(input_path, output_path)

            job.result.save(output_filename, open(output_path, 'rb'))
            os.remove(output_path)

            Job.objects.filter(id=job_id).update(status='done')
            return {'status': 'done', 'job_id': str(job_id)}

    except Exception as e:
        logger.error(f'protect_pdf failed: {e}', exc_info=True)
        Job.objects.filter(id=job_id).update(status='failed', error_message=str(e)[:500])
        raise


def parse_page_range(page_str, total_pages):
    pages = set()
    
    parts = page_str.split(',')
    for part in parts:
        part = part.strip()
        if '-' in part:
            start, end = part.split('-')
            start = int(start.strip()) - 1
            end = int(end.strip()) - 1
            for p in range(max(0, start), min(end + 1, total_pages)):
                pages.add(p)
        else:
            p = int(part.strip()) - 1
            if 0 <= p < total_pages:
                pages.add(p)
    
    return sorted(list(pages))


@shared_task
def organize_pdf(job_id, replace_files=None):
    from apps.pdf.models import Job
    import json
    import io
    import copy
    
    if replace_files is None:
        replace_files = {}
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()
    
    try:
        input_path = job.file.path
        page_order_json = job.page_range or '[]'
        
        try:
            page_order = json.loads(page_order_json.replace('\\/', '/'))
        except Exception:
            page_order = []
        
        reader = PdfReader(input_path)
        total_pages = len(reader.pages)
        
        # Preload any extra / inserted / replaced files into in-memory readers
        extra_readers = {}
        for idx, extra_file in replace_files.items():
            try:
                if hasattr(extra_file, 'read'):
                    content = extra_file.read()
                elif hasattr(extra_file, 'chunks'):
                    content = b''.join(extra_file.chunks())
                else:
                    with open(extra_file, 'rb') as ef:
                        content = ef.read()
                extra_readers[idx] = PdfReader(io.BytesIO(content))
            except Exception as ef_err:
                logger.error(f'Error loading extra file index {idx}: {ef_err}')
        
        output_filename = f'organized_{uuid.uuid4().hex[:8]}.pdf'
        output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        writer = PdfWriter()
        
        for page_info in page_order:
            is_blank = page_info.get('isBlank', False)
            is_extra = (
                page_info.get('isReplaced', False) or
                page_info.get('isInserted', False) or
                page_info.get('isExtra', False) or
                (page_info.get('replacedFileIndex') is not None) or
                (page_info.get('fileIndex') is not None)
            )
            file_idx = page_info.get('fileIndex') if page_info.get('fileIndex') is not None else page_info.get('replacedFileIndex')
            page_num = page_info.get('pageNum') if page_info.get('pageNum') is not None else page_info.get('replacedPageNum')
            rotation = int(page_info.get('rotation', 0) or 0) % 360
            
            target_page = None
            
            if is_blank:
                width, height = 612, 792
                if total_pages > 0:
                    try:
                        width = float(reader.pages[0].mediabox.width)
                        height = float(reader.pages[0].mediabox.height)
                    except Exception:
                        pass
                blank_page = writer.add_blank_page(width=width, height=height)
                if rotation:
                    blank_page.rotate(rotation)
                continue
            elif is_extra and file_idx in extra_readers and page_num is not None:
                extra_reader = extra_readers[file_idx]
                if 0 <= page_num - 1 < len(extra_reader.pages):
                    target_page = copy.copy(extra_reader.pages[page_num - 1])
            else:
                orig_page = page_info.get('original')
                page_idx = page_info.get('index')
                if orig_page is not None and isinstance(orig_page, int) and 0 <= orig_page - 1 < total_pages:
                    target_page = copy.copy(reader.pages[orig_page - 1])
                elif page_idx is not None and isinstance(page_idx, int) and 0 <= page_idx < total_pages:
                    target_page = copy.copy(reader.pages[page_idx])
            
            if target_page is not None:
                if rotation:
                    target_page.rotate(rotation)
                writer.add_page(target_page)
        
        with open(output_path, 'wb') as f:
            writer.write(f)
        
        with open(output_path, 'rb') as f:
            job.result.save(output_filename, f)
        
        if os.path.exists(output_path):
            os.remove(output_path)
        
        job.status = 'done'
        job.save()
        
        return {'status': 'done', 'job_id': str(job_id)}
        
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.save()
        raise


# ── Google Cloud Vision OCR (optional, admin-configured) ───────────────────────
# Off by default. When apps.pdf.models.OCRSettings.use_google_vision is turned
# on (with a valid service account JSON pasted into the admin), ocr_pdf below
# uses Vision's DOCUMENT_TEXT_DETECTION instead of Tesseract, per page, with
# an automatic per-page fallback to Tesseract on any failure (network, quota,
# bad credentials) unless the admin has switched that fallback off.

_VISION_LANG_HINTS = {
    'eng': ['en'],
    'khm': ['km'],
    'eng+khm': ['en', 'km'],
}


def _get_ocr_settings():
    from apps.pdf.models import OCRSettings
    return OCRSettings.objects.filter(pk=1).first()


def _build_vision_client(ocr_settings):
    """Build an authenticated Vision client from the admin-pasted service
    account JSON. Raises if the JSON is missing/malformed so the caller can
    log a clear error and fall back to Tesseract."""
    from google.cloud import vision
    from google.oauth2 import service_account

    info = json.loads(ocr_settings.google_credentials_json)
    credentials = service_account.Credentials.from_service_account_info(info)
    return vision.ImageAnnotatorClient(credentials=credentials)


def _get_vision_prompt(ocr_lang='eng'):
    lang_desc = "Khmer and English" if ocr_lang == 'eng+khm' else ("Khmer" if ocr_lang == 'khm' else "English")
    return (
        f"You are a world-class Khmer document transcription and layout reproduction engine for high-fidelity Microsoft Word (.docx) generation. "
        f"Transcribe and faithfully reconstruct the visual layout, text, and structure of this document image with 100% precision. "
        f"Document language: {lang_desc}.\n\n"
        "CRITICAL KHMER SCRIPT FIDELITY RULES:\n"
        "1. ABSOLUTELY NO FOREIGN SCRIPTS: The document is strictly Khmer. NEVER output Thai characters (e.g. NEVER write 'ตุลาคม' for 'តុលា'). Every character must be valid Khmer Unicode.\n"
        "2. EXACT WORD FOR WORD: Transcribe EXACTLY what is written. NEVER paraphrase, guess, or substitute legal vocabulary. For example, if it says 'ជនត្រូវចោទ', do NOT replace with 'ជនក្រីក្រ'. If it says 'ឥតគិតកម្រៃ', do NOT write 'ឥតគិតថ្លៃ'.\n"
        "3. PRESERVE SUBSCRIPT CONSONANTS (ជើង): Always transcribe subscript consonants with their coeng mark ( ្ ) (e.g. ន្ត, ង្ក, ម្ភ, ញ្ច, ដ្ឋ, ្រ, ្ល, ្វ, ្ត). Never drop subscripts.\n"
        "4. DISTINGUISH SIMILAR CONSONANTS: Distinguish carefully between 'ដ' and 'ត', 'ព' and 'ភ', 'ម' and 'ធ', 'ឈ' and 'ញ', 'ឌ' and 'អ៊ុ', 'ប' and 'ឋ'.\n"
        "5. PRESERVE DIACRITICS & NUMBERS: Preserve Bantoc ( ់ ), Tandakhat ( ៍ ), Reahmuk ( ៌ ), Treisap ( ៊ ), Musikatoan ( ៉ ). Transcribe Khmer numbers (១, ២, ៣, ៤, ៥, ៦, ៧, ៨, ៩, ០) accurately.\n"
        "6. INCLUDE ALL STAMP & SIGNATURE TEXT: Transcribe header stamps, office codes (e.g. 'លេខ ១០៣១ / ប្រ.កម/ភព/២៦'), and signer names (e.g. 'មេធាវី. ហុយ សុផល').\n"
        "- Preserve all dotted fill-in lines (e.g. '..........').\n\n"
        "STRUCTURAL SCHEMAS (Return a JSON array from top to bottom):\n"
        "1. Heading: {\"type\": \"heading\", \"level\": 1|2|3, \"align\": \"center\"|\"left\", \"text\": \"...\"}\n"
        "   - Use 'align': 'center' for top mottos ('ព្រះរាជាណាចក្រកម្ពុជា', 'ជាតិ សាសនា ព្រះមហាក្សត្រ') and main document titles ('លិខិតអញ្ជើញ...', 'សាលក្រម...', 'សេចក្តីសម្រេច...', 'ប្រកាស...', 'ពាក្យស្នើសុំ...').\n"
        "   - CRITICAL: The main document title (e.g. 'លិខិតអញ្ជើញមេធាវី', 'សេចក្តីសម្រេច', 'សាលក្រម') MUST be a separate 'heading' element AFTER the letterhead table. NEVER put the document title inside a letterhead table cell!\n"
        "2. Paragraph: {\"type\": \"paragraph\", \"align\": \"justify\"|\"left\"|\"right\"|\"center\", \"text\": \"...\"}\n"
        "   - Use 'align': 'justify' for multi-line body paragraphs, legal reasoning, requests, and narrative prose.\n"
        "   - IMPORTANT: ALL narrative body paragraphs (even short 1-line or 2-line paragraphs like 'អាស្រ័យដូចបានជម្រាបជូន...' or 'សូម លោកប្រធាន...') MUST be regular 'paragraph' elements with 'align': 'justify'. NEVER put body paragraphs into a table.\n"
        "   - Use 'align': 'right' for date and location lines (e.g. 'រាជធានីភ្នំពេញ ថ្ងៃទី...', 'ថ្ងៃ...ខែ...ឆ្នាំ... / ហត្ថលេខា'). Do NOT put dates in a table.\n"
        "   - Use 'align': 'left' for labels (e.g. '**ដើមចោទ ៖** ...', '**កម្មវត្ថុ ៖** ...', '**យោង ៖** ...'), short entries.\n"
        "   - Use '**bold text**' for labels, party names, headings, and case numbers as in the original.\n"
        "3. List Item: {\"type\": \"list_item\", \"list_type\": \"bullet\"|\"number\", \"text\": \"...\"}\n"
        "   - For all bullet lines starting with '-', output with a space after the dash (e.g. '- អញ្ជើញមេធាវី...').\n"
        "   - For 'យោង ៖' and 'ចម្លងជូន ៖', output each sub-reference as a separate line starting with '- '.\n"
        "4. Table: {\"type\": \"table\", \"layout\": \"borderless\"|\"grid\", \"rows\": [[{\"text\": \"...\", \"align\": \"center\"|\"left\"|\"right\", \"bold\": true|false}, ...]]}\n"
        "   - Use 'layout': 'borderless' ONLY for true multi-column horizontal sections:\n"
        "     * Side-by-side letterheads (e.g. Ministry/Court name on left, 'ព្រះរាជាណាចក្រកម្ពុជា' on right/center, room/stamp on right).\n"
        "     * Bottom approval & signature columns (e.g. a 3-column row with applicant/case on left, spacer in middle, judge/director on right).\n"
        "   - Use 'layout': 'grid' for standard bordered data tables.\n"
        "   - Use '\\n' for multiple lines inside a cell.\n"
        "5. Omit standalone running page numbers (e.g. standalone '1', '2', '១', '២' at page margins).\n\n"
        "CRITICAL: Return ONLY a raw JSON array starting with '[' and ending with ']'. Do not include markdown code block fences (no ```json)."
    )


def _claude_ocr_page(api_key, model, pil_image, ocr_lang='eng'):
    """Run Claude Vision OCR on one page image, returning layout-reconstructed markdown or JSON text."""
    import base64
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    buf = io.BytesIO()
    # JPEG format at quality 92 provides crisp clarity for OCR with low payload size
    pil_image.convert('RGB').save(buf, format='JPEG', quality=92)
    image_base64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    prompt = _get_vision_prompt(ocr_lang=ocr_lang)

    # Normalize model string to ensure valid Anthropic model identifier
    chosen_model = (model or '').strip()
    if not chosen_model or 'sonnet' in chosen_model.lower():
        chosen_model = 'claude-3-5-sonnet-20241022'
    elif 'haiku' in chosen_model.lower():
        chosen_model = 'claude-3-5-haiku-20241022'

    create_kwargs = {
        'model': chosen_model,
        'max_tokens': 8192,
        'messages': [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_base64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
    }

    response = client.messages.create(**create_kwargs)

    text_parts = []
    for block in response.content:
        if getattr(block, 'text', None):
            text_parts.append(block.text)

    return "\n".join(text_parts).strip()


def _gemini_ocr_page(api_key, model, pil_image, ocr_lang='eng'):
    """Run Google Gemini Vision OCR on one page image, returning layout-reconstructed JSON or markdown text."""
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model_name = (model or '').strip()
    if not model_name or model_name in ('gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-3.5-flash-lite'):
        # gemini-flash-lite-latest: fastest (~1s/page), free tier, best output completeness for Khmer
        chosen_model = 'gemini-flash-lite-latest'
    else:
        chosen_model = model_name

    prompt = _get_vision_prompt(ocr_lang=ocr_lang)

    # Convert to RGB mode
    rgb_img = pil_image.convert('RGB') if pil_image.mode != 'RGB' else pil_image

    gen_model = genai.GenerativeModel(chosen_model)
    response = gen_model.generate_content(
        [rgb_img, prompt],
        generation_config={
            'temperature': 0.0,
            'max_output_tokens': 8192,
            'response_mime_type': 'application/json',
        }
    )
    return (response.text or "").strip()


def _add_structured_json_to_docx(doc, items):
    """
    Directly converts Claude's structured JSON array of document elements
    (headings, paragraphs with explicit alignments, lists, tables) into
    native Microsoft Word elements with accurate Khmer font styling.
    """
    import re
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls, qn

    ALIGN_MAP = {
        'center': WD_ALIGN_PARAGRAPH.CENTER,
        'justify': WD_ALIGN_PARAGRAPH.JUSTIFY,
        'left': WD_ALIGN_PARAGRAPH.LEFT,
        'right': WD_ALIGN_PARAGRAPH.RIGHT,
    }

    def _add_styled_runs(paragraph, text, is_bold=False, font_size=11, font_name_override=None):
        parts = text.split('**')
        chars = 0
        for idx, part in enumerate(parts):
            if not part:
                continue
            run_bold = is_bold or (idx % 2 == 1)
            has_khmer = any('\u1780' <= c <= '\u17FF' or '\u19E0' <= c <= '\u19FF' for c in part)
            if has_khmer:
                font_name = font_name_override if font_name_override else 'Khmer OS Battambang'
            else:
                font_name = 'Calibri'

            run = paragraph.add_run(part)
            run.bold = run_bold
            run.font.name = font_name
            run.font.size = Pt(font_size)

            rPr = run._element.get_or_add_rPr()
            rFonts = rPr.get_or_add_rFonts()
            rFonts.set(qn('w:cs'), font_name)
            rFonts.set(qn('w:ascii'), font_name)
            rFonts.set(qn('w:hAnsi'), font_name)
            rFonts.set(qn('w:eastAsia'), font_name)

            chars += len(part)
        return chars

    # ── Sanitization 1: Unpack fake borderless tables that trap narrative paragraphs or have empty padding cells ──
    APPROVAL_KEYWORDS = ('បានឃើញ', 'ប្រធាន', 'អគ្គលេខាធិការ', 'ហត្ថលេខា', 'ក្រឡាបញ្ជី', 'ចៅក្រម', 'អ្នកស្នើសុំ', 'សាមីខ្លួន', 'សាលាដំបូង', 'ក្រសួង', 'ព្រះរាជាណាចក្រ', 'តាងនាម')

    sanitized_items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get('type') == 'table' and it.get('layout') == 'borderless':
            rows = it.get('rows', [])
            all_cells = [c for r in rows for c in r]
            non_empty_cells = [c for c in all_cells if (c.get('text', '') if isinstance(c, dict) else str(c)).strip()]
            
            # Check if this table is actually trapping regular body paragraphs
            is_narrative_table = False
            for c in non_empty_cells:
                c_txt = c.get('text', '') if isinstance(c, dict) else str(c)
                if len(c_txt) > 50 and not any(k in c_txt for k in APPROVAL_KEYWORDS):
                    if any(c_txt.startswith(p) for p in ('អាស្រ័យ', 'តាមកម្មវត្ថុ', 'ខ្ញុំបាទ', 'សូម', 'ដោយហេតុ', 'យោង')):
                        is_narrative_table = True
                        break

            if is_narrative_table or len(non_empty_cells) == 1:
                # Unpack table cells into sequential paragraphs!
                for c in non_empty_cells:
                    c_txt = c.get('text', '') if isinstance(c, dict) else str(c)
                    c_align = c.get('align', 'justify') if isinstance(c, dict) else 'justify'
                    if c_txt.strip():
                        sanitized_items.append({
                            'type': 'paragraph',
                            'align': c_align,
                            'text': c_txt.strip()
                        })
                continue

            # Check if a top letterhead cell trapped the document title (e.g. \n\nលិខិតអញ្ជើញមេធាវី)
            TITLE_SPLIT_RE = re.compile(r'\n+(លិខិតអញ្ជើញ[^\n]*|សេចក្តីសម្រេច[^\n]*|សាលក្រម[^\n]*|សាលដីកា[^\n]*|ដីកា[^\n]*|ប្រកាស[^\n]*|អនុក្រឹត្យ[^\n]*|ព្រះរាជក្រម[^\n]*|កិច្ចសន្យា[^\n]*|ពាក្យស្នើសុំ[^\n]*|DECISION[^\n]*|PRAKAS[^\n]*)', re.IGNORECASE)
            extracted_title = None
            for r in rows:
                for c in r:
                    c_txt = c.get('text', '') if isinstance(c, dict) else str(c)
                    if any(k in c_txt for k in ('ព្រះរាជាណាចក្រ', 'ជាតិ សាសនា')) and TITLE_SPLIT_RE.search(c_txt):
                        m = TITLE_SPLIT_RE.search(c_txt)
                        extracted_title = m.group(1).strip()
                        new_txt = TITLE_SPLIT_RE.sub('', c_txt).strip()
                        if isinstance(c, dict):
                            c['text'] = new_txt

            sanitized_items.append(it)
            if extracted_title:
                sanitized_items.append({
                    'type': 'heading',
                    'level': 1,
                    'align': 'center',
                    'text': extracted_title
                })
            continue

        sanitized_items.append(it)

    # ── Sanitization 2: Coalesce consecutive tail signature/approval paragraphs into a multi-column borderless table ──
    NARRATIVE_OPENERS = ('អាស្រ័យ', 'តាមកម្មវត្ថុ', 'ខ្ញុំបាទ', 'សូម', 'ដោយហេតុ', 'យោង', 'កម្មវត្ថុ')

    def _is_approval_item(it):
        if not isinstance(it, dict) or it.get('type') != 'paragraph':
            return False
        txt = it.get('text', '').strip()
        if any(txt.startswith(n) for n in NARRATIVE_OPENERS):
            return False
        is_approval = txt.startswith(('បានឃើញ', 'ហត្ថលេខា')) or ('\n' in txt and any(k in txt for k in ('ប្រធាន', 'អគ្គលេខាធិការ', 'ចៅក្រម', 'ក្រឡាបញ្ជី')))
        return is_approval and len(txt) < 160

    processed_items = []
    idx = 0
    total_items = len(sanitized_items)
    while idx < total_items:
        it = sanitized_items[idx]
        if _is_approval_item(it):
            group = [it]
            j = idx + 1
            while j < total_items and _is_approval_item(sanitized_items[j]):
                group.append(sanitized_items[j])
                j += 1
            if len(group) in (2, 3):
                # Build 1-row borderless table
                row_cells = []
                for g_it in group:
                    row_cells.append({
                        'text': g_it.get('text', ''),
                        'align': g_it.get('align', 'center'),
                        'bold': False
                    })
                processed_items.append({
                    'type': 'table',
                    'layout': 'borderless',
                    'rows': [row_cells]
                })
                idx = j
                continue
        processed_items.append(it)
        idx += 1

    total_chars = 0
    for item in processed_items:
        if not isinstance(item, dict):
            continue
        itype = item.get('type', 'paragraph').lower()
        align_str = item.get('align', '').lower()
        text = item.get('text', '')

        if itype == 'heading':
            level = item.get('level', 1)
            size_map = {1: 15.0, 2: 13.0, 3: 12.0}
            font_size = size_map.get(level, 13.0)
            align = ALIGN_MAP.get(align_str, WD_ALIGN_PARAGRAPH.CENTER)
            
            p = doc.add_paragraph()
            p.alignment = align
            p.paragraph_format.line_spacing = 1.25
            p.paragraph_format.space_before = Pt(6 if level == 1 else 4)
            p.paragraph_format.space_after = Pt(2)

            # Use Khmer OS Muol Light for major headings, decrees, articles, and chapters
            MUOL_KEYWORDS = (
                'ព្រះរាជាណាចក្រ', 'ជាតិ សាសនា', 'សាលក្រម', 'សេចក្តីសម្រេច', 'ដីកា', 'ប្រកាស',
                'អនុក្រឹត្យ', 'ព្រះរាជក្រម', 'សារាចរ', 'កិច្ចសន្យា', 'ពាក្យស្នើសុំ', 'សម្រេច',
                'បង្គាប់', 'មាត្រា', 'ជំពូក', 'គន្ថី', 'ផ្នែក', 'ចំណុច'
            )
            use_muol = level <= 2 or any(k in text for k in MUOL_KEYWORDS)
            heading_font = 'Khmer OS Muol Light' if use_muol else 'Khmer OS Battambang'
            total_chars += _add_styled_runs(p, text, is_bold=True, font_size=font_size, font_name_override=heading_font)

        elif itype == 'list_item':
            is_sub = item.get('sub', False) or bool(re.match(r'^(?:[0-9\u17E0-\u17E9]+\.[0-9\u17E0-\u17E9]+|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\))', text))
            align = ALIGN_MAP.get(align_str, WD_ALIGN_PARAGRAPH.JUSTIFY if len(text) > 80 else WD_ALIGN_PARAGRAPH.LEFT)

            p = doc.add_paragraph()
            p.alignment = align
            p.paragraph_format.line_spacing = 1.2
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(2)

            # Hanging indent creates clean bullet/number alignment without Word's duplicate marker bug
            if is_sub:
                p.paragraph_format.left_indent = Inches(0.55)
                p.paragraph_format.first_line_indent = Inches(-0.25)
            else:
                p.paragraph_format.left_indent = Inches(0.35)
                p.paragraph_format.first_line_indent = Inches(-0.25)

            total_chars += _add_styled_runs(p, text, is_bold=False, font_size=11)

        elif itype == 'table':
            rows_data = item.get('rows', [])
            if not rows_data:
                continue
            layout = item.get('layout', 'grid').lower()
            num_cols = max(len(r) for r in rows_data)
            tbl = doc.add_table(rows=len(rows_data), cols=num_cols)
            tbl.autofit = False
            tbl.alignment = WD_TABLE_ALIGNMENT.CENTER

            if layout == 'borderless':
                tblPr = tbl._element.xpath('w:tblPr')
                if tblPr:
                    val_none = 'none'
                    borders_xml = f'<w:tblBorders {nsdecls("w")}><w:top w:val="{val_none}"/><w:left w:val="{val_none}"/><w:bottom w:val="{val_none}"/><w:right w:val="{val_none}"/><w:insideH w:val="{val_none}"/><w:insideV w:val="{val_none}"/></w:tblBorders>'
                    tblPr[0].append(parse_xml(borders_xml))
            else:
                tbl.style = 'Table Grid'

            # Calculate balanced column widths for standard printable width (6.5 inches)
            first_row_str = ' '.join(c.get('text', '') if isinstance(c, dict) else str(c) for c in (rows_data[0] if rows_data else []))
            is_top_letterhead = any(k in first_row_str for k in ('សាលាដំបូង', 'ក្រសួង', 'ព្រះរាជាណាចក្រ', 'គណៈមេធាវី', 'តាងនាម'))
            is_bottom_signature = any(k in first_row_str for k in ('ចៅក្រម', 'ប្រធាន', 'មេធាវី', 'ក្រឡាបញ្ជី', '28/026', 'ហត្ថលេខា'))

            if layout == 'borderless' and num_cols == 3:
                if is_top_letterhead:
                    col_widths = [Inches(2.6), Inches(2.5), Inches(1.4)]
                elif is_bottom_signature:
                    col_widths = [Inches(2.2), Inches(1.5), Inches(2.8)]
                else:
                    col_widths = [Inches(2.16), Inches(2.16), Inches(2.16)]
            elif layout == 'borderless' and num_cols == 2:
                if is_top_letterhead:
                    col_widths = [Inches(2.85), Inches(3.65)]
                else:
                    col_widths = [Inches(3.25), Inches(3.25)]
            else:
                col_widths = [Inches(6.5 / num_cols)] * num_cols

            for row_idx, r in enumerate(rows_data):
                row_elem = tbl.rows[row_idx]
                for col_idx in range(num_cols):
                    cell = row_elem.cells[col_idx]
                    if col_idx < len(col_widths):
                        cell.width = col_widths[col_idx]
                    if col_idx >= len(r):
                        continue
                    cell_data = r[col_idx]
                    if isinstance(cell_data, dict):
                        c_text = cell_data.get('text', '')
                        c_align = ALIGN_MAP.get(cell_data.get('align', '').lower(), WD_ALIGN_PARAGRAPH.CENTER)
                        c_bold = cell_data.get('bold', False)
                    else:
                        c_text = str(cell_data)
                        c_align = WD_ALIGN_PARAGRAPH.CENTER
                        c_bold = False

                    lines = [l.strip() for l in re.split(r'\n|<br\s*/?>', c_text) if l.strip()]
                    if not lines:
                        lines = ['']
                    for l_i, l_str in enumerate(lines):
                        p = cell.paragraphs[0] if l_i == 0 else cell.add_paragraph()
                        p.alignment = c_align
                        p.paragraph_format.line_spacing = 1.15
                        p.paragraph_format.space_before = Pt(0)
                        p.paragraph_format.space_after = Pt(2)
                        total_chars += _add_styled_runs(p, l_str, is_bold=c_bold, font_size=10.5)

            sp = doc.add_paragraph()
            sp.paragraph_format.space_before = Pt(0)
            sp.paragraph_format.space_after = Pt(2)

        else:  # paragraph
            # Unpack multiline bullet lists (e.g. យោង ៖ \n- ... or ចម្លងជូន ៖ \n- ...)
            if '\n-' in text or '\n•' in text or '\n–' in text:
                sub_lines = [l.strip() for l in text.split('\n') if l.strip()]
                for sl in sub_lines:
                    p = doc.add_paragraph()
                    p.paragraph_format.line_spacing = 1.2
                    p.paragraph_format.space_before = Pt(1)
                    p.paragraph_format.space_after = Pt(2)
                    if sl.startswith(('-', '–', '—', '•', '*', '+')) and not sl.startswith(('--', '---')):
                        if len(sl) > 1 and sl[1] != ' ':
                            sl = sl[0] + ' ' + sl[1:]
                        p.paragraph_format.left_indent = Inches(0.35)
                        p.paragraph_format.first_line_indent = Inches(-0.25)
                        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY if len(sl) > 70 else WD_ALIGN_PARAGRAPH.LEFT
                    elif sl.startswith(('កម្មវត្ថុ', 'យោង', 'ចម្លងជូន')):
                        p.paragraph_format.left_indent = Inches(0.35)
                        p.paragraph_format.first_line_indent = Inches(-0.35)
                        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    else:
                        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    total_chars += _add_styled_runs(p, sl, is_bold=False, font_size=11)
                continue

            # Check if this paragraph is a bullet point or numbered item (with OR WITHOUT space after dash!)
            is_bullet = False
            if text.startswith(('-', '–', '—', '•', '*', '+')) and not text.startswith(('--', '---')):
                is_bullet = True
                if len(text) > 1 and text[1] != ' ':
                    text = text[0] + ' ' + text[1:]
            elif re.match(r'^(?:[0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+[\.\)]|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\))\s*', text):
                is_bullet = True

            p = doc.add_paragraph()
            p.paragraph_format.line_spacing = 1.25
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.space_after = Pt(2)

            if is_bullet:
                p.paragraph_format.left_indent = Inches(0.35)
                p.paragraph_format.first_line_indent = Inches(-0.25)
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY if len(text) > 70 else WD_ALIGN_PARAGRAPH.LEFT
            else:
                align = ALIGN_MAP.get(align_str, WD_ALIGN_PARAGRAPH.JUSTIFY if len(text) > 65 else WD_ALIGN_PARAGRAPH.LEFT)
                p.alignment = align

                # Hanging indent for legal labels (e.g. "**កម្មវត្ថុ ៖**" or "**យោង ៖**")
                clean_prefix = re.sub(r'[\*\s]', '', text[:15])
                if clean_prefix.startswith(('កម្មវត្ថុ៖', 'កម្មវត្ថុ:', 'យោង៖', 'យោង:', 'ចម្លងជូន៖', 'ចម្លងជូន:')):
                    p.paragraph_format.left_indent = Inches(0.35)
                    p.paragraph_format.first_line_indent = Inches(-0.35)
                # First line indent for narrative body paragraphs
                elif align == WD_ALIGN_PARAGRAPH.JUSTIFY and len(text) > 70:
                    p.paragraph_format.first_line_indent = Inches(0.4)
                elif text.startswith(('ខ្ញុំបាទ', 'ខ្ញុំនាងខ្ញុំ', 'ខ្ញុំព្រះករុណា', 'តាមកម្មវត្ថុ', 'អាស្រ័យ')):
                    p.paragraph_format.first_line_indent = Inches(0.4)

            total_chars += _add_styled_runs(p, text, is_bold=False, font_size=11)

    return total_chars


def _add_structured_content_to_docx(doc, raw_text):
    """
    Parses Claude's or Gemini's output. If raw_text contains a valid or truncated
    JSON array of structural elements, formats each element with exact alignment,
    styling, and geometry.
    If raw_text is genuine markdown, uses the markdown parser.
    """
    import json
    import logging
    logger = logging.getLogger(__name__)

    clean = raw_text.strip()
    if clean.startswith('```'):
        first_nl = clean.find('\n')
        if first_nl != -1:
            clean = clean[first_nl + 1:]
        if clean.endswith('```'):
            clean = clean[:-3].strip()

    start_idx = clean.find('[')
    if start_idx != -1:
        candidate = clean[start_idx:].strip()

        # Attempt 1: Parse candidate directly with strict=False (allows unescaped newlines in strings)
        try:
            items = json.loads(candidate, strict=False)
            if isinstance(items, list) and len(items) > 0:
                logger.info(f'OCR: Successfully parsed {len(items)} structured JSON elements')
                return _add_structured_json_to_docx(doc, items)
        except Exception as e:
            logger.debug(f'OCR: Direct JSON parse failed: {e}')

        # Attempt 2: Truncation recovery if output was cut off mid-JSON
        last_brace = candidate.rfind('}')
        if last_brace != -1:
            repaired = candidate[:last_brace + 1].rstrip().rstrip(',') + ']'
            try:
                items = json.loads(repaired, strict=False)
                if isinstance(items, list) and len(items) > 0:
                    logger.info(f'OCR: Recovered {len(items)} structured JSON elements from truncated output')
                    return _add_structured_json_to_docx(doc, items)
            except Exception as e:
                logger.warning(f'OCR: Repaired JSON parse failed: {e}')

        # Attempt 3: If candidate looks like JSON code (starts with '[{' or '{\n"type"'),
        # NEVER pass raw JSON code to _add_markdown_to_docx (which dumps JSON braces into Word)!
        # Instead, extract text blocks cleanly.
        if candidate.startswith(('[', '{')) and any(k in candidate for k in ('"type":', '"text":', '"rows":')):
            import re
            extracted_texts = re.findall(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate)
            if extracted_texts:
                logger.info(f'OCR: Salvaged {len(extracted_texts)} text blocks from malformed JSON')
                salvaged_chars = 0
                for txt in extracted_texts:
                    clean_t = txt.replace('\\n', '\n').replace('\\"', '"').strip()
                    if clean_t:
                        p = doc.add_paragraph()
                        salvaged_chars += len(clean_t)
                        run = p.add_run(clean_t)
                        run.font.name = 'Khmer OS Battambang'
                if salvaged_chars > 0:
                    return salvaged_chars

    # Fallback to markdown parser only if raw_text is genuine markdown
    return _add_markdown_to_docx(doc, raw_text)


def _add_markdown_to_docx(doc, markdown_text):
    """
    Parse Claude's formatted OCR output (Markdown headers, centered titles,
    bold text, bullet/numbered lists, and tables) into native styled Word
    elements with proper fonts (Khmer OS Battambang for Khmer, Calibri for Latin).
    Returns total extracted character count.
    """
    import re
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls, qn

    def _add_styled_runs(paragraph, text, is_bold=False, font_size=11, font_name_override=None):
        """Parse inline **bold** markers and append runs with explicit Khmer/Latin font switching."""
        parts = text.split('**')
        chars = 0
        for idx, part in enumerate(parts):
            if not part:
                continue
            run_bold = is_bold or (idx % 2 == 1)
            # Detect Khmer characters
            has_khmer = any('\u1780' <= c <= '\u17FF' or '\u19E0' <= c <= '\u19FF' for c in part)
            if has_khmer:
                font_name = font_name_override if font_name_override else 'Khmer OS Battambang'
            else:
                font_name = 'Calibri'

            run = paragraph.add_run(part)
            run.bold = run_bold
            run.font.name = font_name
            run.font.size = Pt(font_size)

            # Explicitly set Word's Complex Script (w:cs) and East Asia fonts
            # so Windows Word renders Khmer characters using Khmer OS Battambang
            rPr = run._element.get_or_add_rPr()
            rFonts = rPr.get_or_add_rFonts()
            rFonts.set(qn('w:cs'), font_name)
            rFonts.set(qn('w:ascii'), font_name)
            rFonts.set(qn('w:hAnsi'), font_name)
            rFonts.set(qn('w:eastAsia'), font_name)

            chars += len(part)
        return chars

    # Clean markdown code fences if Claude wrapped output in ```markdown ... ```
    text = markdown_text.strip()
    if text.startswith('```'):
        first_newline = text.find('\n')
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith('```'):
            text = text[:-3].strip()

    lines = text.split('\n')
    i = 0
    n = len(lines)
    total_chars = 0

    while i < n:
        raw_line = lines[i]
        line = raw_line.strip()

        if not line:
            i += 1
            continue

        # ── 1. Check for Markdown Table ──────────────────────────
        if line.startswith('|') and line.endswith('|'):
            table_lines = []
            while i < n and lines[i].strip().startswith('|') and lines[i].strip().endswith('|'):
                table_lines.append(lines[i].strip())
                i += 1

            is_layout_table = any('[LAYOUT]' in tl or '[layout]' in tl for tl in table_lines)

            valid_rows = []
            for r_i, t_line in enumerate(table_lines):
                # If this is a [LAYOUT] table, skip the header column-definition row
                if is_layout_table and r_i == 0:
                    continue
                clean_tline = re.sub(r'\[LAYOUT\]|\[layout\]', '', t_line)
                cells = [c.strip() for c in clean_tline.split('|')[1:-1]]
                if all(set(c).issubset({'-', ':', ' '}) for c in cells if c):
                    continue  # separator row
                valid_rows.append(cells)

            if valid_rows:
                num_cols = max(len(r) for r in valid_rows)
                normalized_rows = [r + [''] * (num_cols - len(r)) for r in valid_rows]
                tbl = doc.add_table(rows=len(normalized_rows), cols=num_cols)
                tbl.autofit = False
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER

                has_header_keywords = any(
                    any(k in cell for k in ('ព្រះរាជាណាចក្រ', 'សាលាដំបូង', 'ក្រឡាបញ្ជី', 'ចៅក្រម', 'តាងនាមប្រជារាស្ត្រខ្មែរ'))
                    for row in normalized_rows for cell in row
                )

                if is_layout_table or has_header_keywords:
                    # Apply borderless XML to ensure completely invisible boundaries
                    tblPr = tbl._element.xpath('w:tblPr')
                    if tblPr:
                        val_none = 'none'
                        borders_xml = f'<w:tblBorders {nsdecls("w")}><w:top w:val="{val_none}"/><w:left w:val="{val_none}"/><w:bottom w:val="{val_none}"/><w:right w:val="{val_none}"/><w:insideH w:val="{val_none}"/><w:insideV w:val="{val_none}"/></w:tblBorders>'
                        tblPr[0].append(parse_xml(borders_xml))
                else:
                    tbl.style = 'Table Grid'

                # Explicit column widths for standard printable width (~6.5 inches)
                if is_layout_table or has_header_keywords:
                    if num_cols == 2:
                        col_widths = [Inches(2.85), Inches(3.65)]
                    elif num_cols == 3:
                        col_widths = [Inches(2.16), Inches(2.16), Inches(2.16)]
                    else:
                        col_widths = [Inches(6.5 / num_cols)] * num_cols
                else:
                    col_widths = [Inches(6.5 / num_cols)] * num_cols

                for row_idx, row_cells in enumerate(normalized_rows):
                    is_header = (row_idx == 0 and not is_layout_table and not has_header_keywords)
                    row_elem = tbl.rows[row_idx]
                    for col_idx, cell_text in enumerate(row_cells):
                        cell = row_elem.cells[col_idx]
                        if col_idx < len(col_widths):
                            cell.width = col_widths[col_idx]

                        cell_lines = [cl.strip() for cl in re.split(r'<br\s*/?>', cell_text, flags=re.I) if cl.strip()]
                        if not cell_lines:
                            cell_lines = ['']

                        active_cell_align = None
                        for l_idx, cl in enumerate(cell_lines):
                            if l_idx == 0:
                                p = cell.paragraphs[0]
                            else:
                                p = cell.add_paragraph()

                            if '[CENTER]' in cl or '[center]' in cl:
                                active_cell_align = WD_ALIGN_PARAGRAPH.CENTER
                            elif '[RIGHT]' in cl or '[right]' in cl:
                                active_cell_align = WD_ALIGN_PARAGRAPH.RIGHT
                            elif '[JUSTIFY]' in cl or '[justify]' in cl:
                                active_cell_align = WD_ALIGN_PARAGRAPH.JUSTIFY
                            elif '[LEFT]' in cl or '[left]' in cl:
                                active_cell_align = WD_ALIGN_PARAGRAPH.LEFT

                            if active_cell_align:
                                p_align = active_cell_align
                            elif any(k in cl for k in ('ព្រះរាជាណាចក្រ', 'ជាតិ សាសនា', 'តាងនាមប្រជារាស្ត្រខ្មែរ', 'ក្រឡាបញ្ជី', 'ចៅក្រម', 'ប្រធាន', 'អនុប្រធាន')):
                                p_align = WD_ALIGN_PARAGRAPH.CENTER
                            elif len(cl) > 80:
                                p_align = WD_ALIGN_PARAGRAPH.JUSTIFY
                            else:
                                p_align = WD_ALIGN_PARAGRAPH.LEFT

                            if any(k in cl for k in ('[/CENTER]', '[/center]', '[/RIGHT]', '[/right]', '[/JUSTIFY]', '[/justify]', '[/LEFT]', '[/left]')):
                                active_cell_align = None

                            cl = re.sub(r'\[/?CENTER\]|\[/?center\]|\[/?RIGHT\]|\[/?right\]|\[/?JUSTIFY\]|\[/?justify\]|\[/?LEFT\]|\[/?left\]', '', cl).strip()

                            p.alignment = p_align
                            p.paragraph_format.line_spacing = 1.15
                            p.paragraph_format.space_before = Pt(0)
                            p.paragraph_format.space_after = Pt(2)

                            c_count = _add_styled_runs(p, cl, is_bold=is_header, font_size=10.5 if not is_header else 11)
                            total_chars += c_count

                # Subtle spacing paragraph after table
                sp = doc.add_paragraph()
                sp.paragraph_format.space_before = Pt(0)
                sp.paragraph_format.space_after = Pt(2)
            continue

        # ── 2. Omit standalone page numbers ──────────────────────
        # Standalone '1', '2', '១', '២', '៨' at page margins
        clean_num_check = re.sub(r'\[/?CENTER\]|\[/?RIGHT\]|\[/?center\]|\[/?right\]', '', line).strip()
        if re.match(r'^[0-9\u17E0-\u17E9]{1,3}$', clean_num_check):
            i += 1
            continue

        # ── 3. Alignment & Heading tags ──────────────────────────
        explicit_align = None
        if re.search(r'\[/?CENTER\]|\[/?center\]', line):
            explicit_align = WD_ALIGN_PARAGRAPH.CENTER
            line = re.sub(r'\[/?CENTER\]|\[/?center\]', '', line).strip()
        elif re.search(r'\[/?RIGHT\]|\[/?right\]', line):
            explicit_align = WD_ALIGN_PARAGRAPH.RIGHT
            line = re.sub(r'\[/?RIGHT\]|\[/?right\]', '', line).strip()
        elif re.search(r'\[/?JUSTIFY\]|\[/?justify\]', line):
            explicit_align = WD_ALIGN_PARAGRAPH.JUSTIFY
            line = re.sub(r'\[/?JUSTIFY\]|\[/?justify\]', '', line).strip()
        elif re.search(r'\[/?LEFT\]|\[/?left\]', line):
            explicit_align = WD_ALIGN_PARAGRAPH.LEFT
            line = re.sub(r'\[/?LEFT\]|\[/?left\]', '', line).strip()

        font_size = 11.0
        is_heading_bold = False
        space_before = Pt(0)
        space_after = Pt(3)
        is_heading = False

        # Check Heading levels
        if line.startswith('# '):
            is_heading = True
            font_size = 15.0
            is_heading_bold = True
            space_before = Pt(6)
            space_after = Pt(2)
            line = line[2:].strip()
        elif line.startswith('## '):
            is_heading = True
            font_size = 13.0
            is_heading_bold = True
            space_before = Pt(5)
            space_after = Pt(2)
            line = line[3:].strip()
        elif line.startswith('### '):
            is_heading = True
            font_size = 12.0
            is_heading_bold = True
            space_before = Pt(4)
            space_after = Pt(2)
            line = line[4:].strip()

        # Lists & Sub-clause indentations
        is_bullet = False
        is_number = False
        is_sub = False
        left_indent = None

        bullet_match = re.match(r'^([\u2022\u25CB\u25CF\u25AA\u25AB\u2013\u2014\-\*\+\~])\s+(.+)$', line)
        number_match = re.match(r'^([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+[\.\)]|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\))\s+(.+)$', line)
        sub_match = re.match(r'^([0-9\u17E0-\u17E9]+\.[0-9\u17E0-\u17E9]+[\.\)]?)\s+(.+)$', line)

        if sub_match:
            is_sub = True
            is_number = True
        elif number_match:
            is_number = True
        elif bullet_match:
            is_bullet = True
            sym = bullet_match.group(1)
            rest = bullet_match.group(2)
            if sym not in ('-', '–', '—'):
                line = '• ' + rest

        # ── Intelligent Alignment Resolution ──
        # Check if text is center -> make it center; if paragraph/narrative -> make it justify
        if explicit_align is not None:
            align = explicit_align
        elif any(k in line for k in ('ព្រះរាជាណាចក្រកម្ពុជា', 'ជាតិ សាសនា ព្រះមហាក្សត្រ', 'Kingdom of Cambodia', 'Nation Religion King')):
            align = WD_ALIGN_PARAGRAPH.CENTER
        elif is_heading:
            if any(k in line for k in ('សាលក្រម', 'សេចក្តីសម្រេច', 'ដីកា', 'ប្រកាស', 'កិច្ចសន្យា', 'តាងនាមប្រជារាស្ត្រខ្មែរ', 'ព្រះរាជក្រម', 'លិខិត', 'ពាក្យស្នើសុំ', 'NOTICE', 'CERTIFICATE', 'DECISION', 'JUDGMENT', 'CONTRACT', 'ORDER')) or len(line) < 55:
                align = WD_ALIGN_PARAGRAPH.CENTER
            else:
                align = WD_ALIGN_PARAGRAPH.LEFT
        elif re.match(r'^(រាជធានីភ្នំពេញ|ភ្នំពេញ|ធ្វើនៅ|ថ្ងៃទី)\s*,?\s*ថ្ងៃទី', line) or (re.search(r'ថ្ងៃទី\s*[0-9\u17E0-\u17E9]+\s*ខែ\s*[0-9\u17E0-\u17E9\w]+\s*ឆ្នាំ\s*[0-9\u17E0-\u17E9]+', line) and len(line) < 70):
            align = WD_ALIGN_PARAGRAPH.RIGHT
        elif is_bullet or is_number:
            align = WD_ALIGN_PARAGRAPH.JUSTIFY if len(line) > 85 else WD_ALIGN_PARAGRAPH.LEFT
        elif len(line) < 65 or line.endswith(('៖', ':')) or re.match(r'^\*\*[^\*]+(៖|:)\*\*', line):
            align = WD_ALIGN_PARAGRAPH.LEFT
        elif len(line) >= 65:
            align = WD_ALIGN_PARAGRAPH.JUSTIFY
        else:
            align = WD_ALIGN_PARAGRAPH.LEFT

        p = doc.add_paragraph()
        p.alignment = align
        p.paragraph_format.line_spacing = 1.25
        p.paragraph_format.space_before = space_before
        p.paragraph_format.space_after = space_after
        if is_sub:
            p.paragraph_format.left_indent = Inches(0.55)
            p.paragraph_format.first_line_indent = Inches(-0.25)
        elif is_bullet or is_number:
            p.paragraph_format.left_indent = Inches(0.35)
            p.paragraph_format.first_line_indent = Inches(-0.25)
        else:
            clean_prefix = re.sub(r'[\*\s]', '', line[:15])
            if clean_prefix.startswith(('កម្មវត្ថុ៖', 'កម្មវត្ថុ:', 'យោង៖', 'យោង:')):
                p.paragraph_format.left_indent = Inches(0.35)
                p.paragraph_format.first_line_indent = Inches(-0.35)
            elif align == WD_ALIGN_PARAGRAPH.JUSTIFY and len(line) > 70:
                p.paragraph_format.first_line_indent = Inches(0.4)
            elif line.startswith(('ខ្ញុំបាទ', 'ខ្ញុំនាងខ្ញុំ', 'ខ្ញុំព្រះករុណា', 'តាមកម្មវត្ថុ', 'អាស្រ័យ')):
                p.paragraph_format.first_line_indent = Inches(0.4)

        MUOL_KEYWORDS = (
            'ព្រះរាជាណាចក្រ', 'ជាតិ សាសនា', 'សាលក្រម', 'សេចក្តីសម្រេច', 'ដីកា', 'ប្រកាស',
            'អនុក្រឹត្យ', 'ព្រះរាជក្រម', 'កិច្ចសន្យា', 'សម្រេច', 'បង្គាប់', 'មាត្រា', 'ជំពូក'
        )
        use_muol = is_heading and (font_size >= 13.0 or any(k in line for k in MUOL_KEYWORDS))
        heading_font = 'Khmer OS Muol Light' if use_muol else 'Khmer OS Battambang'

        c_count = _add_styled_runs(p, line, is_bold=is_heading_bold, font_size=font_size, font_name_override=heading_font)
        total_chars += c_count
        i += 1

    return total_chars


def _vision_ocr_page(client, pil_image, lang_hints=None):
    """Run Google Vision DOCUMENT_TEXT_DETECTION on one page image, return
    the extracted text (same shape as pytesseract.image_to_string output)."""
    from google.cloud import vision

    buf = io.BytesIO()
    pil_image.convert('RGB').save(buf, format='PNG')
    image = vision.Image(content=buf.getvalue())
    image_context = vision.ImageContext(language_hints=lang_hints) if lang_hints else None

    response = client.document_text_detection(image=image, image_context=image_context)
    if response.error and response.error.message:
        raise RuntimeError(response.error.message)
    return response.full_text_annotation.text


def _khmerocr_page(doc, pil_image, ocr_lang='khm'):
    """
    Run KhmerOCR ONNX deep learning model on a single page PIL image.
    Trained on 3 million Khmer text lines over 800+ fonts.
    Recognizes text lines, identifies Moul vs Regular fonts, preserves layout,
    and appends native styled Word runs with 'Khmer OS Muol Light' for Moul
    and 'Khmer OS Battambang' for regular text.
    Returns: total_chars_extracted (int)
    """
    import re
    import khmerocr
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn

    # Convert image to RGB mode for ONNX inference
    if pil_image.mode != 'RGB':
        pil_image = pil_image.convert('RGB')

    lines = khmerocr.detect(pil_image)
    if not lines:
        return 0

    total_chars = 0
    DET_THRESHOLD = 0.35
    w, h = pil_image.size

    for line in lines:
        line_parts = []
        is_line_moul = False
        line_has_text = False

        for res in line:
            box, cls_id, conf = res[:4], int(res[4]), float(res[5])
            if conf < DET_THRESHOLD:
                continue

            # cls_id == 1 indicates a text region
            if cls_id == 1:
                x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
                # Bounds check
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(w, x2), min(h, y2)
                if x2 <= x1 + 2 or y2 <= y1 + 2:
                    continue

                cropped = pil_image.crop((x1, y1, x2, y2))
                rec = khmerocr.recognize(cropped)
                text = (rec.get("text") or "").strip()
                if not text:
                    continue

                font_style = rec.get("font", "Regular")
                font_conf = float(rec.get("font_confidence", 0.0))
                if font_style in ("Moul", "MoulLight") and font_conf >= 0.65:
                    is_line_moul = True

                line_parts.append(text)
                line_has_text = True

        if not line_has_text or not line_parts:
            continue

        full_line = " ".join(line_parts).strip()
        if not full_line:
            continue

        has_khmer = any('\u1780' <= c <= '\u17FF' or '\u19E0' <= c <= '\u19FF' for c in full_line)
        p = doc.add_paragraph()

        if is_line_moul:
            font_name = 'Khmer OS Muol Light' if has_khmer else 'Calibri'
            font_size = 13.0
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.2
            is_bold = True
        else:
            font_name = 'Khmer OS Battambang' if has_khmer else 'Calibri'
            font_size = 11.0
            is_bold = False

            # Smart paragraph alignment heuristics
            if any(k in full_line for k in ('ព្រះរាជាណាចក្រកម្ពុជា', 'ជាតិ សាសនា ព្រះមហាក្សត្រ', 'Kingdom of Cambodia')):
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif re.search(r'ថ្ងៃទី\s*[0-9\u17E0-\u17E9]+\s*ខែ\s*[0-9\u17E0-\u17E9\w]+\s*ឆ្នាំ\s*[0-9\u17E0-\u17E9]+', full_line) and len(full_line) < 70:
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            elif len(full_line) > 75:
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            else:
                p.alignment = WD_ALIGN_PARAGRAPH.LEFT

            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.15

        run = p.add_run(full_line)
        run.bold = is_bold
        run.font.name = font_name
        run.font.size = Pt(font_size)

        if has_khmer:
            rPr = run._element.get_or_add_rPr()
            rFonts = rPr.get_or_add_rFonts()
            rFonts.set(qn('w:cs'), font_name)
            rFonts.set(qn('w:ascii'), font_name)
            rFonts.set(qn('w:hAnsi'), font_name)
            rFonts.set(qn('w:eastAsia'), font_name)

        total_chars += len(full_line)

    return total_chars


def _tesseract_ocr_page_structured(doc, gray_image, tess_lang='eng'):
    """
    Spatial layout and table reconstruction engine for Tesseract OCR.
    Extracts word bounding boxes, groups words into lines and horizontal column cells,
    detects multi-column grid tables, side-by-side letterheads, centered headings,
    and right-aligned dates, then formats native Microsoft Word elements with
    Khmer OS Battambang and OpenXML Complex Script tags.
    Returns: total_chars_extracted (int)
    """
    import re
    import pytesseract
    import logging
    logger = logging.getLogger(__name__)

    w_img, h_img = gray_image.size
    data = None
    try:
        data = pytesseract.image_to_data(gray_image, lang=tess_lang, output_type=pytesseract.Output.DICT)
    except Exception as e:
        logger.warning(f'_tesseract_ocr_page_structured: image_to_data failed: {e}', exc_info=True)
        data = None

    elements = []
    if data and len(data.get('text', [])) > 0:
        n = len(data['text'])
        words = []
        for i in range(n):
            txt = str(data['text'][i]).strip()
            if not txt:
                continue
            try:
                conf = int(data['conf'][i])
            except (ValueError, TypeError):
                conf = 50
            if conf < 25:
                continue

            left = int(data['left'][i])
            top = int(data['top'][i])
            w = int(data['width'][i])
            h = int(data['height'][i])
            words.append({
                'text': txt,
                'left': left,
                'top': top,
                'right': left + w,
                'bottom': top + h,
                'width': w,
                'height': h,
                'cy': top + h / 2.0
            })

        if words:
            # Sort words vertically by center y
            words.sort(key=lambda w: w['cy'])

            # Cluster words into visual text lines
            lines = []
            curr_line = [words[0]]
            for w in words[1:]:
                prev_w = curr_line[-1]
                line_h = max(cw['height'] for cw in curr_line)
                v_diff = abs(w['cy'] - prev_w['cy'])
                if v_diff < max(8, line_h * 0.45):
                    curr_line.append(w)
                else:
                    lines.append(curr_line)
                    curr_line = [w]
            if curr_line:
                lines.append(curr_line)

            # Median word height across the page
            all_heights = [w['height'] for w in words]
            med_height = sorted(all_heights)[len(all_heights) // 2] if all_heights else 16

            # In each line, detect horizontal column gaps
            processed_lines = []
            for line in lines:
                line.sort(key=lambda w: w['left'])
                cells = []
                curr_cell_words = [line[0]]
                for w in line[1:]:
                    prev_w = curr_cell_words[-1]
                    gap = w['left'] - prev_w['right']

                    # Check if curr_cell_words is merely a bullet or list marker prefix
                    curr_prefix = " ".join(cw['text'] for cw in curr_cell_words).strip()
                    is_prefix_marker = bool(re.match(
                        r'^(?:[\u2022\u25CB\u25CF\u25AA\u25AB\u2013\u2014\-\*\+\~]|'
                        r'[0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]{1,2}[\.\)]|'
                        r'\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]{1,2}\))$',
                        curr_prefix
                    ))

                    # High gap threshold to separate true table columns (> 75px, ~3.5x line height)
                    cell_split_threshold = max(75, int(med_height * 3.5), int(w_img * 0.08))
                    if gap > cell_split_threshold and not is_prefix_marker:
                        cell_text = curr_prefix
                        if cell_text:
                            cells.append({
                                'text': cell_text,
                                'x1': curr_cell_words[0]['left'],
                                'x2': curr_cell_words[-1]['right'],
                                'y': min(cw['top'] for cw in curr_cell_words),
                                'h': max(cw['height'] for cw in curr_cell_words),
                            })
                        curr_cell_words = [w]
                    else:
                        curr_cell_words.append(w)

                if curr_cell_words:
                    cell_text = " ".join(cw['text'] for cw in curr_cell_words).strip()
                    if cell_text:
                        cells.append({
                            'text': cell_text,
                            'x1': curr_cell_words[0]['left'],
                            'x2': curr_cell_words[-1]['right'],
                            'y': min(cw['top'] for cw in curr_cell_words),
                            'h': max(cw['height'] for cw in curr_cell_words),
                        })

                if cells:
                    processed_lines.append(cells)

            # Group multi-line tables vs letterheads vs single paragraphs
            idx = 0
            num_lines = len(processed_lines)
            while idx < num_lines:
                line_cells = processed_lines[idx]

                # Multi-column line (2+ columns)
                if len(line_cells) >= 2:
                    table_rows = [line_cells]
                    j = idx + 1
                    while j < num_lines and len(processed_lines[j]) == len(line_cells):
                        match = True
                        for c_idx in range(len(line_cells)):
                            diff = abs(processed_lines[j][c_idx]['x1'] - line_cells[c_idx]['x1'])
                            if diff > max(60, int(w_img * 0.08)):
                                match = False
                                break
                        if match:
                            table_rows.append(processed_lines[j])
                            j += 1
                        else:
                            break

                    # 2+ matching rows -> Table Grid
                    if len(table_rows) >= 2:
                        elements.append({
                            'type': 'table',
                            'layout': 'grid',
                            'rows': [[c['text'] for c in r] for r in table_rows]
                        })
                        idx = j
                        continue

                    # Single multi-column row (Letterhead or Signature row) -> Borderless Table
                    elements.append({
                        'type': 'table',
                        'layout': 'borderless',
                        'rows': [[c['text'] for c in line_cells]]
                    })
                    idx += 1
                    continue

                # Single cell line
                cell = line_cells[0]
                text = cell['text'].strip()
                if not text:
                    idx += 1
                    continue

                x1, x2, h = cell['x1'], cell['x2'], cell['h']
                cx = (x1 + x2) / 2.0
                line_w = x2 - x1

                page_center = w_img / 2.0
                is_centered = abs(cx - page_center) < (w_img * 0.12) and (line_w < w_img * 0.70)
                is_right = (x2 > w_img * 0.82) and (x1 > w_img * 0.42) and (line_w < w_img * 0.55)
                is_wide = line_w > w_img * 0.68

                # ── 1. Check for List Items (Bullets, Numbers, Legal decree dashes) ──
                # Matches: •, -, *, 1., 1), (1), ១., ១), (១), ក., ក), 1.1., ១.១., - បានឃើញ
                LIST_MARKER_RE = re.compile(
                    r'^(?:'
                    r'[\u2022\u25CB\u25CF\u25AA\u25AB\u2013\u2014\-\*\+\~]\s*'     # Bullets
                    r'|[0-9\u17E0-\u17E9]+(?:\.[0-9\u17E0-\u17E9]+)+[\.\)]?\s+'   # Sub-clauses (1.1, ១.១)
                    r'|(?:[0-9\u17E0-\u17E9]+|[a-zA-Z\u1780-\u17B3])[\.\)]\s+'    # 1. or 1) or ក. or a.
                    r'|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\)\s+'             # (1) or (ក)
                    r')'
                )

                TOP_TITLE_KEYWORDS = (
                    'ព្រះរាជាណាចក្រកម្ពុជា', 'ជាតិ សាសនា ព្រះមហាក្សត្រ',
                    'KINGDOM OF CAMBODIA', 'NATION RELIGION KING', 'រាជរដ្ឋាភិបាល'
                )
                DECREE_TITLE_KEYWORDS = (
                    'សេចក្តីសម្រេច', 'សាលក្រម', 'សាលដីកា', 'ដីកាសម្រេច', 'ដីកា', 'ប្រកាស',
                    'អនុក្រឹត្យ', 'ព្រះរាជក្រម', 'សារាចរ', 'សេចក្តីជូនដំណឹង', 'សេចក្តីណែនាំ',
                    'សេចក្តីថ្លែងការណ៍', 'លិខិត', 'ពាក្យស្នើសុំ', 'ពាក្យបណ្តឹង', 'កំណត់ហេតុ',
                    'កិច្ចសន្យា', 'កិច្ចព្រមព្រៀង', 'លក្ខន្តិកៈ', 'សេចក្តីព្រាង', 'តាងនាមប្រជារាស្ត្រខ្មែរ',
                    'សម្រេច', 'បង្គាប់', 'DECISION', 'JUDGMENT', 'PRAKAS', 'SUB-DECREE',
                    'CONTRACT', 'AGREEMENT', 'CERTIFICATE', 'NOTICE', 'ORDER'
                )
                SECTION_TITLE_RE = re.compile(
                    r'^(?:មាត្រា\s*[0-9\u17E0-\u17E9]+|ជំពូក(?:\s*ទី)?\s*[0-9\u17E0-\u17E9IVXLCDM]+|'
                    r'គន្ថី\s*[0-9\u17E0-\u17E9IVXLCDM]+|ផ្នែក(?:\s*ទី)?\s*[0-9\u17E0-\u17E9]+|'
                    r'កថាភាគ\s*[0-9\u17E0-\u17E9]+|ចំណុច(?:\s*ទី)?\s*[0-9\u17E0-\u17E9]+|'
                    r'Article\s*[0-9]+|Chapter\s*[0-9IVXLCDM]+|Section\s*[0-9]+)',
                    re.IGNORECASE
                )

                if LIST_MARKER_RE.match(text):
                    is_sub = bool(re.match(r'^(?:[0-9\u17E0-\u17E9]+\.[0-9\u17E0-\u17E9]+|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\))', text))
                    is_num = bool(re.match(r'^(?:[0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+[\.\)]|\([0-9\u17E0-\u17E9a-zA-Z\u1780-\u17B3]+\))', text))
                    elements.append({
                        'type': 'list_item',
                        'list_type': 'number' if is_num else 'bullet',
                        'sub': is_sub,
                        'text': text
                    })
                elif any(k in text for k in TOP_TITLE_KEYWORDS):
                    elements.append({
                        'type': 'heading',
                        'level': 1,
                        'align': 'center',
                        'text': text
                    })
                elif any(k in text for k in DECREE_TITLE_KEYWORDS) and len(text) < 80:
                    elements.append({
                        'type': 'heading',
                        'level': 2,
                        'align': 'center' if (is_centered or len(text) < 45) else 'left',
                        'text': text
                    })
                elif SECTION_TITLE_RE.match(text):
                    elements.append({
                        'type': 'heading',
                        'level': 2,
                        'align': 'center' if is_centered else 'left',
                        'text': text
                    })
                elif (h >= med_height * 1.25) and (line_w < w_img * 0.75) and len(text) < 85:
                    elements.append({
                        'type': 'heading',
                        'level': 2,
                        'align': 'center' if is_centered else 'left',
                        'text': text
                    })
                elif is_centered and len(text) < 65:
                    elements.append({
                        'type': 'paragraph',
                        'align': 'center',
                        'text': text
                    })
                elif is_right or (re.search(r'(?:រាជធានីភ្នំពេញ|ភ្នំពេញ|ធ្វើនៅ|ថ្ងៃទី)\s*,?\s*ថ្ងៃទី\s*[0-9\u17E0-\u17E9]+\s*ខែ', text) and line_w < w_img * 0.65):
                    elements.append({
                        'type': 'paragraph',
                        'align': 'right',
                        'text': text
                    })
                elif is_wide:
                    elements.append({
                        'type': 'paragraph',
                        'align': 'justify',
                        'text': text
                    })
                else:
                    elements.append({
                        'type': 'paragraph',
                        'align': 'left',
                        'text': text
                    })
                idx += 1

    if elements:
        c_count = _add_structured_json_to_docx(doc, elements)
        if c_count > 0:
            return c_count

    # Fallback to image_to_string if spatial clustering found no words
    try:
        raw_text = pytesseract.image_to_string(gray_image, lang=tess_lang, config='--psm 6')
        clean_raw = raw_text.strip()
        if clean_raw:
            return _add_markdown_to_docx(doc, clean_raw)
    except Exception as e:
        logger.error(f'_tesseract_ocr_page_structured: image_to_string fallback failed: {e}', exc_info=True)

    return 0


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

            yield (seq_idx + 1, total_to_process, pil_img)
            del pil_img
            gc.collect()
    finally:
        if fitz_doc is not None:
            try:
                fitz_doc.close()
            except Exception:
                pass


@shared_task
def ocr_pdf(job_id):
    from apps.pdf.models import Job
    import logging
    import pytesseract
    from PIL import Image
    logger = logging.getLogger(__name__)
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()

    ocr_lang = job.compression_level or 'eng'

    lang_map = {
        'eng': 'eng',
        'khm': 'khm',
        'eng+khm': 'eng+khm'
    }
    tess_lang = lang_map.get(ocr_lang, 'eng')

    tesseract_cmd = getattr(settings, 'TESSERACT_CMD', None)
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    tessdata_dir = getattr(settings, 'TESSDATA_DIR', None)
    if tessdata_dir:
        os.environ['TESSDATA_PREFIX'] = tessdata_dir
    
    try:
        input_path = job.file.path
        file_size_mb = os.path.getsize(input_path) / (1024 * 1024)
        logger.info(f'Starting OCR to editable Word, file size: {file_size_mb:.1f}MB')
        
        from docx import Document
        from docx.shared import Pt, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement

        doc = Document()
        for section in doc.sections:
            section.top_margin = Inches(1)
            section.bottom_margin = Inches(1)
            section.left_margin = Inches(1)
            section.right_margin = Inches(1)

        def clean_text(text):
            if not text:
                return ""
            lines = text.split('\n')
            cleaned = []
            for line in lines:
                line = line.strip()
                if len(line) < 2:
                    continue
                line = line.rstrip('|_»¿¡')
                if line:
                    cleaned.append(line)
            return '\n'.join(cleaned)
        
        # Set table auto-style and default Complex Script font for Khmer
        doc.styles['Normal'].font.name = 'Calibri'
        doc.styles['Normal'].font.size = Pt(11)
        try:
            norm_rPr = doc.styles['Normal']._element.get_or_add_rPr()
            norm_rFonts = norm_rPr.get_or_add_rFonts()
            norm_rFonts.set(qn('w:cs'), 'Khmer OS Battambang')
        except Exception:
            pass

        extracted_chars = 0

        # ── Optional AI OCR engines (admin-configured) ──
        ocr_settings = _get_ocr_settings()
        local_engine = (ocr_settings.local_engine or 'khmerocr') if ocr_settings else 'khmerocr'
        claude_key = (ocr_settings.claude_api_key or '').strip() if ocr_settings else ''
        use_claude = bool(ocr_settings and ocr_settings.use_claude and claude_key)
        claude_model = (ocr_settings.claude_model or 'claude-3-5-sonnet-20241022').strip() if ocr_settings else 'claude-3-5-sonnet-20241022'

        gemini_key = (ocr_settings.gemini_api_key or '').strip() if ocr_settings else ''
        use_gemini = bool(ocr_settings and ocr_settings.use_gemini and gemini_key)
        gemini_model = (ocr_settings.gemini_model or 'gemini-flash-lite-latest').strip() if ocr_settings else 'gemini-flash-lite-latest'
        if gemini_model in ('gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-flash', 'gemini-flash-latest'):
            gemini_model = 'gemini-flash-lite-latest'

        vision_client = None
        vision_fallback_ok = True
        if ocr_settings:
            vision_fallback_ok = ocr_settings.fallback_to_tesseract

        if not use_claude and not use_gemini and ocr_settings and ocr_settings.use_google_vision and ocr_settings.google_credentials_json.strip():
            try:
                vision_client = _build_vision_client(ocr_settings)
                logger.info('OCR: using Google Cloud Vision (DOCUMENT_TEXT_DETECTION)')
            except Exception as e:
                logger.error(f'OCR: Google Vision client init failed: {e}', exc_info=True)
                if not vision_fallback_ok:
                    raise RuntimeError(f'Google Vision is enabled but could not be configured: {e}')
                vision_client = None  # falls through to Tesseract below

        if use_claude:
            logger.info(f'OCR: using Claude AI Vision ({claude_model})')
        elif use_gemini:
            logger.info(f'OCR: using Google Gemini Flash Vision ({gemini_model})')

        vision_lang_hints = _VISION_LANG_HINTS.get(ocr_lang, ['en'])

        ocr_dpi = 250 if (use_claude or use_gemini) else 200
        # Stream page-by-page (Low memory, handles 100+ pages without OOM)
        page_stream = _iter_document_pages(
            input_path,
            page_range_str=job.page_range,
            max_limit=150,
            dpi=ocr_dpi
        )

        for page_num, total_pages, page in page_stream:
            logger.info(f'Processing page {page_num}/{total_pages}')
            # Update job progress in database for real-time polling
            try:
                job.files = {'current_page': page_num, 'total_pages': total_pages}
                job.save(update_fields=['files'])
            except Exception:
                pass

            gray = page.convert('L')
            page_done = False

            # ── 1. Try Claude AI Vision first, if configured ──
            if use_claude:
                try:
                    ctext = _claude_ocr_page(claude_key, claude_model, page, ocr_lang=ocr_lang)
                    if ctext and ctext.strip():
                        c_count = _add_structured_content_to_docx(doc, ctext)
                        extracted_chars += c_count
                    page_done = True
                except Exception as e:
                    logger.error(f'Claude OCR failed on page {page_num}: {e}', exc_info=True)
                    if not vision_fallback_ok:
                        raise RuntimeError(f'Claude AI OCR failed on page {page_num}: {e}')
                    # else: fall through to Gemini, Google Vision, or KhmerOCR

            # ── 2. Try Gemini Flash Vision, if configured and Claude didn't run ──
            if not page_done and use_gemini:
                try:
                    gtext = _gemini_ocr_page(gemini_key, gemini_model, page, ocr_lang=ocr_lang)
                    if gtext and gtext.strip():
                        c_count = _add_structured_content_to_docx(doc, gtext)
                        extracted_chars += c_count
                    page_done = True
                    logger.info(f'Gemini Flash Vision OCR succeeded on page {page_num}')
                except Exception as e:
                    logger.error(f'Gemini OCR failed on page {page_num}: {e}', exc_info=True)
                    if not vision_fallback_ok:
                        raise RuntimeError(f'Gemini OCR failed on page {page_num}: {e}')
                    # else: fall through to Google Vision or KhmerOCR

            # ── 3. Try Google Vision, if configured and Claude/Gemini didn't run ──
            if not page_done and vision_client:
                try:
                    vtext = clean_text(_vision_ocr_page(vision_client, page, lang_hints=vision_lang_hints))
                    if vtext:
                        for line in vtext.split('\n'):
                            line = line.strip()
                            if not line:
                                continue
                            p = doc.add_paragraph(line)
                            extracted_chars += len(line)
                            has_khmer = any('ក' <= c <= '៿' for c in line)
                            font_name = 'Khmer OS Battambang' if has_khmer else 'Calibri'
                            for run in p.runs:
                                run.font.name = font_name
                                run.font.size = Pt(11)
                                if has_khmer:
                                    rPr = run._element.get_or_add_rPr()
                                    rFonts = rPr.get_or_add_rFonts()
                                    rFonts.set(qn('w:cs'), font_name)
                                    rFonts.set(qn('w:ascii'), font_name)
                                    rFonts.set(qn('w:hAnsi'), font_name)
                                    rFonts.set(qn('w:eastAsia'), font_name)
                    page_done = True
                except Exception as e:
                    logger.error(f'Google Vision OCR failed on page {page_num}: {e}', exc_info=True)
                    if not vision_fallback_ok:
                        raise RuntimeError(f'Google Vision OCR failed on page {page_num}: {e}')
                    # else: fall through to Tesseract for this page

            # ── 4. Try KhmerOCR ONNX (Zero-Cost Local Deep Learning) ──
            if not page_done and local_engine == 'khmerocr' and ocr_lang in ('khm', 'eng+khm'):
                try:
                    k_chars = _khmerocr_page(doc, page, ocr_lang=ocr_lang)
                    if k_chars > 0:
                        extracted_chars += k_chars
                        page_done = True
                        logger.info(f'KhmerOCR ONNX extracted {k_chars} chars on page {page_num}')
                except Exception as e:
                    logger.warning(f'KhmerOCR failed on page {page_num}: {e}, falling back to Tesseract', exc_info=True)
                    # Falls through to Tesseract below

            # ── 5. Run Tesseract with Spatial Layout & Table Reconstruction ──
            if not page_done:
                try:
                    t_chars = _tesseract_ocr_page_structured(doc, gray, tess_lang=tess_lang)
                    if t_chars > 0:
                        extracted_chars += t_chars
                        page_done = True
                        logger.info(f'Tesseract structured layout extracted {t_chars} chars on page {page_num}')
                except Exception as e:
                    logger.error(f'Tesseract structured OCR failed on page {page_num}: {e}', exc_info=True)

            if page_num < total_pages:
                doc.add_page_break()

            # Clean up page images immediately from RAM
            del page
            del gray
            import gc; gc.collect()

        if extracted_chars == 0:
            raise RuntimeError(
                'OCR produced no text on any page. This usually means Tesseract-OCR '
                'is not installed / not found (check TESSERACT_CMD in settings.py or '
                'that tesseract is on PATH), the wrong language pack is missing '
                f"(requested '{tess_lang}'), or the source pages are blank/too low quality."
            )

        orig_name = getattr(job, 'original_filename', None)
        if not orig_name and job.file:
            orig_name = os.path.basename(job.file.name)
        base_name = os.path.splitext(orig_name or 'document')[0].strip() or 'document'
        output_filename = f'{base_name}.docx'
        output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        doc.save(output_path)

        job.result.save(output_filename, open(output_path, 'rb'))
        os.remove(output_path)

        job.status = 'done'
        job.save()

        return {'status': 'done', 'job_id': str(job_id)}

    except Exception as e:
        logger.error(f'OCR error: {e}', exc_info=True)
        job.status = 'failed'
        job.error_message = str(e)
        job.save()
        raise


@shared_task
def pdf_to_image_task(job_id):
    from apps.pdf.models import Job
    import zipfile
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()
    
    try:
        input_path = job.file.path
        image_format = job.page_range or 'png'
        dpi = int(job.compression_level or '300')
        
        from pdf2image import convert_from_path

        logger.info(f'Converting PDF to images, format: {image_format}, DPI: {dpi}')

        poppler_path = getattr(settings, 'POPPLER_PATH', None)
        pages = convert_from_path(input_path, dpi=dpi, poppler_path=poppler_path)

        base_name = os.path.splitext(os.path.basename(job.file.name))[0]
        zip_filename = f'{base_name}_images.zip'
        zip_path = os.path.join(settings.MEDIA_ROOT, 'processed', zip_filename)
        os.makedirs(os.path.dirname(zip_path), exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for i, page in enumerate(pages):
                img_bytes = io.BytesIO()
                ext = 'jpg' if image_format == 'jpg' else 'png'
                save_fmt = 'JPEG' if ext == 'jpg' else 'PNG'
                page.save(img_bytes, format=save_fmt)
                img_bytes.seek(0)
                zip_file.writestr(f'page_{i+1:03d}.{ext}', img_bytes.read())
        
        job.result.save(zip_filename, open(zip_path, 'rb'))
        os.remove(zip_path)
        
        job.status = 'done'
        job.save()
        
        return {'status': 'done', 'job_id': str(job_id)}
        
    except Exception as e:
        logger.error(f'PDF to Image error: {e}', exc_info=True)
        job.status = 'failed'
        job.error_message = str(e)
        job.save()
        raise


@shared_task
def image_to_pdf_task(file_paths, job_id):
    from apps.pdf.models import Job
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()
    
    try:
        first_base_name = ''
        if file_paths:
            first_base_name = os.path.splitext(os.path.basename(file_paths[0]))[0]
        
        output_filename = f'{first_base_name}.pdf' if first_base_name else f'images_{uuid.uuid4().hex[:8]}.pdf'
        output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        images = []
        for img_path in file_paths:
            try:
                if os.path.exists(img_path):
                    img = Image.open(img_path)
                    if img.mode == 'RGBA':
                        background = Image.new('RGB', img.size, (255, 255, 255))
                        background.paste(img, mask=img.split()[3])
                        img = background
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')
                    images.append(img)
            except Exception as e:
                logger.error(f'Error loading image {img_path}: {e}')
                continue
        
        if images:
            images[0].save(output_path, save_all=True, append_images=images[1:])
            job.result.save(output_filename, open(output_path, 'rb'))
            os.remove(output_path)
        
        job.status = 'done'
        job.save()
        
        return {'status': 'done', 'job_id': str(job_id)}
        
    except Exception as e:
        logger.error(f'Image to PDF error: {e}', exc_info=True)
        job.status = 'failed'
        job.error_message = str(e)
        job.save()
        raise
