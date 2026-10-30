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
        ('protect', 'Protect'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.FileField(upload_to=pdf_upload_path, blank=True, null=True)
    original_filename = models.CharField(max_length=255, blank=True, null=True)
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


class OCRSettings(models.Model):
    """Singleton row (always pk=1) — lets the admin switch the OCR engine
    from Tesseract (free, local) to Google Cloud Vision (paid, more
    accurate — especially for Khmer script) without touching code or
    redeploying. See apps/pdf/tasks.py:ocr_pdf for how this is read."""

    use_claude = models.BooleanField(
        default=False,
        verbose_name='Use Claude AI (Anthropic) for OCR',
        help_text='Off by default — Turn this on and provide an Anthropic API key below to use Claude vision models (e.g. Claude 3.5 Sonnet / Haiku) for high-accuracy OCR.',
    )
    claude_api_key = models.CharField(
        max_length=255,
        blank=True,
        verbose_name='Anthropic API Key',
        help_text='Paste your Anthropic API key (starts with sk-ant-...) from console.anthropic.com. Kept secure.',
    )
    claude_model = models.CharField(
        max_length=60,
        default='claude-3-5-sonnet-20241022',
        verbose_name='Claude Model',
        help_text='e.g. claude-3-5-sonnet-20241022 (highest precision for complex scans) or claude-3-5-haiku-20241022 (fast, economical).',
    )
    use_gemini = models.BooleanField(
        default=False,
        verbose_name='Use Google Gemini Flash for OCR',
        help_text='Ultra-low-cost, high-speed Vision AI with excellent Khmer accuracy. Free tier available at aistudio.google.com.',
    )
    gemini_api_key = models.CharField(
        max_length=255,
        blank=True,
        verbose_name='Google Gemini API Key',
        help_text='Paste your Gemini API key from Google AI Studio (aistudio.google.com). Free tier includes up to 15 RPM for free.',
    )
    gemini_model = models.CharField(
        max_length=60,
        default='gemini-3.5-flash-lite',
        verbose_name='Gemini Model',
        help_text='e.g. gemini-3.5-flash-lite (fast, high-precision Khmer OCR, $0 free tier), gemini-3-flash-preview, or gemini-pro-latest.',
    )
    use_google_vision = models.BooleanField(
        default=False,
        verbose_name='Use Google Cloud Vision for OCR',
        help_text='Off by default — OCR uses the built-in Tesseract engine (free). '
                   'Turn this on once a service account key is pasted below.',
    )
    google_credentials_json = models.TextField(
        blank=True,
        verbose_name='Google Cloud service account JSON',
        help_text='Paste the full contents of the service account key file (the JSON you '
                   'downloaded from Google Cloud Console → IAM & Admin → Service Accounts). '
                   'Kept only in this row — treat it like a password.',
    )
    LOCAL_ENGINE_CHOICES = [
        ('khmerocr', 'KhmerOCR ONNX (Deep Learning — 3M lines, 800+ fonts) [Recommended]'),
        ('tesseract', 'Tesseract 5 (Standard / Legacy)'),
    ]
    local_engine = models.CharField(
        max_length=20,
        choices=LOCAL_ENGINE_CHOICES,
        default='khmerocr',
        verbose_name='Local OCR Engine (Zero-Cost)',
        help_text='Engine used for zero-cost local OCR when Cloud/AI models are disabled or as fallback. '
                   'KhmerOCR is an ONNX deep learning model trained on 3 million text lines over 800+ Khmer fonts.',
    )
    fallback_to_tesseract = models.BooleanField(
        default=True,
        verbose_name='Fall back to Tesseract if Cloud / AI engine fails',
        help_text='Recommended. Keeps OCR working if the API key is invalid, the monthly '
                   'quota is used up, or the external AI service is unreachable.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'OCR Settings'
        verbose_name_plural = 'OCR Settings'

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce a single row
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # the singleton row is never deleted

    def __str__(self):
        return 'OCR Settings'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SiteTranslation(models.Model):
    CATEGORY_CHOICES = [
        ('General', 'General'),
        ('Tools', 'Tools & Menus'),
        ('Organize', 'Organize PDF'),
        ('Compress', 'Compress PDF'),
        ('Merge', 'Merge PDF'),
        ('Split', 'Split PDF'),
        ('Protect', 'Protect PDF'),
        ('OCR', 'OCR'),
        ('ImageToPdf', 'Image to PDF'),
        ('PdfToImage', 'PDF to Image'),
        ('Calendar', 'Calendar'),
        ('SEO', 'SEO & Meta'),
    ]

    msgid = models.CharField(
        max_length=500,
        unique=True,
        verbose_name='English (Source)',
        help_text='Original English text as written in templates'
    )
    msgstr = models.TextField(
        verbose_name='Khmer Translation',
        help_text='Khmer translation for this text'
    )
    category = models.CharField(
        max_length=30,
        choices=CATEGORY_CHOICES,
        default='General',
        verbose_name='Category'
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Active',
        help_text='Inactive translations fall back to default English'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'msgid']
        verbose_name = 'Translation (EN → KH)'
        verbose_name_plural = 'Translations (EN → KH)'

    def __str__(self):
        return f"{self.msgid} → {self.msgstr[:40]}"


class AdSnippet(models.Model):
    PLACEMENT_CHOICES = [
        ('head', 'Header / <head> Tag (Meta / Domain Verification / Global Scripts)'),
        ('top_banner', 'Top Banner (Below Header Navigation)'),
        ('sidebar', 'Tool Sidebar / Control Panel Slot'),
        ('bottom_banner', 'Bottom Banner (Above Footer)'),
        ('footer_script', 'Footer / Before </body> (Social Bar, Popunder, Tracking)'),
    ]

    PAGE_TARGET_CHOICES = [
        ('all', 'All Pages'),
        ('home_only', 'Homepage Only'),
        ('tools_only', 'Tool Pages Only (OCR, Merge, Compress, etc.)'),
        ('exclude_policy', 'Exclude Legal Pages (Privacy, Terms, Contact)'),
    ]

    DEVICE_CHOICES = [
        ('all', 'All Devices (Desktop & Mobile)'),
        ('desktop', 'Desktop Only'),
        ('mobile', 'Mobile Only'),
    ]

    name = models.CharField(
        max_length=150,
        blank=True,
        default='',
        verbose_name='Name / Label',
        help_text='Internal label for this ad snippet (e.g., "Adsterra Social Bar"). Auto-generated if left blank.'
    )
    placement = models.CharField(
        max_length=30,
        choices=PLACEMENT_CHOICES,
        default='footer_script',
        verbose_name='Placement Slot',
        help_text='Choose where this code snippet will be injected in the HTML layout'
    )
    code = models.TextField(
        verbose_name='Ad / Script Code',
        help_text='Paste the raw HTML or <script> code provided by Adsterra or your ad network'
    )
    page_target = models.CharField(
        max_length=30,
        choices=PAGE_TARGET_CHOICES,
        default='all',
        verbose_name='Page Target',
        help_text='Control which pages will display this ad code'
    )
    device_target = models.CharField(
        max_length=20,
        choices=DEVICE_CHOICES,
        default='all',
        verbose_name='Device Target',
        help_text='Target specific screen sizes'
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Active',
        help_text='Uncheck to instantly pause or disable this ad snippet without deleting it'
    )
    order = models.PositiveIntegerField(
        default=0,
        blank=True,
        verbose_name='Display Order',
        help_text='Order of execution/display if multiple snippets share the same slot (lower numbers first)'
    )
    notes = models.TextField(
        blank=True,
        verbose_name='Notes / Description',
        help_text='Optional notes (e.g. ad unit ID, banner size, revenue notes)'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', '-created_at']
        verbose_name = 'Ad & Script Code'
        verbose_name_plural = 'Ads & Script Codes'

    def save(self, *args, **kwargs):
        if not self.name or not self.name.strip():
            placement_label = self.get_placement_display().split(' (')[0]
            self.name = f"Adsterra ({placement_label})"
        if self.order is None:
            self.order = 0
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} [{self.get_placement_display()}]"