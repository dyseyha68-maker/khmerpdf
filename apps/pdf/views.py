import os
import threading
import time
import uuid
from django.conf import settings
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status

from .models import Job, Holiday
from django.db import models
from .tasks import compress_pdf, merge_pdf, split_pdf, organize_pdf


def cleanup_old_files():
    """Delete files older than 24 hours - runs automatically when any job is created"""
    try:
        cutoff = time.time() - (24 * 3600)
        deleted_count = 0
        
        for folder in ['uploads', 'processed']:
            folder_path = os.path.join(settings.MEDIA_ROOT, folder)
            if not os.path.exists(folder_path):
                continue
            
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        if os.path.getmtime(file_path) < cutoff:
                            os.remove(file_path)
                            deleted_count += 1
                    except:
                        pass
        
        if deleted_count > 0:
            print(f'Cleaned up {deleted_count} old files')
    except Exception as e:
        print(f'Cleanup error: {e}')


def index(request):
    return render(request, 'upload.html')


def split_page(request):
    return render(request, 'split.html')


def merge_page(request):
    return render(request, 'merge.html')


def compress_page(request):
    return render(request, 'compress.html')


def organize_page(request):
    return render(request, 'organize.html')


def calendar_page(request):
    from django.http import HttpResponse
    from .models import Holiday
    import json
    from datetime import datetime, timedelta
    
    year = request.GET.get('year')
    if not year:
        year = datetime.now().year
    else:
        try:
            year = int(year)
        except:
            year = datetime.now().year
    
    holidays = []
    try:
        holiday_objs = Holiday.objects.all()
        for h in holiday_objs:
            try:
                start = getattr(h, 'start_date', None)
                end = getattr(h, 'end_date', None)
                if start:
                    if end:
                        current = start
                        while current <= end:
                            holidays.append({'day': current.day, 'month': current.month, 'name_en': h.name_en or '', 'name_kh': h.name_kh or ''})
                            current += timedelta(days=1)
                    else:
                        holidays.append({'day': start.day, 'month': start.month, 'name_en': h.name_en or '', 'name_kh': h.name_kh or ''})
            except:
                continue
    except:
        pass
    
    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Calendar</title>
    <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Kantumruy Pro', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; }}
        .container {{ max-width: 900px; margin: 0 auto; background: white; border-radius: 16px; padding: 24px; }}
        h1 {{ text-align: center; margin-bottom: 20px; color: #333; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
        .year-nav button {{ background: #667eea; color: white; border: none; width: 40px; height: 40px; border-radius: 50%; cursor: pointer; font-size: 18px; }}
        .year-nav span {{ font-size: 24px; font-weight: 700; min-width: 100px; text-align: center; }}
        .month-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 20px; }}
        .month-btn {{ padding: 12px; background: #f5f5f5; border: none; border-radius: 8px; cursor: pointer; font-family: 'Kantumruy Pro', sans-serif; }}
        .month-btn:hover {{ background: #e0e0e0; }}
        .month-btn.active {{ background: #d32f2f; color: white; }}
        .calendar-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 8px; }}
        .day-header {{ text-align: center; padding: 10px; font-weight: 700; color: #666; }}
        .calendar-day {{ aspect-ratio: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #f5f5f5; border-radius: 8px; font-size: 16px; }}
        .calendar-day.today {{ background: #d32f2f; color: white; }}
        .calendar-day.holiday {{ background: #ffcdd2; color: #c62828; }}
        .calendar-day.saturday, .calendar-day.sunday {{ background: #ffebee; color: #c62828; }}
        .khmer-day {{ font-size: 10px; color: #999; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Cambodia Calendar</h1>
        <div class="header">
            <div class="year-nav">
                <button onclick="changeYear(-1)">◀</button>
                <span id="yearDisplay">{year}</span>
                <button onclick="changeYear(1)">▶</button>
            </div>
        </div>
        <div class="month-grid" id="monthGrid"></div>
        <div class="calendar-grid" id="calendarGrid"></div>
    </div>
    <script>
        const khmerMonths = ['មករា', 'កុម្ភៈ', 'មីនា', 'មេសា', 'ឧសភា', 'មិថុនា', 'កក្កដា', 'សីហា', 'កញ្ញា', 'តុលា', 'វិចិ្ឆកា', 'ធ្នូ'];
        const khmerDays = ['ច', 'អ', 'ព', 'ព្រ', 'ស', 'សៅ', 'អា'];
        let holidays = {json.dumps(holidays)};
        let currentYear = {year};
        let currentMonth = new Date().getMonth();
        
        function changeYear(delta) {{ currentYear += delta; document.getElementById('yearDisplay').textContent = currentYear; render(); }}
        function selectMonth(m) {{ currentMonth = m; render(); }}
        
        function render() {{
            const grid = document.getElementById('calendarGrid');
            const monthGrid = document.getElementById('monthGrid');
            grid.innerHTML = '';
            ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach(d => {{
                const h = document.createElement('div');
                h.className = 'day-header';
                h.textContent = d;
                grid.appendChild(h);
            }});
            
            const first = new Date(currentYear, currentMonth, 1);
            const days = new Date(currentYear, currentMonth + 1, 0).getDate();
            let start = first.getDay();
            start = start === 0 ? 6 : start - 1;
            
            const today = new Date();
            
            for (let i = 0; i < start + days; i++) {{
                const dayNum = i - start + 1;
                if (dayNum < 1 || dayNum > days) {{ grid.appendChild(document.createElement('div')); continue; }}
                const div = document.createElement('div');
                div.className = 'calendar-day';
                const dow = new Date(currentYear, currentMonth, dayNum).getDay();
                const hol = holidays.find(h => h.day === dayNum && h.month === currentMonth + 1);
                if (today.getFullYear() === currentYear && today.getMonth() === currentMonth && today.getDate() === dayNum) div.classList.add('today');
                else if (hol) div.classList.add('holiday');
                else if (dow === 6 || dow === 0) div.classList.add(dow === 6 ? 'saturday' : 'sunday');
                div.innerHTML = dayNum + '<span class="khmer-day">' + khmerDays[dow] + '</span>';
                grid.appendChild(div);
            }}
            
            monthGrid.innerHTML = '';
            khmerMonths.forEach((m, i) => {{
                const btn = document.createElement('button');
                btn.className = 'month-btn' + (i === currentMonth ? ' active' : '');
                btn.textContent = m;
                btn.onclick = () => selectMonth(i);
                monthGrid.appendChild(btn);
            }});
        }}
        
        render();
    </script>
</body>
</html>'''
    return HttpResponse(html)


def ocr_page(request):
    return render(request, 'ocr.html')


def pdf_to_image_page(request):
    return render(request, 'pdftoimage.html')


def image_to_pdf_page(request):
    return render(request, 'imagetopdf.html')


def khqr(request):
    download_url = request.GET.get('download', '')
    return render(request, 'khqr.html', {'download_url': download_url})


@api_view(['POST'])
@csrf_exempt
@parser_classes([MultiPartParser, FormParser])
def compress_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
    # Cleanup old files every time a job is created
    cleanup_old_files()
    
    files = request.FILES.getlist('files')
    compression_level = request.data.get('compression_level', 'extreme')
    
    logger.info(f'Compress API called, files count: {len(files)}')
    
    if not files:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    for f in files:
        if not f.name.lower().endswith('.pdf'):
            return Response({'error': 'Only PDF files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
        if f.size > settings.MAX_UPLOAD_SIZE:
            return Response({'error': f'File {f.name} too large. Max 350MB'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        if len(files) == 1:
            job = Job.objects.create(file=files[0], tool='compress', compression_level=compression_level)
        else:
            file_ids = []
            for f in files:
                job = Job.objects.create(file=f, tool='upload')
                file_ids.append(str(job.id))
            job = Job.objects.create(files=file_ids, tool='compress', compression_level=compression_level)
        
        logger.info(f'Job created: {job.id}')
        
        # Run synchronously (CELERY_TASK_ALWAYS_EAGER=True)
        from apps.pdf.tasks import compress_pdf
        try:
            compress_pdf(str(job.id))
        except Exception as task_err:
            logger.error(f'Task error: {task_err}')
            job.status = 'failed'
            job.error_message = str(task_err)
            job.save()
        
        job.refresh_from_db()
        
        if job.status == 'failed':
            return Response({
                'error': job.error_message or 'Compression failed',
                'job_id': str(job.id),
                'status': job.status
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response({
            'job_id': str(job.id),
            'status': job.status,
            'result_url': job.result.url if job.result else None,
            'message': 'File compressed successfully'
        }, status=status.HTTP_201_CREATED)
            
    except Exception as e:
        logger.error(f'Compress API error: {e}', exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@csrf_exempt
@parser_classes([MultiPartParser, FormParser])
def merge_api(request):
    # Cleanup old files every time a job is created
    cleanup_old_files()
    
    files = request.FILES.getlist('files')
    
    if not files:
        return Response({'error': 'No files provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    for f in files:
        if not f.name.lower().endswith('.pdf'):
            return Response({'error': 'Only PDF files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
    
    file_ids = []
    for f in files:
        if f.size > settings.MAX_UPLOAD_SIZE:
            return Response({'error': f'File {f.name} too large. Max 350MB'}, status=status.HTTP_400_BAD_REQUEST)
        job = Job.objects.create(file=f, tool='upload')
        file_ids.append(str(job.id))
    
    job = Job.objects.create(files=file_ids, tool='merge')
    
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        merge_pdf(str(job.id))
        job.refresh_from_db()
        
        return Response({
            'job_id': str(job.id),
            'status': job.status,
            'result_url': job.result.url if job.result else None,
            'message': 'Files merged successfully'
        }, status=status.HTTP_201_CREATED)
    else:
        merge_pdf.delay(str(job.id))
        
        return Response({
            'job_id': str(job.id),
            'status': 'pending',
            'message': 'Job created successfully'
        }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@csrf_exempt
@parser_classes([MultiPartParser, FormParser])
def split_api(request):
    # Cleanup old files every time a job is created
    cleanup_old_files()
    
    file = request.FILES.get('file')
    page_range = request.data.get('page_range', '1')
    split_mode = request.data.get('split_mode', 'range')
    
    if not file:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not file.name.lower().endswith('.pdf'):
        return Response({'error': 'Only PDF files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
    
    if file.size > settings.MAX_UPLOAD_SIZE:
        return Response({'error': 'File too large. Max 350MB'}, status=status.HTTP_400_BAD_REQUEST)
    
    job = Job.objects.create(file=file, tool='split', page_range=page_range, compression_level=split_mode)
    
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        split_pdf(str(job.id))
        job.refresh_from_db()
        
        return Response({
            'job_id': str(job.id),
            'status': job.status,
            'result_url': job.result.url if job.result else None,
            'message': 'File split successfully'
        }, status=status.HTTP_201_CREATED)
    else:
        split_pdf.delay(str(job.id))
        
        return Response({
            'job_id': str(job.id),
            'status': 'pending',
            'message': 'Job created successfully'
        }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
def job_status(request, job_id):
    job = get_object_or_404(Job, id=job_id)
    
    data = {
        'job_id': str(job.id),
        'status': job.status,
        'tool': job.tool,
        'created_at': job.created_at.isoformat(),
    }
    
    if job.status == 'done' and job.result:
        data['result_url'] = job.result.url
        data['result_size'] = job.result.size
    
    if job.status == 'failed':
        data['error'] = job.error_message
    
    return Response(data)


@api_view(['POST'])
@csrf_exempt
@parser_classes([MultiPartParser, FormParser])
def organize_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
    # Cleanup old files every time a job is created
    cleanup_old_files()
    
    file = request.FILES.get('file')
    page_order_json = request.data.get('page_order', '[]')
    
    replace_files = {}
    for key in request.FILES:
        if key.startswith('replace_file_'):
            idx = int(key.split('_')[-1])
            replace_files[idx] = request.FILES[key]
    
    logger.info(f'organize_api called, file: {file}, page_order length: {len(page_order_json)}, replace_files: {list(replace_files.keys())}')
    
    if not file:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not file.name.lower().endswith('.pdf'):
        return Response({'error': 'Only PDF files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
    
    if file.size > settings.MAX_UPLOAD_SIZE:
        return Response({'error': 'File too large. Max 350MB'}, status=status.HTTP_400_BAD_REQUEST)
    
    import json
    try:
        page_order = json.loads(page_order_json)
    except Exception as e:
        logger.error(f'Error parsing page_order: {e}')
        page_order = []
    
    job = Job.objects.create(
        file=file,
        tool='split',
        page_range=json.dumps(page_order),
        compression_level='organize'
    )
    
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        try:
            organize_pdf(str(job.id), replace_files)
            job.refresh_from_db()
        except Exception as e:
            logger.error(f'Error in organize_pdf: {e}')
            job.status = 'failed'
            job.error_message = str(e)
            job.save()
        
        return Response({
            'job_id': str(job.id),
            'status': job.status,
            'result_url': job.result.url if job.result else None,
            'message': 'File organized successfully'
        }, status=status.HTTP_201_CREATED)
    else:
        organize_pdf.delay(str(job.id), replace_files)
        
        return Response({
            'job_id': str(job.id),
            'status': 'pending',
            'message': 'Job created successfully'
        }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
@csrf_exempt
def ocr_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
    # Cleanup old files every time a job is created
    cleanup_old_files()
    
    file = request.FILES.get('file')
    ocr_lang = request.data.get('ocr_lang', 'eng')
    
    logger.info(f'ocr_api called, file: {file}, lang: {ocr_lang}')
    
    if not file:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not file.name.lower().endswith('.pdf'):
        return Response({'error': 'Only PDF files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
    
    if file.size > settings.MAX_UPLOAD_SIZE:
        return Response({'error': 'File too large. Max 350MB'}, status=status.HTTP_400_BAD_REQUEST)
    
    job = Job.objects.create(
        file=file,
        tool='ocr',
        compression_level=ocr_lang
    )
    
    logger.info(f'Created job {job.id}')
    
    # Start OCR in background - don't wait for completion
    from .tasks import ocr_pdf
    
    def run_ocr():
        try:
            ocr_pdf(str(job.id))
        except Exception as e:
            logger.error(f'OCR background error: {e}')
            try:
                job.refresh_from_db()
                job.status = 'failed'
                job.error_message = str(e)
                job.save()
            except:
                pass
    
    # Start background thread - don't wait
    background_thread = threading.Thread(target=run_ocr, daemon=True)
    background_thread.start()
    
    return Response({
        'job_id': str(job.id),
        'status': 'processing',
        'message': 'OCR started'
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@csrf_exempt
@parser_classes([MultiPartParser, FormParser])
def pdf_to_image_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
    # Cleanup old files every time a job is created
    cleanup_old_files()
    
    file = request.FILES.get('file')
    image_format = request.data.get('format', 'png')
    dpi = request.data.get('dpi', '300')
    
    logger.info(f'pdf_to_image_api called, file: {file}, format: {image_format}, dpi: {dpi}')
    
    if not file:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not file.name.lower().endswith('.pdf'):
        return Response({'error': 'Only PDF files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
    
    if file.size > settings.MAX_UPLOAD_SIZE:
        return Response({'error': 'File too large. Max 350MB'}, status=status.HTTP_400_BAD_REQUEST)
    
    job = Job.objects.create(
        file=file,
        tool='pdf_to_image',
        page_range=image_format,
        compression_level=dpi
    )
    
    from .tasks import pdf_to_image_task
    
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        pdf_to_image_task(str(job.id))
        job.refresh_from_db()
        
        return Response({
            'job_id': str(job.id),
            'status': job.status,
            'result_url': job.result.url if job.result else None,
            'message': 'PDF converted to images successfully'
        }, status=status.HTTP_201_CREATED)
    else:
        pdf_to_image_task.delay(str(job.id))
        
        return Response({
            'job_id': str(job.id),
            'status': 'pending',
            'message': 'Job created successfully'
        }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@csrf_exempt
@parser_classes([MultiPartParser, FormParser])
def image_to_pdf_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
    files = request.FILES.getlist('files')
    
    logger.info(f'image_to_pdf_api called, files count: {len(files)}')
    
    if not files:
        return Response({'error': 'No files provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    for f in files:
        content_type = f.content_type or ''
        if not ('image' in content_type or f.type.startswith('image/')):
            return Response({'error': 'Only image files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        uploads_dir = os.path.join(settings.MEDIA_ROOT, 'uploads')
        os.makedirs(uploads_dir, exist_ok=True)
        
        file_paths = []
        for f in files:
            safe_name = f'{uuid.uuid4().hex}_{f.name}'
            temp_path = os.path.join(uploads_dir, safe_name)
            with open(temp_path, 'wb') as dest:
                for chunk in f.chunks():
                    dest.write(chunk)
            file_paths.append(temp_path)
        
        logger.info(f'Saved {len(file_paths)} files')
        
        job = Job.objects.create(tool='image_to_pdf')
        
        from .tasks import image_to_pdf_task
        
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            try:
                result = image_to_pdf_task(file_paths, str(job.id))
            except Exception as e:
                logger.error(f'Task error: {e}')
                job.status = 'failed'
                job.error_message = str(e)
                job.save()
            
            job.refresh_from_db()
            
            if job.status == 'failed':
                return Response({'error': job.error_message or 'Processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            return Response({
                'job_id': str(job.id),
                'status': job.status,
                'result_url': job.result.url if job.result else None,
                'message': 'Images converted to PDF successfully'
            }, status=status.HTTP_201_CREATED)
        else:
            image_to_pdf_task.delay(file_paths, str(job.id))
            
            return Response({
                'job_id': str(job.id),
                'status': 'pending',
                'message': 'Job created successfully'
            }, status=status.HTTP_201_CREATED)
            
    except Exception as e:
        logger.error(f'Image to PDF API error: {e}', exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
