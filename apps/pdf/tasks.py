import os
import uuid
import zipfile
import subprocess
import logging
from celery import shared_task
from django.conf import settings
import fitz
from PIL import Image
import io
from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)


def compress_with_ghostscript(input_path, output_path, compression_level='recommended'):
    """Use Ghostscript for powerful PDF compression - best for scanned documents"""
    
    # Get file size
    file_size_mb = os.path.getsize(input_path) / (1024 * 1024)
    logger.info(f'Input file size: {file_size_mb:.2f} MB')
    
    # PDFSETTINGS presets:
    # /screen   - 72 DPI, smallest size (for web/screen)
    # /ebook    - 150 DPI, balanced (for e-readers) 
    # /printer  - 300 DPI, high quality (for printing)
    # /prepress - 300 DPI, best quality (for professional print)
    
    # More aggressive settings for better compression
    preset_map = {
        'extreme': '/screen',
        'recommended': '/screen',
        'less': '/ebook',
    }
    preset = preset_map.get(compression_level, '/screen')
    
    logger.info(f'Compressing with Ghostscript, preset: {preset}')
    
    # Adjust timeout based on file size (larger files need more time)
    # 1 minute per 10MB, minimum 2 minutes, max 15 minutes
    timeout = max(120, int(file_size_mb * 6))  # 6 seconds per MB
    timeout = min(timeout, 900)  # max 15 minutes
    
    # Ghostscript command for PDF compression with additional optimization flags
    cmd = [
        'gs',
        '-sDEVICE=pdfwrite',
        '-dCompatibilityLevel=1.4',
        f'-dPDFSETTINGS={preset}',
        '-dDetectDuplicateImages=true',
        '-dRemoveUnusedResources=true',
        '-dCompressFonts=true',
        '-dSubsetFonts=true',
        '-dNOPAUSE',
        '-dQUIET',
        '-dBATCH',
        '-dSAFER',
        # Memory settings for large files
        '-dMaxBitmap=500000000',  # 500MB max bitmap
        '-dBufferSpace=100000000',  # 100MB buffer
        f'-sOutputFile={output_path}',
        input_path
    ]
    
    logger.info(f'Running Ghostscript with timeout {timeout}s')
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(f'Ghostscript timed out after {timeout}s')
        raise Exception(f'Compression timed out for large file ({file_size_mb:.1f}MB)')
    
    if result.returncode != 0:
        logger.error(f'Ghostscript error: {result.stderr}')
        # Try fallback method
        raise Exception(f'Ghostscript error: {result.stderr[:200]}')
    
    # Check output file exists and has size
    if os.path.exists(output_path):
        output_size = os.path.getsize(output_path)
        output_size_mb = output_size / (1024 * 1024)
        logger.info(f'Compressed file size: {output_size_mb:.2f} MB (was {file_size_mb:.2f} MB)')
    else:
        raise Exception('Ghostscript did not create output file')
    
    return output_path


def compress_with_pymupdf(input_path, output_path, compression_level='recommended'):
    """Fallback: Use PyMuPDF for compression - works for embedded images"""
    
    quality_map = {
        'extreme': 10,
        'recommended': 30,
        'less': 50,
    }
    quality = quality_map.get(compression_level, 20)
    
    max_dim_map = {
        'extreme': 300,
        'recommended': 600,
        'less': 1200,
    }
    max_dim = max_dim_map.get(compression_level, 400)
    
    doc = fitz.open(input_path)
    
    for page_num, page in enumerate(doc):
        images = page.get_images(full=True)
        for img_index, img in enumerate(images):
            xref = img[0]
            try:
                base_img = doc.extract_image(xref)
                img_data = base_img["image"]
                
                img_pil = Image.open(io.BytesIO(img_data))
                
                if img_pil.mode in ('RGBA', 'P'):
                    img_pil = img_pil.convert('RGB')
                
                if img_pil.width > max_dim or img_pil.height > max_dim:
                    img_pil.thumbnail((max_dim, max_dim), Image.LANCZOS)
                
                output_buffer = io.BytesIO()
                img_pil.save(output_buffer, format='JPEG', quality=quality, optimize=True)
                img_data_compressed = output_buffer.getvalue()
                
                img_rect = page.get_image_rects(xref)
                if img_rect:
                    rect = img_rect[0]
                    page.insert_image(rect, stream=img_data_compressed, keep_proportion=True)
                    try:
                        page.delete_image(xref)
                    except:
                        pass
                        
            except Exception as e:
                continue
    
    doc.save(output_path, deflate=True, garbage=4, clean=True)
    doc.close()


