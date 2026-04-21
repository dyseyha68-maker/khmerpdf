from django.contrib import admin
from .models import Job, Holiday, LunarDate, CalendarEvent


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ['id', 'tool', 'status', 'created_at']
    list_filter = ['status', 'tool', 'created_at']
    search_fields = ['id']
    readonly_fields = ['id', 'created_at']


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ['name_kh', 'name_en', 'start_date', 'end_date', 'is_public']
    list_filter = ['is_public', 'is_lunar']
    search_fields = ['name_kh', 'name_en']
    ordering = ['-start_date']


@admin.register(LunarDate)
class LunarDateAdmin(admin.ModelAdmin):
    list_display = ['solar_date', 'khmer_month_name', 'lunar_day', 'lunar_year', 'is_holy_day']
    list_filter = ['is_holy_day', 'is_full_moon', 'is_new_moon']
    search_fields = ['khmer_month_name', 'khmer_day_name']
    ordering = ['-solar_date']
    readonly_fields = ['solar_date']


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display = ['title_kh', 'title_en', 'event_type', 'solar_date']
    list_filter = ['event_type', 'is_active']
    search_fields = ['title_kh', 'title_en']
    ordering = ['solar_date']
