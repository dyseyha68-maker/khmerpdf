import os
import uuid
from django.db import models


def pdf_upload_path(instance, filename):
    ext = filename.split('.')[-1]
    filename = f'{uuid.uuid4()}.{ext}'
    return os.path.join('uploads', filename)


def pdf_processed_path(instance, filename):
    ext = filename.split('.')[-1]
    filename = f'processed_{uuid.uuid4()}.{ext}'
    return os.path.join('processed', filename)


class Holiday(models.Model):
    name_en = models.CharField(max_length=200, blank=True, null=True)
    name_kh = models.CharField(max_length=200, verbose_name='Name (Khmer)')
    start_date = models.DateField(verbose_name='Start Date', default='2026-01-01')
    end_date = models.DateField(verbose_name='End Date', blank=True, null=True)
    year = models.IntegerField(verbose_name='Year', blank=True, null=True)
    is_public = models.BooleanField(default=True, verbose_name='Public Holiday')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['start_date']
    
    def __str__(self):
        if self.end_date:
            return f"{self.start_date} - {self.end_date} - {self.name_kh}"
        return f"{self.start_date} - {self.name_kh}"
    
    def get_days_in_range(self):
        from datetime import date, timedelta
        days = []
        if not self.start_date:
            return days
        if self.end_date:
            current = self.start_date
            while current <= self.end_date:
                days.append({'day': current.day, 'month': current.month})
                current += timedelta(days=1)
        else:
            days = [{'day': self.start_date.day, 'month': self.start_date.month}]
        return days


class Job(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ]

    TOOL_CHOICES = [
        ('compress', 'Compress'),
        ('merge', 'Merge'),
        ('split', 'Split'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.FileField(upload_to=pdf_upload_path, blank=True, null=True)
    files = models.JSONField(blank=True, null=True, default=list)
    result = models.FileField(upload_to=pdf_processed_path, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    tool = models.CharField(max_length=20, choices=TOOL_CHOICES, default='compress')
    created_at = models.DateTimeField(auto_now_add=True)
    error_message = models.TextField(blank=True, null=True)
    page_range = models.CharField(max_length=100, blank=True, null=True)
    compression_level = models.CharField(max_length=20, default='extreme')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.tool} - {self.status} - {self.id}'