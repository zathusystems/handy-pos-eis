"""
Management command to initialize the desktop app
Usage: python manage.py init_desktop_app
"""

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import connection
import os

User = get_user_model()


class Command(BaseCommand):
    help = 'Initialize the HandyPOS desktop application'

    def add_arguments(self, parser):
        parser.add_argument(
            '--create-superuser',
            action='store_true',
            help='Create a superuser account',
        )
        parser.add_argument(
            '--email',
            type=str,
            help='Email for superuser',
        )
        parser.add_argument(
            '--password',
            type=str,
            help='Password for superuser',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Initializing HandyPOS Desktop App...'))
        
        # Run migrations
        self.stdout.write('Running migrations...')
        os.system('python manage.py migrate')
        self.stdout.write(self.style.SUCCESS('✓ Migrations completed'))
        
        # Create superuser if requested
        if options['create_superuser']:
            email = options.get('email') or input('Enter superuser email: ')
            password = options.get('password') or input('Enter superuser password: ')
            
            if not User.objects.filter(email=email).exists():
                User.objects.create_superuser(
                    email=email,
                    password=password,
                    displayName='Admin'
                )
                self.stdout.write(self.style.SUCCESS(f'✓ Superuser created: {email}'))
            else:
                self.stdout.write(self.style.WARNING(f'Superuser already exists: {email}'))
        
        # Check database
        self.stdout.write('Checking database...')
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
            self.stdout.write(self.style.SUCCESS('✓ Database connection OK'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'✗ Database error: {e}'))
        
        # Summary
        self.stdout.write(self.style.SUCCESS('\n✓ Desktop app initialization complete!'))
        self.stdout.write('\nNext steps:')
        self.stdout.write('1. Run: npm run dev:all')
        self.stdout.write('2. Open: http://localhost:3000')
        self.stdout.write('3. Backend API: http://127.0.0.1:8000')
