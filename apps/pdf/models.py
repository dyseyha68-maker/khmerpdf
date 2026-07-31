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
    name_kh = models.CharField(max_length=200, blank=True, null=True, verbose_name='Name (Khmer)')
    start_date = models.DateField(verbose_name='Start Date')
    end_date = models.DateField(verbose_name='End Date', blank=True, null=True)
    is_public = models.BooleanField(default=True, verbose_name='Public Holiday')
    is_lunar = models.BooleanField(default=False, verbose_name='Lunar Calendar')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['start_date']
    
    def __str__(self):
        return f"{self.start_date} - {self.name_kh or self.name_en or 'Holiday'}"


class CalendarEvent(models.Model):
    """Events, celebrations, and holidays stored in database"""
    EVENT_TYPES = [
        ('public', 'Public Holiday'),
        ('religious', 'Religious Day'),
        ('national', 'National Day'),
        ('festival', 'Festival'),
        ('custom', 'Custom Event'),
    ]
    
    title_en = models.CharField(max_length=200, blank=True)
    title_kh = models.CharField(max_length=200, verbose_name='Title (Khmer)')
    description_en = models.TextField(blank=True)
    description_kh = models.TextField(blank=True, verbose_name='Description (Khmer)')
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES, default='custom')
    
    # Can be solar or lunar date
    solar_date = models.DateField(blank=True, null=True)
    lunar_month = models.IntegerField(blank=True, null=True)
    lunar_day = models.IntegerField(blank=True, null=True)
    lunar_year = models.IntegerField(blank=True, null=True)
    
    # Recurring yearly
    is_recurring = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['solar_date', 'lunar_month', 'lunar_day']
    
    def __str__(self):
        return self.title_kh or self.title_en


class PageVisit(models.Model):
    """Lightweight visitor log — one row per page view, written by
    apps.pdf.middleware.VisitorTrackingMiddleware."""
    path = models.CharField(max_length=255, db_index=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.CharField(max_length=255, blank=True)
    referrer = models.CharField(max_length=500, blank=True)
    language = models.CharField(max_length=10, blank=True)
    is_bot = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_at', 'path']),
        ]
        verbose_name = 'Page Visit'
        verbose_name_plural = 'Page Visits'

    def __str__(self):
        return f'{self.path} — {self.ip_address} — {self.created_at:%Y-%m-%d %H:%M}'


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