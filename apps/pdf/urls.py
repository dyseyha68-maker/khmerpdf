from django.urls import path
from . import views

urlpatterns = [
    path('compress/', views.compress_api, name='compress'),
    path('merge/', views.merge_api, name='merge'),
    path('split/', views.split_api, name='split'),
    path('organize/', views.organize_api, name='organize'),
    path('ocr/', views.ocr_api, name='ocr'),
    path('job/<uuid:job_id>/', views.job_status, name='job_status'),
]
