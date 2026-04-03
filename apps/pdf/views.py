import os
import threading
from django.conf import settings
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.urls import reverse
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status

from .models import Job
from .tasks import compress_pdf, merge_pdf, split_pdf, organize_pdf


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
    return render(request, 'calendar.html')


def ocr_page(request):
    return render(request, 'ocr.html')


def pdf_to_image_page(request):
    return render(request, 'pdftoimage.html')


def image_to_pdf_page(request):
    return render(request, 'imagetopdf.html')


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def compress_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
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
        
        if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
            from apps.pdf.tasks import compress_pdf
            compress_pdf(str(job.id))
            job.refresh_from_db()
            
            logger.info(f'Job completed, status: {job.status}, result: {job.result}')
            
            return Response({
                'job_id': str(job.id),
                'status': job.status,
                'result_url': job.result.url if job.result else None,
                'message': 'File compressed successfully'
            }, status=status.HTTP_201_CREATED)
        else:
            from apps.pdf.tasks import compress_pdf
            compress_pdf.delay(str(job.id))
            
            return Response({
                'job_id': str(job.id),
                'status': 'pending',
                'message': 'Job created successfully'
            }, status=status.HTTP_201_CREATED)
            
    except Exception as e:
        logger.error(f'Compress API error: {e}', exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def merge_api(request):
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
@parser_classes([MultiPartParser, FormParser])
def split_api(request):
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
@parser_classes([MultiPartParser, FormParser])
def organize_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
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
def ocr_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
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
@parser_classes([MultiPartParser, FormParser])
def pdf_to_image_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
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
@parser_classes([MultiPartParser, FormParser])
def image_to_pdf_api(request):
    import logging
    logger = logging.getLogger(__name__)
    
    files = request.FILES.getlist('files')
    
    logger.info(f'image_to_pdf_api called, files count: {len(files)}')
    
    if not files:
        return Response({'error': 'No files provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    for f in files:
        if not f.type.startswith('image/'):
            return Response({'error': 'Only image files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
        if f.size > settings.MAX_UPLOAD_SIZE:
            return Response({'error': f'File {f.name} too large. Max 350MB'}, status=status.HTTP_400_BAD_REQUEST)
    
    file_ids = []
    for f in files:
        job = Job.objects.create(file=f, tool='upload')
        file_ids.append(str(job.id))
    
    job = Job.objects.create(files=file_ids, tool='image_to_pdf')
    
    from .tasks import image_to_pdf_task
    
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        image_to_pdf_task(str(job.id))
        job.refresh_from_db()
        
        return Response({
            'job_id': str(job.id),
            'status': job.status,
            'result_url': job.result.url if job.result else None,
            'message': 'Images converted to PDF successfully'
        }, status=status.HTTP_201_CREATED)
    else:
        image_to_pdf_task.delay(str(job.id))
        
        return Response({
            'job_id': str(job.id),
            'status': 'pending',
            'message': 'Job created successfully'
        }, status=status.HTTP_201_CREATED)
