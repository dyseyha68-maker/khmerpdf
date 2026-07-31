import csv
import io
import json
from datetime import datetime, timedelta, date

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from .models import Job, Holiday, LunarDate, CalendarEvent, PageVisit


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
    list_display  = ['short_id', 'tool', 'status_badge', 'file_size_display', 'created_at']
    list_filter   = ['status', 'tool', 'created_at']
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
            '<span style="background:{};color:white;padding:2px 8px;'
            'border-radius:10px;font-size:11px;font-weight:600">{}</span>',
            color, obj.status.upper()
        )
    status_badge.short_description = 'Status'

    def file_size_display(self, obj):
        if obj.file:
            try:
                return f'{obj.file.size / 1024 / 1024:.1f} MB'
            except Exception:
                return '—'
        return '—'
    file_size_display.short_description = 'File Size'


# ── Holiday Admin ─────────────────────────────────────────────────────────────

@admin.action(description='✔ Mark selected as public holiday')
def make_public(modeladmin, request, queryset):
    updated = queryset.update(is_public=True)
    messages.success(request, f'{updated} holiday(s) marked as public.')

@admin.action(description='✖ Mark selected as non-public')
def make_non_public(modeladmin, request, queryset):
    updated = queryset.update(is_public=False)
    messages.success(request, f'{updated} holiday(s) marked as non-public.')

@admin.action(description='📋 Duplicate selected holidays to next year')
def duplicate_to_next_year(modeladmin, request, queryset):
    created = skipped = 0
    for h in queryset:
        try:
            next_start = h.start_date.replace(year=h.start_date.year + 1)
            next_end = h.end_date.replace(year=h.end_date.year + 1) if h.end_date else None
            _, was_created = Holiday.objects.get_or_create(
                start_date=next_start,
                defaults={
                    'name_en': h.name_en,
                    'name_kh': h.name_kh,
                    'end_date': next_end,
                    'is_public': h.is_public,
                    'is_lunar': h.is_lunar,
                }
            )
            if was_created:
                created += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    messages.success(request, f'{created} holiday(s) duplicated to next year, {skipped} already existed.')


class HolidayYearFilter(admin.SimpleListFilter):
    """Quick year filter — shows all years that have holidays."""
    title = 'Year'
    parameter_name = 'year'

    def lookups(self, request, model_admin):
        years = Holiday.objects.dates('start_date', 'year').order_by('-start_date')
        return [(str(d.year), str(d.year)) for d in years]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(start_date__year=self.value())
        return queryset


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    # ── List view ──────────────────────────────────────────────────
    list_display       = ['start_date', 'end_date', 'duration_display', 'name_kh', 'name_en', 'public_badge', 'lunar_badge']
    list_display_links = ['start_date']          # click date to open record
    list_editable      = ['name_kh', 'name_en']  # edit names inline, no need to open
    list_filter        = [HolidayYearFilter, 'is_public', 'is_lunar']
    search_fields      = ['name_kh', 'name_en']
    ordering           = ['start_date']
    date_hierarchy     = 'start_date'            # drill Year → Month → Day at top
    list_per_page      = 20
    save_as            = True   # "Save as new" button — clone any holiday

    actions = [make_public, make_non_public, duplicate_to_next_year]

    fieldsets = (
        ('Names', {'fields': ('name_kh', 'name_en')}),
        ('Dates', {'fields': ('start_date', 'end_date')}),
        ('Type', {'fields': ('is_public', 'is_lunar')}),
    )

    def duration_display(self, obj):
        if obj.end_date and obj.end_date != obj.start_date:
            days = (obj.end_date - obj.start_date).days + 1
            return format_html('<span style="color:#2563eb;font-weight:600">{} days</span>', days)
        return '1 day'
    duration_display.short_description = 'Duration'

    def public_badge(self, obj):
        if obj.is_public:
            return format_html('<span style="color:#059669;font-weight:700">✔ Public</span>')
        return format_html('<span style="color:#94a3b8">Private</span>')
    public_badge.short_description = 'Public'

    def lunar_badge(self, obj):
        return '🌙 Lunar' if obj.is_lunar else '☀️ Solar'
    lunar_badge.short_description = 'Calendar'

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

            for i, row in enumerate(reader, start=2):
                try:
                    start_raw = row.get('start_date', '').strip()
                    if not start_raw:
                        errors.append(f'Row {i}: missing start_date — skipped.')
                        skipped += 1
                        continue
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
                        start_date=start_date, defaults=defaults,
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


