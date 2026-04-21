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


class LunarDate(models.Model):
    """Khmer Lunar Calendar - stores lunar month and day for each solar date"""
    solar_date = models.DateField(unique=True)
    lunar_month = models.IntegerField()  # 1-15 (waxing), 16-30 (waning)
    lunar_day = models.IntegerField()  # 1-15
    lunar_year = models.IntegerField()  # Buddhist Era
    khmer_month_name = models.CharField(max_length=50)  # មករា, បុណ្យចូលឆ្នាំ, etc.
    khmer_day_name = models.CharField(max_length=50)  # ច័ន្ទ, អង្គារ, etc.
    is_holy_day = models.BooleanField(default=False)  # ថ្ងៃបុណ្យ
    is_full_moon = models.BooleanField(default=False)
    is_new_moon = models.BooleanField(default=False)
    
    class Meta:
        ordering = ['-solar_date']
        verbose_name = 'Lunar Date'
        verbose_name_plural = 'Lunar Dates'
    
    def __str__(self):
        return f"{self.solar_date} - {self.khmer_month_name} {self.lunar_day}"


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