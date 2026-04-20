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
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch

logger = logging.getLogger(__name__)


def compress_with_ghostscript(input_path, output_path, compression_level='recommended'):
    """Use Ghostscript for powerful PDF compression - best for scanned documents"""
    
    original_size = os.path.getsize(input_path)
    original_size_mb = original_size / (1024 * 1024)
    logger.info(f'Input file size: {original_size_mb:.2f} MB')
    
    preset_map = {
        'low': '/screen',
        'recommended': '/ebook',
        'high': '/prepress',
        'extreme': '/screen',
        'less': '/ebook',
    }
    preset = preset_map.get(compression_level, '/screen')
    
    timeout = max(120, int(original_size_mb * 6))
    timeout = min(timeout, 900)
    
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
        '-dMaxBitmap=500000000',
        '-dBufferSpace=100000000',
        f'-sOutputFile={output_path}',
        input_path
    ]
    
    logger.info(f'Running Ghostscript with preset: {preset}')
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Exception(f'Compression timed out for large file ({original_size_mb:.1f}MB)')
    
    if result.returncode != 0:
        raise Exception(f'Ghostscript error: {result.stderr[:200]}')
    
    if not os.path.exists(output_path):
        raise Exception('Ghostscript did not create output file')
    
    output_size = os.path.getsize(output_path)
    output_size_mb = output_size / (1024 * 1024)
    logger.info(f'Ghostscript result: {output_size_mb:.2f} MB (was {original_size_mb:.2f} MB)')
    
    if output_size >= original_size:
        logger.warning(f'Ghostscript did not reduce size, trying more aggressive compression')
        os.remove(output_path)
        
        more_aggressive_cmd = [
            'gs',
            '-sDEVICE=pdfwrite',
            '-dCompatibilityLevel=1.4',
            '-dPDFSETTINGS=/screen',
            '-dDetectDuplicateImages=true',
            '-dRemoveUnusedResources=true',
            '-dCompressFonts=true',
            '-dSubsetFonts=true',
            '-dMonoImageFilter=/DCTFilter',
            '-dColorImageFilter=/DCTFilter',
            '-dAutoFilterColorImages=false',
            '-dAutoFilterMonoImages=false',
            '-dColorImageDownsampleThreshold=1.0',
            '-dColorImageDownsampleType=/Bicubic',
            '-dColorImageResolution=72',
            '-dMonoImageResolution=72',
            '-dNOPAUSE',
            '-dQUIET',
            '-dBATCH',
            '-dSAFER',
            f'-sOutputFile={output_path}',
            input_path
        ]
        
        try:
            result = subprocess.run(more_aggressive_cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            logger.error(f'Aggressive compression failed: {e}')
        
        if os.path.exists(output_path):
            output_size = os.path.getsize(output_path)
            output_size_mb = output_size / (1024 * 1024)
            logger.info(f'Aggressive Ghostscript result: {output_size_mb:.2f} MB')
        else:
            raise Exception('Both Ghostscript and aggressive compression failed to produce output')
    
    return output_path


def compress_with_pymupdf(input_path, output_path, compression_level='recommended'):
    """Fallback: Use PyMuPDF for compression - works for embedded images"""
    
    quality_map = {
        'low': 10,
        'extreme': 10,
        'recommended': 30,
        'high': 50,
        'less': 50,
    }
    quality = quality_map.get(compression_level, 30)
    
    max_dim_map = {
        'low': 300,
        'extreme': 300,
        'recommended': 600,
        'high': 1200,
        'less': 1200,
    }
    max_dim = max_dim_map.get(compression_level, 600)
    
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


@shared_task
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
                try:
                    compress_with_pymupdf(input_path, output_path, compression_level)
                except Exception as pymupdf_err:
                    logger.error(f'Both compression methods failed: GS={gs_err}, PyMuPDF={pymupdf_err}')
                    raise Exception(f'Compression failed: {pymupdf_err}')
            
            if not os.path.exists(output_path):
                raise Exception(f'Compression failed: output file not created')
            
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


@shared_task
def ocr_pdf(job_id):
    from apps.pdf.models import Job
    import logging
    import pytesseract
    from PIL import Image
    import fitz  # PyMuPDF
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
    
    try:
        input_path = job.file.path
        file_size_mb = os.path.getsize(input_path) / (1024 * 1024)
        logger.info(f'Starting searchable OCR, file size: {file_size_mb:.1f}MB, lang: {tess_lang}')
        
        from pdf2image import convert_from_path
        
        pages = convert_from_path(input_path, dpi=200)
        max_pages = min(len(pages), 30)
        
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
        
        # Create new PDF with searchable text overlay
        output_filename = f'ocr_{uuid.uuid4().hex[:8]}.pdf'
        output_path = os.path.join(settings.MEDIA_ROOT, 'processed', output_filename)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Create new PDF document
        new_doc = fitz.open()
        
        for i in range(max_pages):
            logger.info(f'Processing page {i+1}/{max_pages}')
            page_img = pages[i]
            
            # Get page dimensions
            width, height = page_img.size
            
            # Save page as image
            img_path = os.path.join(settings.MEDIA_ROOT, 'processed', f'temp_{uuid.uuid4().hex[:8]}.png')
            page_img.save(img_path, 'PNG')
            
            # Create new page in PDF with same dimensions
            page_width_pt = width * 72 / 200  # Convert pixels to points
            page_height_pt = height * 72 / 200
            new_page = new_doc.new_page(width=page_width_pt, height=page_height_pt)
            
            # Insert the image as background
            new_page.insert_image(fitz.Rect(0, 0, page_width_pt, page_height_pt), filename=img_path)
            
            # Run OCR to get text with positions
            try:
                # Get detailed OCR data with bounding boxes
                data = pytesseract.image_to_data(page_img, lang=tess_lang, output_type=pytesseract.Output.DICT)
                
                # Add each word as invisible text layer
                n = len(data['text'])
                for j in range(n):
                    text = data['text'][j].strip()
                    if text and int(data['conf'][j]) > 30:  # Only add high-confidence text
                        x = data['left'][j]
                        y = data['top'][j]
                        w = data['width'][j]
                        h = data['height'][j]
                        
                        # Insert invisible text at the position
                        # Scale coordinates from image to PDF points
                        x_pt = x * page_width_pt / width
                        y_pt = y * page_height_pt / height
                        w_pt = w * page_width_pt / width
                        h_pt = h * page_height_pt / height
                        
                        # Insert invisible text
                        try:
                            new_page.insert_text(
                                fitz.Point(x_pt, y_pt + h_pt * 0.7),
                                text,
                                fontsize=h_pt * 0.8,
                                color=(0, 0, 0)
                            )
                        except:
                            pass
            except Exception as e:
                logger.warning(f'OCR position failed for page {i+1}: {e}')
            
            # Clean up temp image
            if os.path.exists(img_path):
                os.remove(img_path)
        
        # Save the searchable PDF
        new_doc.save(output_path)
        new_doc.close()
        
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
        
        # Use PIL to create PDF directly
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
