import os
import time
from datetime import timedelta
from django.conf import settings


def cleanup():
    """Delete files older than 24 hours"""
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
    
    print(f'Cleaned up {deleted_count} files')


if __name__ == '__main__':
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    
    cleanup()
    print('Daily cleanup completed')