import os
import time
from django.conf import settings
from django.core.management import execute_from_command_line


def cleanup():
    """Delete files older than 24 hours - runs automatically when any job is created"""
    cutoff = time.time() - (24 * 3600)
    deleted_count = 0
    
    for folder in ['uploads', 'processed']:
        folder_path = os.path.join(settings.MEDIA_ROOT, folder)
        if not os.path.exists(folder_path):
            continue
        
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    if os.path.getmtime(file_path) < cutoff:
                        os.remove(file_path)
                        deleted_count += 1
                except:
                    pass
    
    print(f'Cleaned up {deleted_count} old files')


if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    
    cleanup()
    print('Done')