def compress_pdf(job_id):
    from apps.pdf.models import Job
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()
    
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
                            
                            # Try Ghostscript first, fallback to PyMuPDF
                            try:
                                compress_with_ghostscript(input_path, output_path, compression_level)
                                logger.info(f'Used Ghostscript for {base_name}')
                            except Exception as gs_err:
                                logger.warning(f'Ghostscript failed, falling back to PyMuPDF: {gs_err}')
                                # Fallback to PyMuPDF if Ghostscript fails
                                compress_with_pymupdf(input_path, output_path, compression_level)
                            
                            compressed_size = os.path.getsize(output_path)
                            zip_file.write(output_path, output_filename)
                            os.remove(output_path)
                    except Exception as e:
                        continue
            
            job.result.save(zip_filename, open(zip_path, 'rb'))
            os.remove(zip_path)
            job.status = 'done'
            job.save()
            return {'status': 'done', 'job_id': str(job_id)}
        
        else:
            # Single file
            input_path = job.file.path
            original_size = os.path.getsize(input_path)
            base_name = os.path.splitext(os.path.basename(job.file.name))[0]
            output_filename = f'{base_name}_compressed.pdf'
            output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
            
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Try Ghostscript first, fallback to PyMuPDF
            try:
                compress_with_ghostscript(input_path, output_path, compression_level)
                logger.info(f'Used Ghostscript for single file')
            except Exception as gs_err:
                logger.warning(f'Ghostscript failed, falling back to PyMuPDF: {gs_err}')
                # Fallback to PyMuPDF if Ghostscript fails
                compress_with_pymupdf(input_path, output_path, compression_level)
            
            compressed_size = os.path.getsize(output_path)
            
            job.result.save(output_filename, open(output_path, 'rb'))
            os.remove(output_path)
            
            job.status = 'done'
            job.save()
            
            return {'status': 'done', 'job_id': str(job_id), 'original': original_size, 'compressed': compressed_size}
        
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.save()
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
        except:
            page_order = []
        
        reader = PdfReader(input_path)
        total_pages = len(reader.pages)
        
        output_filename = f'organized_{uuid.uuid4().hex[:8]}.pdf'
        output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        writer = PdfWriter()
        
        for page_info in page_order:
            page_idx = page_info.get('index')
            is_blank = page_info.get('isBlank', False)
            is_replaced = page_info.get('isReplaced', False)
            replaced_file_index = page_info.get('replacedFileIndex')
            replaced_page_num = page_info.get('replacedPageNum')
            
            if is_blank:
                from reportlab.pdfgen import canvas
                from reportlab.lib.pagesizes import letter
                
                temp_blank = os.path.join(settings.MEDIA_ROOT, 'processed', f'blank_{uuid.uuid4().hex[:8]}.pdf')
                c = canvas.Canvas(temp_blank, pagesize=letter)
                c.showPage()
                c.save()
                
                blank_reader = PdfReader(temp_blank)
                writer.add_page(blank_reader.pages[0])
                os.remove(temp_blank)
            elif is_replaced and replaced_file_index is not None and replaced_page_num is not None:
                replace_file = replace_files.get(replaced_file_index)
                if replace_file:
                    replace_path = os.path.join(settings.MEDIA_ROOT, 'uploads', replace_file.name)
                    os.makedirs(os.path.dirname(replace_path), exist_ok=True)
                    with open(replace_path, 'wb') as f:
                        for chunk in replace_file.chunks():
                            f.write(chunk)
                    
                    replace_reader = PdfReader(replace_path)
                    if 0 <= replaced_page_num - 1 < len(replace_reader.pages):
                        writer.add_page(replace_reader.pages[replaced_page_num - 1])
                    os.remove(replace_path)
            else:
                orig_page = page_info.get('original')
                if orig_page and 0 <= orig_page - 1 < total_pages:
                    writer.add_page(reader.pages[orig_page - 1])
                elif page_idx is not None and 0 <= page_idx < total_pages:
                    writer.add_page(reader.pages[page_idx])
        
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


