# Delete all holidays first, then add new fields

from django.db import migrations


def delete_all_holidays(apps, schema_editor):
    Holiday = apps.get_model('pdf', 'Holiday')
    Holiday.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('pdf', '0004_holiday'),
    ]

    operations = [
        migrations.RunPython(delete_all_holidays, migrations. RunPython.noop),
    ]
