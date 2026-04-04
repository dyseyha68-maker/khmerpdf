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
    list_display = ['name_kh', 'name_en', 'day', 'month', 'year', 'is_public']
    list_filter = ['is_public', 'month', 'year']
    search_fields = ['name_kh', 'name_en']
    ordering = ['month', 'day']
    
    fieldsets = (
        ('Holiday Info', {
            'fields': ('name_kh', 'name_en', 'day', 'month', 'year', 'is_public')
        }),
    )