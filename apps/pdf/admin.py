from django.contrib import admin
from .models import Job, Holiday


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ['id', 'tool', 'status', 'created_at']
    list_filter = ['status', 'tool', 'created_at']
    search_fields = ['id']
    readonly_fields = ['id', 'created_at']


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ['name_kh', 'name_en', 'start_date', 'end_date', 'is_public']
    list_filter = ['is_public']
    search_fields = ['name_kh', 'name_en']
    ordering = ['-start_date']