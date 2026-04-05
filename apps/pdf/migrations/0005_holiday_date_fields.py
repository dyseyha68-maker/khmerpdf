# Generated migration for Holiday model - handles existing data

from django.db import migrations, models
from datetime import date


def convert_to_start_date(apps, schema_editor):
    Holiday = apps.get_model('pdf', 'Holiday')
    for holiday in Holiday.objects.all():
        if holiday.day and holiday.month:
            try:
                holiday.start_date = date(2026, holiday.month, holiday.day)
                holiday.save()
            except:
                holiday.start_date = date(2026, 1, 1)
                holiday.save()


def reverse_convert(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('pdf', '0004_holiday'),
    ]

    operations = [
        migrations.AddField(
            model_name='holiday',
            name='start_date',
            field=models.DateField(default=date(2026, 1, 1), verbose_name='Start Date'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='holiday',
            name='end_date',
            field=models.DateField(blank=True, null=True, verbose_name='End Date'),
        ),
        migrations.RunPython(convert_to_start_date, reverse_convert),
        migrations.RemoveField(
            model_name='holiday',
            name='day',
        ),
        migrations.RemoveField(
            model_name='holiday',
            name='month',
        ),
        migrations.AlterModelOptions(
            name='holiday',
            options={'ordering': ['start_date']},
        ),
    ]
