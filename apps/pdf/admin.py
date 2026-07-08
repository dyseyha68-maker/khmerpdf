import csv
import io
from datetime import datetime

from django.contrib import admin
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.html import format_html
from .models import Job, Holiday, LunarDate, CalendarEvent


# ── Job Admin ─────────────────────────────────────────────────────────────────

@admin.action(description='Mark selected jobs as failed')
def mark_failed(modeladmin, request, queryset):
    updated = queryset.update(status='failed')
    messages.warning(request, f'{updated} job(s) marked as failed.')

@admin.action(description='Delete result files and reset to pending')
def reset_jobs(modeladmin, request, queryset):
    import os
    count = 0
    for job in queryset:
        if job.result and job.result.name:
            try:
                if os.path.exists(job.result.path):
                    os.remove(job.result.path)
            except Exception:
                pass
        job.result = None
        job.status = 'pending'
        job.error_message = ''
        job.save()
        count += 1
    messages.success(request, f'{count} job(s) reset to pending.')


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ['short_id', 'tool', 'status_badge', 'file_size_display', 'created_at']
    list_filter  = ['status', 'tool', 'created_at']
    search_fields = ['id']
    readonly_fields = ['id', 'created_at']
    actions = [mark_failed, reset_jobs]
    ordering = ['-created_at']

    def short_id(self, obj):
        return str(obj.id)[:8] + '…'
    short_id.short_description = 'ID'

    def status_badge(self, obj):
        colors = {
            'done': '#059669', 'processing': '#2563eb',
            'failed': '#dc2626', 'pending': '#92400e',
        }
        color = colors.get(obj.status, '#64748b')
        return format_html(
            '<span style="background:{};color:white;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{}</span>',
            color, obj.status.upper()
        )
    status_badge.short_description = 'Status'

    def file_size_display(self, obj):
        if obj.file:
            try:
                size = obj.file.size
                return f'{size / 1024 / 1024:.1f} MB'
            except Exception:
                return '—'
        return '—'
    file_size_display.short_description = 'File Size'


# ── Holiday Admin ─────────────────────────────────────────────────────────────

@admin.action(description='Mark selected as public holiday')
def make_public(modeladmin, request, queryset):
    updated = queryset.update(is_public=True)
    messages.success(request, f'{updated} holiday(s) marked as public.')

@admin.action(description='Mark selected as non-public')
def make_non_public(modeladmin, request, queryset):
    updated = queryset.update(is_public=False)
    messages.success(request, f'{updated} holiday(s) marked as non-public.')


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display   = ['name_kh', 'name_en', 'start_date', 'end_date', 'public_badge', 'lunar_badge']
    list_filter    = ['is_public', 'is_lunar', 'start_date']
    search_fields  = ['name_kh', 'name_en']
    ordering       = ['start_date']
    actions        = [make_public, make_non_public]
    list_per_page  = 30

    fieldsets = (
        ('Names', {'fields': ('name_kh', 'name_en')}),
        ('Dates', {'fields': ('start_date', 'end_date')}),
        ('Type', {'fields': ('is_public', 'is_lunar')}),
    )

    # ── CSV Import ────────────────────────────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('import-csv/', self.admin_site.admin_view(self.import_csv_view), name='pdf_holiday_import_csv'),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['import_csv_url'] = reverse('admin:pdf_holiday_import_csv')
        return super().changelist_view(request, extra_context=extra_context)

    def import_csv_view(self, request):
        if request.method == 'POST':
            csv_file = request.FILES.get('csv_file')
            if not csv_file:
                messages.error(request, 'No file selected.')
                return HttpResponseRedirect(request.path)
            if not csv_file.name.endswith('.csv'):
                messages.error(request, 'Please upload a .csv file.')
                return HttpResponseRedirect(request.path)

            decoded = csv_file.read().decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(decoded))

            created = updated = skipped = 0
            errors = []

            for i, row in enumerate(reader, start=2):  # row 1 = header
                try:
                    start_raw = row.get('start_date', '').strip()
                    if not start_raw:
                        errors.append(f'Row {i}: missing start_date — skipped.')
                        skipped += 1
                        continue

                    # Accept YYYY-MM-DD or DD/MM/YYYY
                    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y'):
                        try:
                            start_date = datetime.strptime(start_raw, fmt).date()
                            break
                        except ValueError:
                            continue
                    else:
                        errors.append(f'Row {i}: unrecognised date "{start_raw}" — skipped.')
                        skipped += 1
                        continue

                    end_raw = row.get('end_date', '').strip()
                    end_date = None
                    if end_raw:
                        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y'):
                            try:
                                end_date = datetime.strptime(end_raw, fmt).date()
                                break
                            except ValueError:
                                continue

                    def to_bool(val):
                        return str(val).strip().lower() in ('1', 'true', 'yes', 'y')

                    defaults = {
                        'name_en':   row.get('name_en', '').strip(),
                        'name_kh':   row.get('name_kh', '').strip(),
                        'end_date':  end_date,
                        'is_public': to_bool(row.get('is_public', '1')),
                        'is_lunar':  to_bool(row.get('is_lunar', '0')),
                    }

                    _, was_created = Holiday.objects.update_or_create(
                        start_date=start_date,
                        defaults=defaults,
                    )
                    if was_created:
                        created += 1
                    else:
                        updated += 1

                except Exception as e:
                    errors.append(f'Row {i}: {e}')
                    skipped += 1

            summary = f'Import done — {created} created, {updated} updated, {skipped} skipped.'
            if errors:
                messages.warning(request, summary + ' Errors: ' + ' | '.join(errors[:10]))
            else:
                messages.success(request, summary)

            return HttpResponseRedirect(reverse('admin:pdf_holiday_changelist'))

        # GET — show the upload form
        context = {
            **self.admin_site.each_context(request),
            'title': 'Import Holidays from CSV',
            'opts': self.model._meta,
            'csv_columns': 'name_en, name_kh, start_date, end_date, is_public, is_lunar',
            'example_rows': [
                'New Year,បុណ្យចូលឆ្នាំថ្មី,2027-01-01,,1,0',
                'Khmer New Year,បុណ្យចូលឆ្នាំខ្មែរ,2027-04-14,2027-04-16,1,0',
            ],
        }
        return render(request, 'admin/pdf/holiday/import_csv.html', context)

    # ── Badges ───────────────────────────────────────────────────

    def public_badge(self, obj):
        if obj.is_public:
            return format_html('<span style="color:#059669;font-weight:700">✔ Public</span>')
        return format_html('<span style="color:#94a3b8">Private</span>')
    public_badge.short_description = 'Public'

    def lunar_badge(self, obj):
        return '🌙 Lunar' if obj.is_lunar else '☀️ Solar'
    lunar_badge.short_description = 'Calendar'


