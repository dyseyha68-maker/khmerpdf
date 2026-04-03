import os
import time
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.conf import settings
from apscheduler.schedulers.blocking import BlockingScheduler


class Command(BaseCommand):
    help = 'Run cleanup scheduler'

    def handle(self, *args, **options):
        scheduler = BlockingScheduler()
        
        scheduler.add_job(
            self.cleanup,
            'interval',
            hours=24,
            id='cleanup_job'
        )
        
        self.stdout.write(self.style.SUCCESS('Scheduler started - will cleanup every 24 hours'))
        scheduler.start()
    
    def cleanup(self):
        from django.core.management import call_command
        call_command('cleanup_files')


def run_scheduler():
    from django.core.management import execute_from_command_line
    execute_from_command_line(['manage.py', 'runcleanup'])