def ocr_pdf(job_id):
    from apps.pdf.models import Job
    import logging
    import subprocess
    import sys
    from PIL import Image, ImageEnhance
    import io
    logger = logging.getLogger(__name__)
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()
    
    ocr_lang = job.compression_level or 'eng'
    
    try:
        try:
            import easyocr
        except ImportError:
            logger.info('Installing EasyOCR...')
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet', 'easyocr'])
            import easyocr
        
        input_path = job.file.path
        file_size_mb = os.path.getsize(input_path) / (1024 * 1024)
        logger.info(f'Starting EasyOCR, file size: {file_size_mb:.1f}MB')
        
        from pdf2image import convert_from_path
        from docx import Document
        from docx.shared import Pt
        
        doc = Document()
        style = doc.styles['Normal']
        style.font.name = 'Times New Roman'
        style.font.size = Pt(11)
        
        pages = convert_from_path(input_path, dpi=350)
        max_pages = min(len(pages), 25)
        
        # Initialize EasyOCR reader with better settings
        logger.info('Loading EasyOCR model...')
        reader = easyocr.Reader(['en', 'km'], gpu=False, verbose=False)
        
        def enhance_image(img_pil):
            """Enhance image for better OCR"""
            # Convert to grayscale
            img = img_pil.convert('L')
            
            # Increase contrast
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(1.5)
            
            # Increase sharpness
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(1.5)
            
            return img
        
        def clean_text(text):
            if not text:
                return text
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
        
        for i in range(max_pages):
            logger.info(f'Processing page {i+1}/{max_pages}')
            page = pages[i]
            
            # Save page as image
            temp_img_path = os.path.join(settings.MEDIA_ROOT, 'processed', f'temp_ocr_{i}.png')
            page.save(temp_img_path, format='PNG')
            
            # Try multiple passes with different settings
            all_results = []
            
            # Pass 1: Enhanced image
            try:
                enhanced_img = enhance_image(page)
                enhanced_img.save(temp_img_path)
                results1 = reader.readtext(temp_img_path, detail=0)
                if results1:
                    all_results.append(('\n'.join(results1), len(results1)))
            except Exception as e:
                logger.warning(f'Pass 1 failed: {e}')
            
            # Pass 2: Original image
            try:
                results2 = reader.readtext(temp_img_path, detail=0)
                if results2:
                    all_results.append(('\n'.join(results2), len(results2)))
            except Exception as e:
                logger.warning(f'Pass 2 failed: {e}')
            
            # Pass 3: Try batch mode
            try:
                results3 = reader.readtext(temp_img_path, detail=0, batch_size=1)
                if results3:
                    all_results.append(('\n'.join(results3), len(results3)))
            except Exception as e:
                logger.warning(f'Pass 3 failed: {e}')
            
            # Pick the result with most text (usually most accurate)
            if all_results:
                best = max(all_results, key=lambda x: x[1])
                text = best[0]
            else:
                text = ""
            
            text = clean_text(text)
            
            # Clean up temp file
            if os.path.exists(temp_img_path):
                os.remove(temp_img_path)
            
            doc.add_heading(f'Page {i+1}', level=1)
            
            lines = text.split('\n')
            for line in lines:
                if line.strip():
                    p = doc.add_paragraph(line)
                    has_khmer = any('\u1780' <= c <= '\u17FF' for c in line)
                    run = p.runs[0] if p.runs else p.add_run()
                    if has_khmer:
                        run.font.name = 'Kantumruy Pro'
                    else:
                        run.font.name = 'Times New Roman'
            
            if i < max_pages - 1:
                doc.add_page_break()
        
        output_filename = f'ocr_{uuid.uuid4().hex[:8]}.docx'
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
        
        pages = convert_from_path(input_path, dpi=dpi)
        
        base_name = os.path.splitext(os.path.basename(job.file.name))[0]
        zip_filename = f'{base_name}_images.zip'
        zip_path = os.path.join(settings.MEDIA_ROOT, 'processed', zip_filename)
        os.makedirs(os.path.dirname(zip_path), exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for i, page in enumerate(pages):
                img_bytes = io.BytesIO()
                ext = 'jpg' if image_format == 'jpg' else 'png'
                page.save(img_bytes, format=ext.upper())
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


def image_to_pdf_task(job_id):
    from apps.pdf.models import Job
    
    job = Job.objects.get(id=job_id)
    job.status = 'processing'
    job.save()
    
    try:
        file_ids = job.files if job.files else []
        
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
        
        output_filename = f'{first_base_name}.pdf' if first_base_name else f'images_{uuid.uuid4().hex[:8]}.pdf'
        output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        writer = PdfWriter()
        
        for file_id in file_ids:
            try:
                job_file = Job.objects.get(id=file_id)
                if job_file.file and os.path.exists(job_file.file.path):
                    img = Image.open(job_file.file.path)
                    
                    if img.mode == 'RGBA':
                        background = Image.new('RGB', img.size, (255, 255, 255))
                        background.paste(img, mask=img.split()[3])
                        img = background
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')
                    
                    img_width, img_height = img.size
                    
                    from reportlab.lib.pagesizes import letter
                    from reportlab.pdfgen import canvas
                    
                    pdf_width_pt = img_width * 0.75
                    pdf_height_pt = img_height * 0.75
                    
                    temp_pdf_path = os.path.join(settings.MEDIA_ROOT, 'processed', f'temp_{uuid.uuid4().hex[:8]}.pdf')
                    c = canvas.Canvas(temp_pdf_path, pagesize=(pdf_width_pt, pdf_height_pt))
                    c.set_fill_color(255, 255, 255)
                    c.rect(0, 0, pdf_width_pt, pdf_height_pt, fill=1)
                    
                    img_buffer = io.BytesIO()
                    img.save(img_buffer, format='JPEG', quality=95)
                    img_buffer.seek(0)
                    
                    c.drawImage(img_buffer, 0, 0, width=pdf_width_pt, height=pdf_height_pt)
                    c.showPage()
                    c.save()
                    
                    temp_reader = PdfReader(temp_pdf_path)
                    for page in temp_reader.pages:
                        writer.add_page(page)
                    os.remove(temp_pdf_path)
                    
            except Exception as e:
                logger.warning(f'Error processing image {file_id}: {e}')
                continue
        
        with open(output_path, 'wb') as f:
            writer.write(f)
        
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
