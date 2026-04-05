# Generated migration for Holiday model update

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pdf', '0004_holiday'),
    ]

    operations = [
        migrations.AddField(
            model_name='holiday',
            name='start_date',
            field=models.DateField(default='2026-01-01', verbose_name='Start Date'),
        ),
        migrations.AddField(
            model_name='holiday',
            name='end_date',
            field=models.DateField(blank=True, null=True, verbose_name='End Date'),
        ),
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
