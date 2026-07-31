import os
import sys
import django
from django.core.management.base import BaseCommand
from apps.pdf.models import Holiday, CalendarEvent
from datetime import date, timedelta
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Populate Cambodia public holiday data'

    def handle(self, *args, **options):
        self.stdout.write('Populating Cambodia public holidays...')

        # Create public holidays
        self.create_holidays()

        self.stdout.write(self.style.SUCCESS('Successfully populated calendar data'))

    def create_holidays(self):
        """Create public holidays for Cambodia"""
        holidays_data = [
            {'name_en': 'New Year', 'name_kh': 'បុណ្យចូលឆ្នាំថ្មី', 'date': date(2026, 1, 1), 'is_public': True},
            {'name_en': 'Victory Day', 'name_kh': 'ទិវាជ័យជម្នះ', 'date': date(2026, 1, 7), 'is_public': True},
            {'name_en': 'Meak Bochea', 'name_kh': 'មាឃបូជា', 'date': date(2026, 2, 15), 'is_public': True},
            {'name_en': 'Khmer New Year', 'name_kh': 'បុណ្យចូលឆ្នាំខ្មែរ', 'date': date(2026, 4, 14), 'is_public': True},
            {'name_en': 'Labor Day', 'name_kh': 'ទិវាពលកម្ម', 'date': date(2026, 5, 1), 'is_public': True},
            {'name_en': 'Pchum Ben', 'name_kh': 'បុណ្យភ្ជុំបិណ្យ', 'date': date(2026, 9, 22), 'is_public': True},
            {'name_en': "King's Birthday", 'name_kh': 'បុណ្យព្រះរាជហុង', 'date': date(2026, 10, 15), 'is_public': True},
            {'name_en': 'Independence Day', 'name_kh': 'ទិវាឯករាជ្យ', 'date': date(2026, 11, 9), 'is_public': True},
            # 2027
            {'name_en': 'New Year 2027', 'name_kh': 'បុណ្យចូលឆ្នាំថ្មី', 'date': date(2027, 1, 1), 'is_public': True},
            {'name_en': 'Victory Day 2027', 'name_kh': 'ទិវាជ័យជម្នះ', 'date': date(2027, 1, 7), 'is_public': True},
            {'name_en': 'Khmer New Year 2027', 'name_kh': 'បុណ្យចូលឆ្នាំខ្មែរ', 'date': date(2027, 4, 14), 'is_public': True},
        ]

        for h in holidays_data:
            Holiday.objects.update_or_create(
                start_date=h['date'],
                defaults={
                    'name_en': h['name_en'],
                    'name_kh': h['name_kh'],
                    'is_public': h['is_public']
                }
            )

        self.stdout.write(f'Created {len(holidays_data)} holidays')
