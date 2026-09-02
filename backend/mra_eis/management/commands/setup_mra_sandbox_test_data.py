"""
Management command to set up MRA EIS sandbox test data
Usage: python manage.py setup_mra_sandbox_test_data
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import uuid

from business.models import Business, Branch
from inventory.models import InventoryItem, MRAProductMapping
from mra_eis.models import (
    TerminalActivationCode,
    Terminal,
)


class Command(BaseCommand):
    help = 'Set up MRA EIS sandbox test data'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clean',
            action='store_true',
            help='Clean up existing test data before creating new data',
        )

    def handle(self, *args, **options):
        if options['clean']:
            self.clean_test_data()

        self.stdout.write(self.style.SUCCESS('Setting up MRA EIS sandbox test data...'))

        # Create test business
        business = self.create_test_business()
        self.stdout.write(f'✓ Created test business: {business.id}')

        # Create test branch
        branch = self.create_test_branch(business)
        self.stdout.write(f'✓ Created test branch: {branch.id}')

        # Create TAC
        tac = self.create_test_tac(business)
        self.stdout.write(f'✓ Created TAC: {tac.code}')

        # Create terminal
        terminal = self.create_test_terminal(business, branch, tac)
        self.stdout.write(f'✓ Created terminal: {terminal.terminal_id}')

        # Create test products
        products = self.create_test_products(branch)
        self.stdout.write(f'✓ Created {len(products)} test products')

        # Create MRA mappings
        mappings = self.create_mra_mappings(business, products)
        self.stdout.write(f'✓ Created {len(mappings)} MRA mappings')

        # Print summary
        self.print_summary(business, branch, tac, terminal, products)

    def clean_test_data(self):
        """Clean up existing test data"""
        self.stdout.write('Cleaning up existing test data...')

        from mra_eis.models import (
            Terminal, TerminalActivationCode, MRAInvoice,
            OfflineInvoiceQueue, InvoiceAuditLog, Receipt,
        )

        Terminal.objects.filter(terminal_id__startswith='TERM-TEST').delete()
        TerminalActivationCode.objects.filter(code__startswith='TAC-TEST').delete()
        MRAInvoice.objects.filter(seller_tin='123456789').delete()
        OfflineInvoiceQueue.objects.all().delete()
        InvoiceAuditLog.objects.all().delete()
        Receipt.objects.all().delete()
        MRAProductMapping.objects.filter(mra_product_code__startswith='MRA-PROD-TEST').delete()

        self.stdout.write(self.style.SUCCESS('✓ Test data cleaned up'))

    def create_test_business(self):
        """Create test business"""
        business, created = Business.objects.get_or_create(
            name='MRA Sandbox Test Business',
            defaults={
                'tin': '123456789',
                'vat_registered': True,
                'mra_enrolled': True,
                'enable_eis': True,
                'eis_environment': 'TEST',
                'block_sales_if_eis_down': True,
            }
        )
        return business

    def create_test_branch(self, business):
        """Create test branch"""
        branch, created = Branch.objects.get_or_create(
            business=business,
            name='Test Branch',
            defaults={
                'address': '123 Test Street, Test City',
            }
        )
        return branch

    def create_test_tac(self, business):
        """Create Terminal Activation Code"""
        tac, created = TerminalActivationCode.objects.get_or_create(
            code='TAC-TEST-001',
            defaults={
                'business': business,
                'status': 'unused',
                'expires_at': timezone.now() + timedelta(days=30),
            }
        )
        return tac

    def create_test_terminal(self, business, branch, tac):
        """Create test terminal"""
        terminal, created = Terminal.objects.get_or_create(
            business=business,
            branch=branch,
            defaults={
                'terminal_id': 'TERM-TEST-001',
                'device_serial': 'HANDY-WEB-TEST-001',
                'mac_address': '00:11:22:33:44:55',
                'pos_name': 'Handy-POS',
                'pos_version': '1.0.0',
                'os_type': 'Web',
                'mra_terminal_id': 'MRA-TERM-TEST-001',
                'mra_api_key': 'sandbox-api-key-test',
                'status': 'active',
                'is_online': True,
                'activated_at': timezone.now(),
            }
        )

        # Mark TAC as used if terminal was just created
        if created and tac.status == 'unused':
            tac.mark_as_used(terminal)

        return terminal

    def create_test_products(self, branch):
        """Create test products"""
        products = []

        product_data = [
            {
                'name': 'Test Product - Standard',
                'category': 'Test',
                'price': 100.00,
                'stock': 100,
            },
            {
                'name': 'Test Product - Zero Rated',
                'category': 'Test',
                'price': 50.00,
                'stock': 50,
            },
            {
                'name': 'Test Product - Exempt',
                'category': 'Test',
                'price': 75.00,
                'stock': 75,
            },
        ]

        for i, data in enumerate(product_data):
            product, created = InventoryItem.objects.get_or_create(
                business=branch.business,
                branch=branch,
                name=data['name'],
                defaults={
                    'category': data['category'],
                    'item_type': 'sellable',
                    'price': Decimal(str(data['price'])),
                    'stock_units': Decimal(str(data['stock'])),
                    'unit_type': 'unit',
                    'is_produced': False,
                    'status': 'In Stock',
                }
            )
            products.append(product)

        return products

    def create_mra_mappings(self, business, products):
        """Create MRA product mappings"""
        mappings = []

        tax_categories = [
            ('standard', 16.50),
            ('zero', 0.00),
            ('exempt', 0.00),
        ]

        for i, product in enumerate(products):
            tax_category, tax_rate = tax_categories[i]

            mapping, created = MRAProductMapping.objects.update_or_create(
                inventory_item=product,
                defaults={
                    'branch': product.branch,
                    'mra_product_code': f'MRA-PROD-TEST-{i+1:03d}',
                    'mra_product_name': product.name,
                    'mra_tax_type': tax_category,
                    'mra_tax_rate': Decimal(str(tax_rate)),
                    'mra_unit_measure': 'unit',
                    'tax_calculation_method': 'inclusive',
                    'is_approved': True,
                    'approved_at': timezone.now(),
                    'mra_synced': True,
                    'last_synced_at': timezone.now(),
                }
            )
            mappings.append(mapping)

        return mappings

    def print_summary(self, business, branch, tac, terminal, products):
        """Print test data summary"""
        self.stdout.write('\n' + '='*60)
        self.stdout.write(self.style.SUCCESS('MRA EIS SANDBOX TEST DATA CREATED'))
        self.stdout.write('='*60)

        self.stdout.write('\nBusiness:')
        self.stdout.write(f'  ID: {business.id}')
        self.stdout.write(f'  Name: {business.name}')
        self.stdout.write(f'  TIN: {business.tin}')
        self.stdout.write(f'  EIS Enabled: {business.enable_eis}')
        self.stdout.write(f'  Environment: {business.eis_environment}')

        self.stdout.write('\nBranch:')
        self.stdout.write(f'  ID: {branch.id}')
        self.stdout.write(f'  Name: {branch.name}')
        self.stdout.write(f'  Address: {branch.address}')

        self.stdout.write('\nTerminal Activation Code:')
        self.stdout.write(f'  Code: {tac.code}')
        self.stdout.write(f'  Status: {tac.status}')
        self.stdout.write(f'  Expires: {tac.expires_at}')

        self.stdout.write('\nTerminal:')
        self.stdout.write(f'  ID: {terminal.id}')
        self.stdout.write(f'  Terminal ID: {terminal.terminal_id}')
        self.stdout.write(f'  Status: {terminal.status}')
        self.stdout.write(f'  Online: {terminal.is_online}')
        self.stdout.write(f'  Device Serial: {terminal.device_serial}')
        self.stdout.write(f'  POS: {terminal.pos_name} v{terminal.pos_version}')

        self.stdout.write('\nProducts:')
        for product in products:
            self.stdout.write(f'  - {product.name} (${product.price})')

        self.stdout.write('\n' + '='*60)
        self.stdout.write(self.style.SUCCESS('NEXT STEPS:'))
        self.stdout.write('='*60)
        self.stdout.write('1. Go to Settings → MRA EIS')
        self.stdout.write('2. Enable EIS Integration')
        self.stdout.write('3. Enter TIN: 123456789')
        self.stdout.write('4. Save Settings')
        self.stdout.write('5. Go to Terminal Activation')
        self.stdout.write('6. Enter TAC: TAC-TEST-001')
        self.stdout.write('7. Activate Terminal')
        self.stdout.write('8. Go to POS and create a test sale')
        self.stdout.write('='*60 + '\n')