# ── LunarDate Admin ───────────────────────────────────────────────────────────

@admin.action(description='🔄 Populate lunar calendar (run management command)')
def run_populate_calendar(modeladmin, request, queryset):
    from django.core.management import call_command
    try:
        call_command('populate_calendar')
        messages.success(request, 'Lunar calendar populated successfully.')
    except Exception as e:
        messages.error(request, f'Error: {e}')

@admin.action(description='🗑 Delete ALL lunar date entries')
def delete_all_lunar(modeladmin, request, queryset):
    count = LunarDate.objects.all().delete()[0]
    messages.warning(request, f'Deleted {count} lunar date entries.')


class LunarYearFilter(admin.SimpleListFilter):
    title = 'Year'
    parameter_name = 'year'

    def lookups(self, request, model_admin):
        years = LunarDate.objects.dates('solar_date', 'year').order_by('-solar_date')
        return [(str(d.year), str(d.year)) for d in years]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(solar_date__year=self.value())
        return queryset


class LunarMonthFilter(admin.SimpleListFilter):
    title = 'Month'
    parameter_name = 'month'

    def lookups(self, request, model_admin):
        return [
            ('1','January'),('2','February'),('3','March'),
            ('4','April'),('5','May'),('6','June'),
            ('7','July'),('8','August'),('9','September'),
            ('10','October'),('11','November'),('12','December'),
        ]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(solar_date__month=self.value())
        return queryset


# ── LunarDate custom form (dropdowns — no Khmer typing needed) ────────────────

def _to_kh_num(n):
    """Convert an integer to Khmer digit string (e.g. 12 → '១២')."""
    digits = '០១២៣៤៥៦៧៨៩'
    return ''.join(digits[int(d)] for d in str(n))

_LUNAR_MONTH_CHOICES = (
    [('', '--- select ---')] +
    [(str(i), f'{_to_kh_num(i)} កើត') for i in range(1, 16)] +
    [(str(i), f'{_to_kh_num(i)} រោច') for i in range(16, 31)]
)

_KHMER_MONTH_CHOICES = [
    ('', '--- select ---'),
    ('មករា',              'មករា  (January)'),
    ('កុម្ភៈ',           'កុម្ភៈ  (February)'),
    ('មីនា',             'មីនា  (March)'),
    ('មេសា',             'មេសា  (April)'),
    ('ឧសភា',             'ឧសភា  (May)'),
    ('មិថុនា',           'មិថុនា  (June)'),
    ('កក្កដា',           'កក្កដា  (July)'),
    ('សីហា',             'សីហា  (August)'),
    ('កញ្ញា',            'កញ្ញា  (September)'),
    ('តុលា',             'តុលា  (October)'),
    ('វិចិ្ឆកា',         'វិចិ្ឆកា  (November)'),
    ('ធ្នូ',             'ធ្នូ  (December)'),
    ('បុណ្យចូលឆ្នាំ',   'បុណ្យចូលឆ្នាំ'),
    ('មាឃបូជា',          'មាឃបូជា'),
    ('ពិធីបុណ្យចូលបិណ្យ','ពិធីបុណ្យចូលបិណ្យ'),
]

_KHMER_DAY_CHOICES = [
    ('', '--- select ---'),
    ('ច័ន្ទ',        'ច័ន្ទ  (Monday)'),
    ('អង្គារ',       'អង្គារ  (Tuesday)'),
    ('ពុធ',          'ពុធ  (Wednesday)'),
    ('ព្រហស្បតិ៍',  'ព្រហស្បតិ៍  (Thursday)'),
    ('សុក្រ',        'សុក្រ  (Friday)'),
    ('សៅរ៍',         'សៅរ៍  (Saturday)'),
    ('អាទិត្យ',      'អាទិត្យ  (Sunday)'),
]


