import os
import time
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = 'Clean up old uploaded and processed files'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=24, help='Delete files older than this many hours')

    def handle(self, *args, **options):
        hours = options['hours']
        cutoff = time.time() - (hours * 3600)
        deleted_count = 0
        deleted_size = 0

        for folder in ['uploads', 'processed']:
            folder_path = os.path.join(settings.MEDIA_ROOT, folder)
            if not os.path.exists(folder_path):
                continue

            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        if os.path.getmtime(file_path) < cutoff:
                            size = os.path.getsize(file_path)
                            os.remove(file_path)
                            deleted_count += 1
                            deleted_size += size
                            self.stdout.write(f'Deleted: {file_path}')
                    except Exception as e:
                        self.stderr.write(f'Error deleting {file_path}: {e}')

        size_mb = deleted_size / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f'Deleted {deleted_count} files ({size_mb:.2f} MB)'))