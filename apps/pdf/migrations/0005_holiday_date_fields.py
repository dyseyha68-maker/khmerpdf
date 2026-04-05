# Generated migration for Holiday model - handles existing data

from django.db import migrations, models
from datetime import date


def set_default_start_date(apps, schema_editor):
    Holiday = apps.get_model('pdf', 'Holiday')
    for holiday in Holiday.objects.filter(start_date__isnull=True):
        holiday.start_date = date(2026, 1, 1)
        holiday.save()


class Migration(migrations.Migration):

    dependencies = [
        ('pdf', '0004_holiday'),
    ]

    operations = [
        migrations.AddField(
            model_name='holiday',
            name='start_date',
            field=models.DateField(default=date(2026, 1, 1), verbose_name='Start Date'),
        ),
        migrations.AddField(
            model_name='holiday',
            name='end_date',
            field=models.DateField(blank=True, null=True, verbose_name='End Date'),
        ),
        migrations.RunPython(set_default_start_date, migrations. RunPython.noop),
        migrations.RemoveField(
            model_name='holiday',
            name='day',
        ),
        migrations.RemoveField(
            model_name='holiday',
            name='month',
        ),
        migrations.RemoveField(
            model_name='holiday',
            name='year',
        ),
        migrations.AlterModelOptions(
            name='holiday',
            options={'ordering': ['-start_date']},
        ),
    ]
