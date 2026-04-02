from django.contrib import admin
from .models import Job

@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ['id', 'tool', 'status', 'created_at']
    list_filter = ['status', 'tool', 'created_at']
    search_fields = ['id']
    readonly_fields = ['id', 'created_at']