class LunarDateForm(forms.ModelForm):
    # Solar (Gregorian) date entered as three separate, editable number boxes
    # instead of a date picker — and, unlike before, this is no longer
    # read-only, so existing entries can be corrected and new ones added.
    solar_day = forms.IntegerField(
        min_value=1, max_value=31, label='Day (solar)',
        widget=forms.NumberInput(attrs={'style': 'width:70px'}),
    )
    solar_month = forms.IntegerField(
        min_value=1, max_value=12, label='Month (solar)',
        widget=forms.NumberInput(attrs={'style': 'width:70px'}),
    )
    solar_year = forms.IntegerField(
        min_value=1900, max_value=2100, label='Year (solar)',
        widget=forms.NumberInput(attrs={'style': 'width:90px'}),
    )

    lunar_month = forms.ChoiceField(
        choices=_LUNAR_MONTH_CHOICES,
        label='Lunar Month',
        help_text='1–15 = ខ្នើត (Waxing)  ·  16–30 = រោច (Waning)',
    )
    khmer_month_name = forms.ChoiceField(
        choices=_KHMER_MONTH_CHOICES,
        label='Khmer Month Name',
    )
    khmer_day_name = forms.ChoiceField(
        choices=_KHMER_DAY_CHOICES,
        label='Khmer Day Name',
    )

    class Meta:
        model = LunarDate
        exclude = ['solar_date']  # replaced by solar_day / solar_month / solar_year above

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.solar_date:
            self.fields['solar_day'].initial = self.instance.solar_date.day
            self.fields['solar_month'].initial = self.instance.solar_date.month
            self.fields['solar_year'].initial = self.instance.solar_date.year

    def clean_lunar_month(self):
        val = self.cleaned_data.get('lunar_month', '')
        if not val:
            raise forms.ValidationError('Please select a lunar month.')
        return int(val)

    def clean(self):
        cleaned = super().clean()
        d, m, y = cleaned.get('solar_day'), cleaned.get('solar_month'), cleaned.get('solar_year')
        if d and m and y:
            try:
                new_date = date(y, m, d)
            except ValueError:
                raise forms.ValidationError(f'{y}-{m:02d}-{d:02d} is not a valid calendar date.')

            qs = LunarDate.objects.filter(solar_date=new_date)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(f'A lunar date entry for {new_date} already exists — edit that entry instead.')

            cleaned['solar_date'] = new_date
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.solar_date = self.cleaned_data['solar_date']
        if commit:
            instance.save()
        return instance


@admin.register(LunarDate)
class LunarDateAdmin(admin.ModelAdmin):
    form = LunarDateForm
    list_display       = ['solar_date', 'khmer_month_name', 'lunar_day', 'lunar_year', 'moon_phase', 'is_holy_day']
    list_display_links = ['solar_date']
    list_editable      = ['is_holy_day']         # toggle holy day directly in list
    list_filter        = [LunarYearFilter, LunarMonthFilter, 'is_holy_day', 'is_full_moon', 'is_new_moon']
    search_fields      = ['khmer_month_name', 'khmer_day_name']
    ordering           = ['-solar_date']
    actions            = [run_populate_calendar, delete_all_lunar]
    list_per_page      = 31   # one month at a time
    date_hierarchy     = 'solar_date'            # drill Year → Month → Day

    fieldsets = (
        ('Solar Date', {
            'fields': (('solar_day', 'solar_month', 'solar_year'),),
            'description': 'Enter the Gregorian day, month, and year this lunar entry belongs to. Editable — use this to add a missing date or correct an existing one.',
        }),
        ('Lunar Date', {
            'fields': ('lunar_month', 'lunar_day', 'lunar_year'),
            'description': 'lunar_month: 1–15 waxing / 16–30 waning · lunar_day: 1–15 · lunar_year: Buddhist Era',
        }),
        ('Khmer Names', {
            'fields': ('khmer_month_name', 'khmer_day_name'),
        }),
        ('Flags', {
            'fields': ('is_holy_day', 'is_full_moon', 'is_new_moon'),
        }),
    )

    def moon_phase(self, obj):
        if obj.is_full_moon:
            return '🌕 Full'
        if obj.is_new_moon:
            return '🌑 New'
        return '—'
    moon_phase.short_description = 'Moon'


# ── CalendarEvent Admin ───────────────────────────────────────────────────────