# ── LunarDate Admin ───────────────────────────────────────────────────────────

@admin.action(description='Populate lunar calendar (run management command)')
def run_populate_calendar(modeladmin, request, queryset):
    from django.core.management import call_command
    try:
        call_command('populate_calendar')
        messages.success(request, 'Lunar calendar populated successfully.')
    except Exception as e:
        messages.error(request, f'Error: {e}')

@admin.action(description='Delete ALL lunar date entries')
def delete_all_lunar(modeladmin, request, queryset):
    count = LunarDate.objects.all().delete()[0]
    messages.warning(request, f'Deleted {count} lunar date entries.')


@admin.register(LunarDate)
class LunarDateAdmin(admin.ModelAdmin):
    list_display   = ['solar_date', 'khmer_month_name', 'lunar_day', 'lunar_year', 'moon_phase', 'is_holy_day']
    list_filter    = ['is_holy_day', 'is_full_moon', 'is_new_moon', 'khmer_month_name']
    search_fields  = ['khmer_month_name', 'khmer_day_name']
    ordering       = ['-solar_date']
    readonly_fields = ['solar_date']
    actions        = [run_populate_calendar, delete_all_lunar]
    list_per_page  = 50

    def moon_phase(self, obj):
        if obj.is_full_moon:
            return '🌕 Full'
        if obj.is_new_moon:
            return '🌑 New'
        return '—'
    moon_phase.short_description = 'Moon'


# ── CalendarEvent Admin ───────────────────────────────────────────────────────

@admin.action(description='Activate selected events')
def activate_events(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    messages.success(request, f'{updated} event(s) activated.')

@admin.action(description='Deactivate selected events')
def deactivate_events(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    messages.success(request, f'{updated} event(s) deactivated.')


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display   = ['title_kh', 'title_en', 'event_type_badge', 'solar_date', 'active_badge']
    list_filter    = ['event_type', 'is_active', 'is_recurring']
    search_fields  = ['title_kh', 'title_en']
    ordering       = ['solar_date']
    actions        = [activate_events, deactivate_events]
    list_per_page  = 30

    fieldsets = (
        ('Titles', {'fields': ('title_kh', 'title_en')}),
        ('Descriptions', {'fields': ('description_kh', 'description_en'), 'classes': ('collapse',)}),
        ('Event Type', {'fields': ('event_type', 'is_recurring', 'is_active')}),
        ('Solar Date', {'fields': ('solar_date',)}),
        ('Lunar Date (optional)', {
            'fields': ('lunar_month', 'lunar_day', 'lunar_year'),
            'classes': ('collapse',)
        }),
    )

    def event_type_badge(self, obj):
        colors = {
            'public': '#059669', 'religious': '#d97706',
            'national': '#2563eb', 'festival': '#7c3aed', 'custom': '#64748b',
        }
        color = colors.get(obj.event_type, '#64748b')
        return format_html(
            '<span style="background:{};color:white;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{}</span>',
            color, obj.get_event_type_display()
        )
    event_type_badge.short_description = 'Type'

    def active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#059669;font-weight:700">● Active</span>')
        return format_html('<span style="color:#94a3b8">○ Inactive</span>')
    active_badge.short_description = 'Status'
