import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pdf', '0008_job_original_filename'),
    ]

    operations = [
        migrations.CreateModel(
            name='PageVisit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('path', models.CharField(db_index=True, max_length=255)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.CharField(blank=True, max_length=255)),
                ('referrer', models.CharField(blank=True, max_length=500)),
                ('language', models.CharField(blank=True, max_length=10)),
                ('is_bot', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                'verbose_name': 'Page Visit',
                'verbose_name_plural': 'Page Visits',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='pagevisit',
            index=models.Index(fields=['created_at', 'path'], name='pdf_pagevis_created_49faf2_idx'),
        ),
    ]