@admin.action(description='✔ Activate selected events')
def activate_events(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    messages.success(request, f'{updated} event(s) activated.')

@admin.action(description='✖ Deactivate selected events')
def deactivate_events(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    messages.success(request, f'{updated} event(s) deactivated.')


@admin.register(CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    list_display       = ['solar_date', 'title_kh', 'title_en', 'event_type_badge', 'active_badge']
    list_display_links = ['solar_date']
    list_editable      = ['title_kh', 'title_en']
    list_filter        = ['event_type', 'is_active', 'is_recurring']
    search_fields      = ['title_kh', 'title_en']
    ordering           = ['solar_date']
    date_hierarchy     = 'solar_date'
    list_per_page      = 30
    save_as            = True
    actions            = [activate_events, deactivate_events]

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
            '<span style="background:{};color:white;padding:2px 8px;'
            'border-radius:10px;font-size:11px;font-weight:600">{}</span>',
            color, obj.get_event_type_display()
        )
    event_type_badge.short_description = 'Type'

    def active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#059669;font-weight:700">● Active</span>')
        return format_html('<span style="color:#94a3b8">○ Inactive</span>')
    active_badge.short_description = 'Status'


# ── PageVisit Admin (Visitor Statistics) ──────────────────────────────────────

@admin.register(PageVisit)
class PageVisitAdmin(admin.ModelAdmin):
    list_display   = ['created_at', 'path', 'ip_address', 'short_ua', 'referrer_display', 'bot_badge']
    list_filter    = ['is_bot', 'path', 'created_at']
    search_fields  = ['path', 'ip_address', 'user_agent', 'referrer']
    ordering       = ['-created_at']
    date_hierarchy = 'created_at'
    list_per_page  = 50

    def has_add_permission(self, request):
        return False  # rows are only ever written by the tracking middleware

    def has_change_permission(self, request, obj=None):
        return False  # read-only log

    def short_ua(self, obj):
        return (obj.user_agent[:60] + '…') if len(obj.user_agent) > 60 else obj.user_agent
    short_ua.short_description = 'User Agent'

    def referrer_display(self, obj):
        return obj.referrer[:50] if obj.referrer else '—'
    referrer_display.short_description = 'Referrer'

    def bot_badge(self, obj):
        if obj.is_bot:
            return format_html('<span style="color:#94a3b8">🤖 Bot</span>')
        return format_html('<span style="color:#059669;font-weight:700">● Human</span>')
    bot_badge.short_description = 'Type'

    # ── Stats dashboard ───────────────────────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('stats/', self.admin_site.admin_view(self.stats_view), name='pdf_pagevisit_stats'),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['stats_url'] = reverse('admin:pdf_pagevisit_stats')
        return super().changelist_view(request, extra_context=extra_context)

    def stats_view(self, request):
        human = PageVisit.objects.filter(is_bot=False)

        now = timezone.localtime()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)

        def totals(qs):
            return {
                'visits': qs.count(),
                'unique': qs.exclude(ip_address__isnull=True).values('ip_address').distinct().count(),
            }

        stats_today = totals(human.filter(created_at__gte=today_start))
        stats_week = totals(human.filter(created_at__gte=week_start))
        stats_month = totals(human.filter(created_at__gte=month_start))
        stats_all = totals(human)
        bot_count = PageVisit.objects.filter(is_bot=True).count()

        top_pages = list(
            human.values('path').annotate(count=Count('id')).order_by('-count')[:10]
        )

        # Daily visits for the last 30 days, zero-filled.
        window_start = (today_start - timedelta(days=29)).date()
        daily_qs = (
            human.filter(created_at__date__gte=window_start)
            .annotate(day=TruncDate('created_at'))
            .values('day')
            .annotate(count=Count('id'))
        )
        by_day = {row['day']: row['count'] for row in daily_qs}
        chart_labels, chart_values = [], []
        for i in range(30):
            d = window_start + timedelta(days=i)
            chart_labels.append(d.strftime('%b %d'))
            chart_values.append(by_day.get(d, 0))

        top_referrers = list(
            human.exclude(referrer='').values('referrer').annotate(count=Count('id')).order_by('-count')[:10]
        )

        context = {
            **self.admin_site.each_context(request),
            'title': 'Visitor Statistics',
            'opts': self.model._meta,
            'stats_today': stats_today,
            'stats_week': stats_week,
            'stats_month': stats_month,
            'stats_all': stats_all,
            'bot_count': bot_count,
            'top_pages': top_pages,
            'top_referrers': top_referrers,
            'chart_labels': json.dumps(chart_labels),
            'chart_values': json.dumps(chart_values),
        }
        return render(request, 'admin/pdf/pagevisit/stats.html', context)
