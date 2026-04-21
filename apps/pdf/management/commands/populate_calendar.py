import os
import sys
import django
from django.core.management.base import BaseCommand
from apps.pdf.models import LunarDate, Holiday, CalendarEvent
from datetime import date, timedelta
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Populate Khmer lunar calendar data'
    
    # Khmer month names
    KHMER_MONTHS = [
        'មករា', 'កុម្ភៈ', 'មីនា', 'មេសា', 'ឧសភា', 'មិថុនា',
        'កក្កដា', 'សីហា', 'កញ្ញា', 'តុលា', ' វិចិ្ឆកា', ' ធ្នូ',
        'បុណ្យចូលឆ្នាំ', 'មាឃបូជា', 'ពិធីបុណ្យចូលបិណ្យ'
    ]
    
    # Khmer day names
    KHMER_DAYS = ['ច័ន្ទ', 'អង្គារ', 'ពុធ', 'ព្រហស្បតិ៍', 'សុក្រ', 'សៅរ៍', 'អាទិត្យ']
    
    def handle(self, *args, **options):
        self.stdout.write('Populating Khmer lunar calendar...')
        
        # Create public holidays
        self.create_holidays()
        
        # Generate lunar calendar for 2024-2030
        self.generate_lunar_calendar()
        
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
    
    def generate_lunar_calendar(self):
        """Generate lunar calendar data - simplified calculation"""
        # This is a simplified calculation. For accurate Khmer lunar calendar,
        # you would need astronomical calculations or reference data.
        
        years = [2024, 2025, 2026, 2027, 2028, 2029, 2030]
        
        for year in years:
            start_date = date(year, 1, 1)
            end_date = date(year, 12, 31)
            
            current = start_date
            while current <= end_date:
                # Simplified lunar calculation
                # This would need to be replaced with proper astronomical data
                day_of_year = current.timetuple().tm_yday
                
                # Approximate lunar month (simplified)
                lunar_month = ((day_of_year - 1) // 30) % 15 + 1
                if lunar_month > 12:
                    lunar_month = lunar_month - 12
                
                lunar_day = ((day_of_year - 1) % 30) + 1
                lunar_year = year + 543
                
                # Determine day of week
                day_of_week = current.weekday()
                
                # Check if it's a holy day (8th, 15th day of lunar month)
                is_holy = lunar_day in [1, 8, 15]
                is_full_moon = lunar_day == 15
                is_new_moon = lunar_day == 1
                
                LunarDate.objects.update_or_create(
                    solar_date=current,
                    defaults={
                        'lunar_month': lunar_month,
                        'lunar_day': lunar_day,
                        'lunar_year': lunar_year,
                        'khmer_month_name': self.KHMER_MONTHS[lunar_month - 1] if lunar_month <= 12 else self.KHMER_MONTHS[12],
                        'khmer_day_name': self.KHMER_DAYS[day_of_week],
                        'is_holy_day': is_holy,
                        'is_full_moon': is_full_moon,
                        'is_new_moon': is_new_moon,
                    }
                )
                
                current += timedelta(days=1)
        
        self.stdout.write('Generated lunar calendar data')