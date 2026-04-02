import os
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


def khqr(request):
    download_url = request.GET.get('download', '')
    return render(request, 'khqr.html', {'download_url': download_url})


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def compress_api(request):
    files = request.FILES.getlist('files')
    compression_level = request.data.get('compression_level', 'extreme')
    
    if not files:
        return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    for f in files:
        if not f.name.lower().endswith('.pdf'):
            return Response({'error': 'Only PDF files are allowed'}, status=status.HTTP_400_BAD_REQUEST)
        if f.size > settings.MAX_UPLOAD_SIZE:
            return Response({'error': f'File {f.name} too large. Max 50MB'}, status=status.HTTP_400_BAD_REQUEST)
    
    if len(files) == 1:
        job = Job.objects.create(file=files[0], tool='compress', compression_level=compression_level)
    else:
        file_ids = []
        for f in files:
            job = Job.objects.create(file=f, tool='upload')
            file_ids.append(str(job.id))
        job = Job.objects.create(files=file_ids, tool='compress', compression_level=compression_level)
    
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        compress_pdf(str(job.id))
        job.refresh_from_db()
        
        return Response({
            'job_id': str(job.id),
            'status': job.status,
            'result_url': job.result.url if job.result else None,
            'message': 'File compressed successfully'
        }, status=status.HTTP_201_CREATED)
    else:
        compress_pdf.delay(str(job.id))
        
        return Response({
            'job_id': str(job.id),
            'status': 'pending',
            'message': 'Job created successfully'
        }, status=status.HTTP_201_CREATED)


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
            return Response({'error': f'File {f.name} too large. Max 50MB'}, status=status.HTTP_400_BAD_REQUEST)
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
        return Response({'error': 'File too large. Max 50MB'}, status=status.HTTP_400_BAD_REQUEST)
    
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
        return Response({'error': 'File too large. Max 50MB'}, status=status.HTTP_400_BAD_REQUEST)
    
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
