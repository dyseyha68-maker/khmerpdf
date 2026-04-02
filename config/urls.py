from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve
from django.urls import re_path
from django.views.generic import RedirectView
from apps.pdf.views import index, split_page, merge_page, compress_page, organize_page, calendar_page, khqr, ocr_page

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('apps.pdf.urls')),
    path('', index, name='index'),
    path('pdf-split/', split_page, name='split'),
    path('merge/', merge_page, name='merge'),
    path('compress/', compress_page, name='compress'),
    path('organize/', organize_page, name='organize'),
    path('calendar/', calendar_page, name='calendar'),
    path('khqr/', khqr, name='khqr'),
    path('ocr/', ocr_page, name='ocr'),
    
    # SEO files
    path('robots.txt', lambda r: RedirectView.as_view(url='/static/robots.txt', permanent=False)),
    path('sitemap.xml', lambda r: RedirectView.as_view(url='/static/sitemap.xml', permanent=False)),
]

# Serve media files in production
if not settings.DEBUG:
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
    ]

urlpatterns += static(settings.STATIC_URL, document_root=settings.STATICFILES_DIRS[0] if settings.DEBUG else settings.STATIC_ROOT)