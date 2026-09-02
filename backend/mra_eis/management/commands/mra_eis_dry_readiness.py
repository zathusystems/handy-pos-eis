"""
Run a dry-run readiness suite for MRA EIS certification preparation.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import get_runner
from django.utils import timezone

from business.models import Branch, Business
from inventory.models import InventoryItem, MRAProductMapping as InventoryMRAProductMapping
from mra_eis.models import MRAAPIError, MRAInvoice, SyncRetryQueue, Terminal, TerminalActivationCode
from mra_eis.security import redact_sensitive_data
from mra_eis.services import (
    ConfigurationService,
    InvoiceService,
    ProductMappingService,
    ReceiptService,
    TerminalService,
)


class Command(BaseCommand):
    help = (
        'Run MRA EIS dry-run readiness checks, execute core functional flows, '
        'run mra_eis tests, and write an evidence report.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--output',
            type=str,
            default='',
            help='Optional JSON report path. Defaults to docs/mra-eis/dry-readiness-<timestamp>.json',
        )
        parser.add_argument(
            '--skip-tests',
            action='store_true',
            help='Skip running mra_eis.tests.',
        )

    def handle(self, *args, **options):
        started_at = timezone.now()

        report: dict[str, Any] = {
            'generated_at': started_at.isoformat(),
            'environment': {
                'django_settings_module': settings.SETTINGS_MODULE,
                'database_engine': settings.DATABASES['default']['ENGINE'],
                'mra_eis_mode': settings.MRA_EIS_MODE,
                'mra_eis_dry_run': settings.MRA_EIS_DRY_RUN,
                'mra_eis_allow_live_submission': settings.MRA_EIS_ALLOW_LIVE_SUBMISSION,
                'mra_eis_enable_http_calls': settings.MRA_EIS_ENABLE_HTTP_CALLS,
                'mra_eis_base_url': settings.MRA_EIS_BASE_URL,
                'mra_eis_strict_product_codes': settings.MRA_EIS_STRICT_PRODUCT_CODES,
            },
            'checks': [],
            'functional_flow': {},
            'tests': {},
        }

        checks = report['checks']
        checks.extend(self._build_settings_checks())

        try:
            flow_result = self._run_functional_flow()
            report['functional_flow'] = flow_result
            checks.append(
                self._check(
                    name='functional_dry_run_flow',
                    passed=flow_result.get('status') == 'pass',
                    pass_message='Core onboarding/config/mapping/invoice flows completed in dry-run mode.',
                    fail_message='Core dry-run flow failed. Check functional_flow.error for details.',
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            report['functional_flow'] = {
                'status': 'fail',
                'error': str(exc),
            }
            checks.append(
                self._check(
                    name='functional_dry_run_flow',
                    passed=False,
                    pass_message='Core dry-run flow completed.',
                    fail_message=f'Core dry-run flow failed: {exc}',
                )
            )

        if options.get('skip_tests'):
            report['tests'] = {
                'status': 'skipped',
                'labels': ['mra_eis.tests'],
            }
            checks.append(
                {
                    'name': 'mra_eis_test_suite',
                    'status': 'warn',
                    'message': 'Skipped by --skip-tests.',
                }
            )
        else:
            test_result = self._run_mra_tests()
            report['tests'] = test_result
            checks.append(
                self._check(
                    name='mra_eis_test_suite',
                    passed=test_result.get('status') == 'pass',
                    pass_message='mra_eis.tests passed.',
                    fail_message='mra_eis.tests has failures.',
                )
            )

        summary = self._build_summary(checks)
        report['summary'] = summary
        report['completed_at'] = timezone.now().isoformat()

        output_path = self._write_report(report, options.get('output') or '')
        item2_paths = self._write_item2_artifacts(report, output_path)
        item4_paths = self._write_item4_artifacts(report, output_path)
        item5_paths = self._write_item5_artifacts(report, output_path)
        item6_paths = self._write_item6_artifacts(report, output_path)
        self.stdout.write(self.style.SUCCESS(f'Readiness report written: {output_path}'))
        self.stdout.write(self.style.SUCCESS(f'Item 2 API evidence written: {item2_paths["api_json"]}'))
        self.stdout.write(self.style.SUCCESS(f'Item 2 technical document written: {item2_paths["technical_md"]}'))
        self.stdout.write(self.style.SUCCESS(f'Item 4 evidence written: {item4_paths["json"]}'))
        self.stdout.write(self.style.SUCCESS(f'Item 4 technical document written: {item4_paths["technical_md"]}'))
        self.stdout.write(self.style.SUCCESS(f'Item 5 evidence written: {item5_paths["json"]}'))
        self.stdout.write(self.style.SUCCESS(f'Item 5 technical document written: {item5_paths["technical_md"]}'))
        self.stdout.write(self.style.SUCCESS(f'Item 6 evidence written: {item6_paths["json"]}'))
        self.stdout.write(self.style.SUCCESS(f'Item 6 technical document written: {item6_paths["technical_md"]}'))
        self.stdout.write(
            f"Checks -> pass: {summary['pass']}, warn: {summary['warn']}, fail: {summary['fail']}"
        )

        if summary['fail'] > 0:
            raise CommandError('Dry-run readiness checks failed. See report for details.')

    def _build_settings_checks(self) -> list[dict[str, str]]:
        checks: list[dict[str, str]] = []

        checks.append(
            self._check(
                name='mode_is_test',
                passed=settings.MRA_EIS_MODE == 'TEST',
                pass_message='MRA_EIS_MODE is TEST.',
                fail_message='MRA_EIS_MODE must stay TEST during dry-run certification prep.',
            )
        )
        checks.append(
            self._check(
                name='dry_run_enabled',
                passed=bool(settings.MRA_EIS_DRY_RUN),
                pass_message='MRA_EIS_DRY_RUN is enabled.',
                fail_message='MRA_EIS_DRY_RUN must be True for dry-run certification prep.',
            )
        )
        checks.append(
            self._check(
                name='live_submission_disabled',
                passed=not bool(settings.MRA_EIS_ALLOW_LIVE_SUBMISSION),
                pass_message='Live submission is disabled.',
                fail_message='MRA_EIS_ALLOW_LIVE_SUBMISSION must be False in dry-run prep.',
            )
        )
        checks.append(
            self._check(
                name='http_calls_enabled',
                passed=bool(settings.MRA_EIS_ENABLE_HTTP_CALLS),
                pass_message='HTTP calls are enabled (dry-run still prevents live mutations).',
                fail_message='HTTP calls are disabled, limiting realistic dry-run flow testing.',
                failure_status='warn',
            )
        )
        checks.append(
            self._check(
                name='dev_base_url',
                passed='dev-eis-api' in str(settings.MRA_EIS_BASE_URL).lower(),
                pass_message='MRA_EIS_BASE_URL points to dev endpoint.',
                fail_message='MRA_EIS_BASE_URL is not pointing to dev endpoint.',
                failure_status='warn',
            )
        )
        checks.append(
            self._check(
                name='strict_product_codes_enabled',
                passed=bool(settings.MRA_EIS_STRICT_PRODUCT_CODES),
                pass_message='Strict product code mode is enabled.',
                fail_message='Strict product code mode is disabled. Enable before go-live.',
                failure_status='warn',
            )
        )
        checks.append(
            self._check(
                name='product_id_configured',
                passed=bool((settings.MRA_EIS_PRODUCT_ID or '').strip()),
                pass_message='MRA_EIS_PRODUCT_ID is configured.',
                fail_message='MRA_EIS_PRODUCT_ID must be a non-empty POS product ID.',
                failure_status='warn',
            )
        )
        checks.append(
            self._check(
                name='access_key_present',
                passed=bool((settings.MRA_EIS_ACCESS_KEY or '').strip()),
                pass_message='MRA_EIS_ACCESS_KEY is configured for production TAC activation x-access-key.',
                fail_message='MRA_EIS_ACCESS_KEY is empty. Official production TAC activation requires x-access-key.',
                failure_status='warn',
            )
        )
        checks.append(
            self._check(
                name='secret_key_present',
                passed=bool((settings.MRA_EIS_SECRET_KEY or '').strip()),
                pass_message='Optional fallback MRA_EIS_SECRET_KEY is configured.',
                fail_message='MRA_EIS_SECRET_KEY is empty. The official terminal signing secret should come from TAC activation.',
                failure_status='warn',
            )
        )

        return checks

    def _run_functional_flow(self) -> dict[str, Any]:
        timestamp = timezone.now().strftime('%y%m%d%H%M%S')
        user, business, branch = self._get_or_create_core_entities(timestamp)
        inventory_item, inventory_mapping = self._get_or_create_inventory_mapping(business, branch, timestamp)

        tac = TerminalActivationCode.objects.create(
            business=business,
            code=f'DRY-{timezone.now().strftime("%y%m%d%H%M%S%f")}',
            status='unused',
            expires_at=timezone.now() + timedelta(days=7),
        )

        terminal = TerminalService.activate_terminal(
            business=business,
            branch=branch,
            tac_code=tac.code,
            pos_name='Handy POS',
            pos_version='dry-readiness-1.0',
            os_type='Web',
            device_serial=f'DRY-DEVICE-{timestamp}',
            mac_address='00:1A:2B:3C:4D:5E',
        )

        latest_terminal_audit = terminal.audit_logs.order_by('-created_at').first()
        activation_details = (latest_terminal_audit.details or {}) if latest_terminal_audit else {}
        activation_dry_run = bool(activation_details.get('dry_run'))
        activation_details = self._redact_sensitive(activation_details)

        sync_log = ConfigurationService.fetch_and_store_configuration(business)

        product_sync = ProductMappingService.sync_inventory_mapping_to_mra(
            inventory_mapping,
            terminal=terminal,
        )

        invoice_items = [
            {
                'mra_product_code': inventory_mapping.mra_product_code,
                'name': inventory_item.name,
                'quantity': Decimal('2.000'),
                'unit_price': inventory_item.price or Decimal('1000.00'),
                'tax_rate': inventory_mapping.mra_tax_rate,
                'tax_category': inventory_mapping.mra_tax_type,
            }
        ]

        online_invoice = InvoiceService.create_invoice(
            terminal=terminal,
            seller_tin=business.tin or '0000000000',
            seller_name=business.name,
            items=invoice_items,
            buyer_tin='',
            buyer_name='',
            is_online=True,
        )
        online_invoice = InvoiceService.submit_invoice(online_invoice)
        online_receipt = ReceiptService.generate_receipt(online_invoice)
        online_qr_payload: dict[str, Any] = {}
        try:
            online_qr_payload = json.loads(online_receipt.qr_code_data or '{}')
        except Exception:  # pragma: no cover - defensive
            online_qr_payload = {}
        receipt_qr_code_present = bool(str(online_receipt.qr_code_data or '').strip())
        receipt_qr_signature_present = bool(str(online_qr_payload.get('signature') or '').strip())

        offline_invoice = InvoiceService.create_invoice(
            terminal=terminal,
            seller_tin=business.tin or '0000000000',
            seller_name=business.name,
            items=invoice_items,
            buyer_tin='',
            buyer_name='',
            is_online=False,
        )
        queue_entry = InvoiceService.queue_offline_invoice(offline_invoice)
        offline_sync = InvoiceService.sync_offline_invoices(terminal)
        offline_invoice.refresh_from_db()
        queue_entry.refresh_from_db()

        online_dry_run = bool((online_invoice.mra_response or {}).get('dry_run'))
        offline_dry_run = bool((offline_invoice.mra_response or {}).get('dry_run'))
        online_response = self._redact_sensitive(online_invoice.mra_response or {})
        offline_response = self._redact_sensitive(offline_invoice.mra_response or {})
        online_hash_valid = InvoiceService.verify_invoice_hash(online_invoice)
        offline_hash_valid = InvoiceService.verify_invoice_hash(offline_invoice)

        all_branches = list(Branch.objects.filter(business=business).order_by('created_at'))
        all_terminals = list(
            Terminal.objects.filter(business=business).select_related('branch').order_by('created_at')
        )
        all_mappings = list(
            InventoryMRAProductMapping.objects.filter(inventory_item__business=business)
            .select_related('inventory_item', 'branch')
            .order_by('-created_at')
        )
        inventory_items_total = InventoryItem.objects.filter(business=business).count()
        approved_mappings_count = sum(1 for item in all_mappings if item.is_approved)
        synced_mappings_count = sum(1 for item in all_mappings if item.mra_synced)

        branch_stock_summary = []
        for item_branch in all_branches:
            inventory_count = InventoryItem.objects.filter(business=business, branch=item_branch).count()
            branch_mappings = [
                mapping
                for mapping in all_mappings
                if (mapping.branch_id or mapping.inventory_item.branch_id) == item_branch.id
            ]
            branch_stock_summary.append(
                {
                    'branch_id': str(item_branch.id),
                    'branch_name': item_branch.name,
                    'inventory_items': inventory_count,
                    'mapped_items': len(branch_mappings),
                    'synced_mapped_items': sum(1 for mapping in branch_mappings if mapping.mra_synced),
                }
            )

        status_pass = all(
            [
                activation_dry_run,
                sync_log.status == 'success',
                bool(product_sync.get('dry_run')),
                online_invoice.status == 'submitted',
                online_dry_run,
                queue_entry.status == 'synced',
                offline_invoice.status == 'offline_synced',
                offline_dry_run,
                offline_sync.get('failed', 0) == 0,
                online_hash_valid,
                offline_hash_valid,
            ]
        )

        return {
            'status': 'pass' if status_pass else 'fail',
            'user_id': str(user.id),
            'business_id': str(business.id),
            'branch_id': str(branch.id),
            'terminal_id': str(terminal.id),
            'business_profile': {
                'business_id': str(business.id),
                'business_name': business.name,
                'tin': business.tin,
                'email': business.email,
                'phone': business.phone,
                'vat_registered': bool(business.vat_registered),
                'mra_enrolled': bool(business.mra_enrolled),
                'missing_fields': [
                    field_name
                    for field_name, field_value in [
                        ('tin', business.tin),
                        ('email', business.email),
                        ('phone', business.phone),
                    ]
                    if not (field_value and str(field_value).strip())
                ],
            },
            'activation': {
                'terminal_status': terminal.status,
                'dry_run': activation_dry_run,
                'audit_details': activation_details,
            },
            'configuration_sync': {
                'status': sync_log.status,
                'sync_log_id': str(sync_log.id),
                'config_types': sync_log.config_types,
            },
            'product_sync': {
                'synced': bool(product_sync.get('synced')),
                'dry_run': bool(product_sync.get('dry_run')),
                'endpoint': product_sync.get('endpoint'),
                'response': self._redact_sensitive(product_sync.get('response', {})),
                'mra_product_code': product_sync.get('mra_product_code'),
            },
            'inventory_mapping_snapshot': {
                'total_inventory_items': inventory_items_total,
                'total_mappings': len(all_mappings),
                'approved_mappings': approved_mappings_count,
                'synced_mappings': synced_mappings_count,
                'sample_mappings': [
                    {
                        'inventory_item_id': str(mapping.inventory_item_id),
                        'inventory_item_name': mapping.inventory_item.name if mapping.inventory_item_id else '',
                        'branch_id': str(mapping.branch_id or mapping.inventory_item.branch_id),
                        'mra_product_code': mapping.mra_product_code,
                        'tax_type': mapping.mra_tax_type,
                        'tax_rate': str(mapping.mra_tax_rate),
                        'is_approved': bool(mapping.is_approved),
                        'mra_synced': bool(mapping.mra_synced),
                    }
                    for mapping in all_mappings[:10]
                ],
            },
            'branch_terminal_linkage_snapshot': {
                'branches': [
                    {
                        'branch_id': str(item_branch.id),
                        'branch_name': item_branch.name,
                        'mra_branch_code': item_branch.mra_branch_code,
                        'address': item_branch.address,
                        'city': item_branch.city,
                        'country': item_branch.country,
                    }
                    for item_branch in all_branches
                ],
                'terminals': [
                    {
                        'terminal_id': str(item_terminal.id),
                        'local_terminal_code': item_terminal.terminal_id,
                        'mra_terminal_id': item_terminal.mra_terminal_id,
                        'branch_id': str(item_terminal.branch_id),
                        'branch_name': item_terminal.branch.name,
                        'status': item_terminal.status,
                        'activated_at': (
                            item_terminal.activated_at.isoformat()
                            if item_terminal.activated_at else None
                        ),
                    }
                    for item_terminal in all_terminals
                ],
                'branch_stock_summary': branch_stock_summary,
            },
            'online_invoice': {
                'invoice_id': str(online_invoice.id),
                'invoice_number': int(online_invoice.invoice_number),
                'status': online_invoice.status,
                'dry_run': online_dry_run,
                'mra_invoice_id': online_invoice.mra_invoice_id,
                'receipt_number': online_receipt.receipt_number,
                'receipt_id': str(online_receipt.id),
                'invoice_hash_valid': online_hash_valid,
                'receipt_qr_code_present': receipt_qr_code_present,
                'receipt_qr_signature_present': receipt_qr_signature_present,
                'mra_response': online_response,
            },
            'offline_invoice': {
                'invoice_id': str(offline_invoice.id),
                'invoice_number': int(offline_invoice.invoice_number),
                'status': offline_invoice.status,
                'dry_run': offline_dry_run,
                'queue_status': queue_entry.status,
                'queue_entry_id': str(queue_entry.id),
                'invoice_hash_valid': offline_hash_valid,
                'mra_response': offline_response,
                'sync_summary': offline_sync,
            },
        }

    def _run_mra_tests(self) -> dict[str, Any]:
        test_labels = ['mra_eis.tests']
        try:
            test_runner_class = get_runner(settings)
            test_runner = test_runner_class(verbosity=1, interactive=False, failfast=False)
            failures = int(test_runner.run_tests(test_labels))
            return {
                'status': 'pass' if failures == 0 else 'fail',
                'labels': test_labels,
                'failures': failures,
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {
                'status': 'fail',
                'labels': test_labels,
                'failures': -1,
                'error': str(exc),
            }

    def _get_or_create_core_entities(self, timestamp: str):
        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            email='mra-readiness@handypos.local',
            defaults={'is_active': True},
        )

        business = Business.objects.filter(owner=user, name='MRA Dry Readiness Business').first()
        if business is None:
            business = Business.objects.create(
                owner=user,
                name='MRA Dry Readiness Business',
                business_type='generic',
                tin=f'9{timestamp}',
                vat_registered=True,
                mra_taxpayer_type='VAT',
                mra_enrolled=True,
                mra_enrolled_at=timezone.now(),
                country='Malawi',
            )
        else:
            dirty_fields: list[str] = []
            if not business.tin:
                business.tin = f'9{timestamp}'
                dirty_fields.append('tin')
            if not business.mra_enrolled:
                business.mra_enrolled = True
                dirty_fields.append('mra_enrolled')
            if not business.vat_registered:
                business.vat_registered = True
                dirty_fields.append('vat_registered')
            if business.mra_taxpayer_type != 'VAT':
                business.mra_taxpayer_type = 'VAT'
                dirty_fields.append('mra_taxpayer_type')
            if dirty_fields:
                business.save(update_fields=dirty_fields + ['updated_at'])

        # Always create a dedicated branch for each run so terminal/TAC bindings
        # do not collide with previous readiness evidence runs.
        branch = Branch.objects.create(
            business=business,
            name=f'Dry Readiness Branch {timestamp}',
            address='Readiness Address',
            city='Lilongwe',
            country='Malawi',
        )

        return user, business, branch

    def _get_or_create_inventory_mapping(self, business, branch, timestamp: str):
        inventory_item, _ = InventoryItem.objects.get_or_create(
            business=business,
            branch=branch,
            name='Dry Readiness Product',
            defaults={
                'category': 'Readiness',
                'item_type': 'sellable',
                'stock_units': Decimal('25.000'),
                'unit_type': 'unit',
                'reorder_level': Decimal('2.000'),
                'status': 'In Stock',
                'cost': Decimal('800.00'),
                'price': Decimal('1000.00'),
            },
        )

        mapping, created = InventoryMRAProductMapping.objects.get_or_create(
            inventory_item=inventory_item,
            defaults={
                'branch': branch,
                'mra_product_code': f'DRY-PRODUCT-{timestamp}',
                'mra_product_name': inventory_item.name,
                'mra_tax_type': 'standard',
                'mra_tax_rate': Decimal('16.50'),
                'mra_unit_measure': 'unit',
                'tax_calculation_method': 'inclusive',
                'is_approved': True,
                'approved_at': timezone.now(),
            },
        )

        if not created:
            dirty_fields: list[str] = []
            if mapping.branch_id != branch.id:
                mapping.branch = branch
                dirty_fields.append('branch')
            if mapping.mra_tax_type != 'standard':
                mapping.mra_tax_type = 'standard'
                dirty_fields.append('mra_tax_type')
            if mapping.mra_tax_rate != Decimal('16.50'):
                mapping.mra_tax_rate = Decimal('16.50')
                dirty_fields.append('mra_tax_rate')
            if mapping.tax_calculation_method != 'inclusive':
                mapping.tax_calculation_method = 'inclusive'
                dirty_fields.append('tax_calculation_method')
            if not mapping.is_approved:
                mapping.is_approved = True
                dirty_fields.append('is_approved')
            if mapping.approved_at is None:
                mapping.approved_at = timezone.now()
                dirty_fields.append('approved_at')
            if dirty_fields:
                mapping.save(update_fields=dirty_fields + ['updated_at'])

        return inventory_item, mapping

    @staticmethod
    def _check(
        *,
        name: str,
        passed: bool,
        pass_message: str,
        fail_message: str,
        failure_status: str = 'fail',
    ) -> dict[str, str]:
        return {
            'name': name,
            'status': 'pass' if passed else failure_status,
            'message': pass_message if passed else fail_message,
        }

    @staticmethod
    def _build_summary(checks: list[dict[str, str]]) -> dict[str, int]:
        return {
            'pass': sum(1 for item in checks if item.get('status') == 'pass'),
            'warn': sum(1 for item in checks if item.get('status') == 'warn'),
            'fail': sum(1 for item in checks if item.get('status') == 'fail'),
        }

    def _write_report(self, report: dict[str, Any], output_arg: str) -> Path:
        if output_arg:
            output_path = Path(output_arg).expanduser().resolve()
        else:
            timestamp = timezone.now().strftime('%Y%m%d-%H%M%S')
            output_path = (
                Path(settings.BASE_DIR).parent
                / 'docs'
                / 'mra-eis'
                / f'dry-readiness-{timestamp}.json'
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(report, indent=2, default=self._json_default)
        output_path.write_text(rendered, encoding='utf-8')

        latest_path = output_path.parent / 'dry-readiness-latest.json'
        latest_path.write_text(rendered, encoding='utf-8')
        return output_path

    def _write_item2_artifacts(self, report: dict[str, Any], report_path: Path) -> dict[str, Path]:
        cert_dir = Path(settings.BASE_DIR).parent / 'docs' / 'mra-eis' / 'certification'
        cert_dir.mkdir(parents=True, exist_ok=True)

        flow = report.get('functional_flow') or {}
        activation = flow.get('activation') or {}
        product_sync = flow.get('product_sync') or {}
        online = flow.get('online_invoice') or {}
        offline = flow.get('offline_invoice') or {}

        online_resp = online.get('mra_response') or {}
        offline_resp = offline.get('mra_response') or {}
        activation_audit = activation.get('audit_details') or {}

        api_evidence = {
            'generated_at': report.get('completed_at') or report.get('generated_at'),
            'source_report': str(report_path),
            'environment': report.get('environment', {}),
            'api_input_output_examples': {
                'activate_terminal': {
                    'endpoint': settings.MRA_EIS_ENDPOINTS.get('activate_terminal'),
                    'input_payload': activation_audit.get('request_payload'),
                    'output_payload': activation_audit.get('response'),
                    'dry_run': activation_audit.get('dry_run'),
                },
                'get_latest_config': {
                    'endpoint': settings.MRA_EIS_ENDPOINTS.get('get_latest_config'),
                    'requested_config_types': (flow.get('configuration_sync') or {}).get('config_types', []),
                    'sync_status': (flow.get('configuration_sync') or {}).get('status'),
                },
                'product_status': {
                    'endpoint': product_sync.get('endpoint') or settings.MRA_EIS_ENDPOINTS.get('product_status'),
                    'output_payload': product_sync.get('response'),
                    'dry_run': product_sync.get('dry_run'),
                },
                'report_sale_online': {
                    'endpoint': settings.MRA_EIS_ENDPOINTS.get('report_sale'),
                    'input_payload': online_resp.get('payload'),
                    'output_payload': online_resp.get('response'),
                    'dry_run': online_resp.get('dry_run'),
                },
                'report_sale_offline': {
                    'endpoint': settings.MRA_EIS_ENDPOINTS.get('report_sale_offline'),
                    'input_payload': offline_resp.get('payload'),
                    'output_payload': offline_resp.get('response'),
                    'dry_run': offline_resp.get('dry_run'),
                },
            },
            'error_handling_evidence': {
                'unresolved_mra_api_errors': MRAAPIError.objects.filter(is_resolved=False).count(),
                'pending_retry_jobs': SyncRetryQueue.objects.filter(status='pending').count(),
                'retry_jobs_failed': SyncRetryQueue.objects.filter(status='failed').count(),
            },
            'offline_sync_evidence': {
                'queue_status': offline.get('queue_status'),
                'invoice_status': offline.get('status'),
                'sync_summary': offline.get('sync_summary'),
            },
        }

        api_json_path = cert_dir / 'item-2-api-evidence-latest.json'
        api_json_path.write_text(
            json.dumps(api_evidence, indent=2, default=self._json_default),
            encoding='utf-8',
        )

        technical_md_path = cert_dir / 'item-2-technical-evidence-latest.md'
        technical_md_path.write_text(
            self._build_item2_markdown(report, api_json_path),
            encoding='utf-8',
        )

        return {
            'api_json': api_json_path,
            'technical_md': technical_md_path,
        }

    def _write_item4_artifacts(self, report: dict[str, Any], report_path: Path) -> dict[str, Path]:
        cert_dir = Path(settings.BASE_DIR).parent / 'docs' / 'mra-eis' / 'certification'
        cert_dir.mkdir(parents=True, exist_ok=True)

        flow = report.get('functional_flow') or {}
        business_profile_snapshot = flow.get('business_profile') or {}
        inventory_snapshot = flow.get('inventory_mapping_snapshot') or {}
        linkage_snapshot = flow.get('branch_terminal_linkage_snapshot') or {}

        # Fallback to DB when a snapshot is unavailable.
        if not business_profile_snapshot:
            business_id = flow.get('business_id')
            business = Business.objects.filter(id=business_id).first() if business_id else None
            business_profile_snapshot = {
                'business_id': str(business.id) if business else None,
                'business_name': business.name if business else None,
                'tin': business.tin if business else None,
                'email': business.email if business else None,
                'phone': business.phone if business else None,
                'vat_registered': bool(business.vat_registered) if business else False,
                'mra_enrolled': bool(business.mra_enrolled) if business else False,
                'missing_fields': [
                    field_name
                    for field_name, field_value in [
                        ('tin', business.tin if business else None),
                        ('email', business.email if business else None),
                        ('phone', business.phone if business else None),
                    ]
                    if not (field_value and str(field_value).strip())
                ],
            }

        portal_registration = {
            'developer_portal_registration': 'manual_confirmation_required',
            'taxpayer_portal_registration': 'manual_confirmation_required',
            'notes': (
                'MRA portal registration is external to this codebase and must be '
                'confirmed manually with screenshots or acknowledgment email.'
            ),
        }

        inventory_upload_and_approval = {
            'total_inventory_items': inventory_snapshot.get('total_inventory_items', 0),
            'total_mappings': inventory_snapshot.get('total_mappings', 0),
            'approved_mappings': inventory_snapshot.get('approved_mappings', 0),
            'synced_mappings': inventory_snapshot.get('synced_mappings', 0),
            'sample_mappings': inventory_snapshot.get('sample_mappings', []),
            'integration_points': {
                'mapping_service': 'ProductMappingService.sync_inventory_mapping_to_mra',
                'configuration_service': 'ConfigurationService.fetch_and_store_configuration',
            },
        }

        branch_terminal_linkage = {
            'branches': linkage_snapshot.get('branches', []),
            'terminals': linkage_snapshot.get('terminals', []),
            'branch_stock_summary': linkage_snapshot.get('branch_stock_summary', []),
        }

        activation_and_reporting = {
            'terminal_activation_request_response': (flow.get('activation') or {}).get('audit_details'),
            'post_activation_reporting_evidence': {
                'online_invoice': flow.get('online_invoice'),
                'offline_invoice': flow.get('offline_invoice'),
            },
            'reporting_endpoints': {
                'report_sale': settings.MRA_EIS_ENDPOINTS.get('report_sale'),
                'report_sale_offline': settings.MRA_EIS_ENDPOINTS.get('report_sale_offline'),
            },
        }

        item4_json = {
            'generated_at': report.get('completed_at') or report.get('generated_at'),
            'source_report': str(report_path),
            'pre_integration_preparation': {
                'portal_registration': portal_registration,
                'business_identification': business_profile_snapshot,
                'inventory_upload_and_approval': inventory_upload_and_approval,
                'branch_and_stock_terminal_linkage': branch_terminal_linkage,
                'terminal_activation_and_reporting': activation_and_reporting,
            },
            'manual_actions_required': [
                'Complete MRA Taxpayer/Developer portal registration and keep proof.',
                'Confirm business contact fields (email/phone) are complete for submission forms.',
                'Attach portal screenshots and MRA correspondence to submission dossier.',
            ],
        }
        item4_json = self._redact_sensitive(item4_json)

        item4_json_path = cert_dir / 'item-4-preintegration-evidence-latest.json'
        item4_json_path.write_text(
            json.dumps(item4_json, indent=2, default=self._json_default),
            encoding='utf-8',
        )

        item4_md_path = cert_dir / 'item-4-preintegration-evidence-latest.md'
        item4_md_path.write_text(
            self._build_item4_markdown(item4_json, item4_json_path),
            encoding='utf-8',
        )

        return {
            'json': item4_json_path,
            'technical_md': item4_md_path,
        }

    def _write_item5_artifacts(self, report: dict[str, Any], report_path: Path) -> dict[str, Path]:
        cert_dir = Path(settings.BASE_DIR).parent / 'docs' / 'mra-eis' / 'certification'
        cert_dir.mkdir(parents=True, exist_ok=True)

        flow = report.get('functional_flow') or {}
        activation = flow.get('activation') or {}
        config_sync = flow.get('configuration_sync') or {}
        online = flow.get('online_invoice') or {}
        offline = flow.get('offline_invoice') or {}
        activation_audit = activation.get('audit_details') or {}

        activation_ok = bool(
            activation.get('dry_run')
            and activation_audit.get('request_payload')
            and activation_audit.get('response')
        )
        configuration_ok = bool(
            config_sync.get('status') == 'success'
            and isinstance(config_sync.get('config_types'), list)
            and len(config_sync.get('config_types', [])) > 0
        )
        reporting_ok = bool(
            online.get('status') in {'submitted', 'accepted'}
            and bool(online.get('dry_run'))
            and offline.get('queue_status') in {'queued', 'synced'}
            and offline.get('status') in {'offline_queued', 'offline_synced', 'submitted', 'accepted'}
            and bool(offline.get('dry_run'))
        )

        item5_json = {
            'generated_at': report.get('completed_at') or report.get('generated_at'),
            'source_report': str(report_path),
            'terminal_activation_flow': {
                'environment': report.get('environment', {}),
                'endpoint_map': {
                    'activate_terminal': settings.MRA_EIS_ENDPOINTS.get('activate_terminal'),
                    'get_latest_config': settings.MRA_EIS_ENDPOINTS.get('get_latest_config'),
                    'report_sale': settings.MRA_EIS_ENDPOINTS.get('report_sale'),
                    'report_sale_offline': settings.MRA_EIS_ENDPOINTS.get('report_sale_offline'),
                },
                'terminal_activation_evidence': {
                    'terminal_id': flow.get('terminal_id'),
                    'terminal_status': activation.get('terminal_status'),
                    'dry_run': activation.get('dry_run'),
                    'request_payload': activation_audit.get('request_payload'),
                    'response_payload': activation_audit.get('response'),
                },
                'configuration_pull_evidence': {
                    'status': config_sync.get('status'),
                    'sync_log_id': config_sync.get('sync_log_id'),
                    'config_types': config_sync.get('config_types'),
                },
                'transaction_reporting_evidence': {
                    'real_time_reporting': online,
                    'queued_reporting': offline,
                },
                'certification_assertions': [
                    {
                        'requirement': 'activate_terminal_via_onboarding_api',
                        'status': 'pass' if activation_ok else 'fail',
                        'details': 'Terminal activation request/response captured from onboarding flow.',
                    },
                    {
                        'requirement': 'pull_configuration_after_terminal_approval',
                        'status': 'pass' if configuration_ok else 'fail',
                        'details': 'Configuration sync executed with MRA config types.',
                    },
                    {
                        'requirement': 'report_transactions_realtime_or_queued',
                        'status': 'pass' if reporting_ok else 'fail',
                        'details': 'Online report_sale and offline queued/sync paths executed.',
                    },
                ],
            },
        }
        item5_json = self._redact_sensitive(item5_json)

        item5_json_path = cert_dir / 'item-5-terminal-activation-evidence-latest.json'
        item5_json_path.write_text(
            json.dumps(item5_json, indent=2, default=self._json_default),
            encoding='utf-8',
        )

        item5_md_path = cert_dir / 'item-5-terminal-activation-evidence-latest.md'
        item5_md_path.write_text(
            self._build_item5_markdown(item5_json, item5_json_path),
            encoding='utf-8',
        )

        return {
            'json': item5_json_path,
            'technical_md': item5_md_path,
        }

    def _write_item6_artifacts(self, report: dict[str, Any], report_path: Path) -> dict[str, Path]:
        cert_dir = Path(settings.BASE_DIR).parent / 'docs' / 'mra-eis' / 'certification'
        cert_dir.mkdir(parents=True, exist_ok=True)

        flow = report.get('functional_flow') or {}
        environment = report.get('environment') or {}
        product_sync = flow.get('product_sync') or {}
        inventory_snapshot = flow.get('inventory_mapping_snapshot') or {}
        online = flow.get('online_invoice') or {}
        offline = flow.get('offline_invoice') or {}
        online_payload = ((online.get('mra_response') or {}).get('payload') or {})
        offline_payload = ((offline.get('mra_response') or {}).get('payload') or {})
        offline_summary = offline.get('sync_summary') or {}

        required_invoice_fields = [
            'terminalId',
            'invoiceNumber',
            'sellerTin',
            'sellerName',
            'items',
            'netAmount',
            'taxAmount',
            'grossAmount',
            'invoiceDate',
            'signature',
            'isOffline',
        ]

        def _has_required_fields(payload: dict[str, Any]) -> bool:
            return all(field in payload for field in required_invoice_fields)

        def _is_decimal_like(value: Any) -> bool:
            if value in (None, ''):
                return False
            try:
                Decimal(str(value))
                return True
            except Exception:  # pragma: no cover - defensive
                return False

        payload_format_ok = bool(
            _has_required_fields(online_payload)
            and _has_required_fields(offline_payload)
            and _is_decimal_like(online_payload.get('netAmount'))
            and _is_decimal_like(online_payload.get('taxAmount'))
            and _is_decimal_like(online_payload.get('grossAmount'))
            and _is_decimal_like(offline_payload.get('netAmount'))
            and _is_decimal_like(offline_payload.get('taxAmount'))
            and _is_decimal_like(offline_payload.get('grossAmount'))
            and isinstance(online_payload.get('items'), list)
            and isinstance(offline_payload.get('items'), list)
            and len(online_payload.get('items') or []) > 0
            and len(offline_payload.get('items') or []) > 0
        )

        online_invoice_obj = (
            MRAInvoice.objects.filter(id=online.get('invoice_id')).first()
            if online.get('invoice_id') else None
        )
        offline_invoice_obj = (
            MRAInvoice.objects.filter(id=offline.get('invoice_id')).first()
            if offline.get('invoice_id') else None
        )
        online_hash_revalidated = bool(
            online_invoice_obj and InvoiceService.verify_invoice_hash(online_invoice_obj)
        )
        offline_hash_revalidated = bool(
            offline_invoice_obj and InvoiceService.verify_invoice_hash(offline_invoice_obj)
        )
        flow_online_hash_valid = online.get('invoice_hash_valid')
        flow_offline_hash_valid = offline.get('invoice_hash_valid')
        online_hash_valid = (
            bool(flow_online_hash_valid)
            if isinstance(flow_online_hash_valid, bool)
            else online_hash_revalidated
        )
        offline_hash_valid = (
            bool(flow_offline_hash_valid)
            if isinstance(flow_offline_hash_valid, bool)
            else offline_hash_revalidated
        )
        hash_validation_ok = online_hash_valid and offline_hash_valid
        qr_code_present = bool(online.get('receipt_qr_code_present'))
        qr_signature_present = bool(online.get('receipt_qr_signature_present'))

        security_https_ok = str(environment.get('mra_eis_base_url') or '').startswith('https://')
        terminal_secret_present = Terminal.objects.exclude(mra_api_key='').exists()
        optional_gateway_keys_present = bool(
            (settings.MRA_EIS_ACCESS_KEY or '').strip() and (settings.MRA_EIS_SECRET_KEY or '').strip()
        )
        signature_present = bool(
            str(online_payload.get('signature') or '').strip()
            and str(offline_payload.get('signature') or '').strip()
        )

        inventory_sync_ok = bool(
            product_sync.get('synced')
            and inventory_snapshot.get('synced_mappings', 0) >= 1
            and inventory_snapshot.get('total_mappings', 0) >= inventory_snapshot.get('synced_mappings', 0)
        )

        error_retry_ok = bool(
            offline.get('queue_status') in {'queued', 'synced'}
            and offline.get('status') in {'offline_queued', 'offline_synced', 'submitted', 'accepted'}
            and int(offline_summary.get('failed', 0)) == 0
            and MRAAPIError.objects.filter(is_resolved=False).count() == 0
        )

        assertions: list[dict[str, str]] = [
            {
                'requirement': 'secure_transmission_authentication_encryption',
                'status': 'pass' if (security_https_ok and signature_present) else 'fail',
                'details': 'HTTPS endpoint and signed invoice payloads were detected.',
            },
            {
                'requirement': 'credentials_ready_for_live_authentication',
                'status': 'pass' if terminal_secret_present else 'warn',
                'details': (
                    'Terminal signing secret should be stored after TAC activation. '
                    f'Optional gateway key pair present: {optional_gateway_keys_present}.'
                ),
            },
            {
                'requirement': 'sales_invoice_format_accuracy',
                'status': 'pass' if payload_format_ok else 'fail',
                'details': 'Online and offline payloads include required invoice fields and decimal amount formats.',
            },
            {
                'requirement': 'invoice_hash_validation',
                'status': 'pass' if hash_validation_ok else 'fail',
                'details': 'Online and offline signatures re-validated against canonical invoice payloads.',
            },
            {
                'requirement': 'receipt_qr_code_presence',
                'status': 'pass' if (qr_code_present and qr_signature_present) else 'fail',
                'details': 'Receipt QR payload is generated and includes invoice signature metadata.',
            },
            {
                'requirement': 'inventory_stock_sync_accuracy',
                'status': 'pass' if inventory_sync_ok else 'fail',
                'details': 'Inventory mappings were synced and tracked without mismatch.',
            },
            {
                'requirement': 'graceful_errors_retries_offline',
                'status': 'pass' if error_retry_ok else 'fail',
                'details': 'Offline queued flow synced with zero failed sync attempts and no unresolved API errors.',
            },
        ]

        item6_json = {
            'generated_at': report.get('completed_at') or report.get('generated_at'),
            'source_report': str(report_path),
            'security_authentication_accuracy': {
                'environment': environment,
                'security_controls': {
                    'https_base_url': security_https_ok,
                    'signature_fields_present': signature_present,
                    'access_key_present': bool((settings.MRA_EIS_ACCESS_KEY or '').strip()),
                    'secret_key_present': bool((settings.MRA_EIS_SECRET_KEY or '').strip()),
                    'dry_run_mode': bool(environment.get('mra_eis_dry_run')),
                    'receipt_qr_code_present': qr_code_present,
                    'receipt_qr_signature_present': qr_signature_present,
                },
                'invoice_payload_validation': {
                    'required_fields': required_invoice_fields,
                    'online_has_required_fields': _has_required_fields(online_payload),
                    'offline_has_required_fields': _has_required_fields(offline_payload),
                    'format_assertion_passed': payload_format_ok,
                    'online_invoice_hash_valid': online_hash_valid,
                    'offline_invoice_hash_valid': offline_hash_valid,
                    'online_invoice_hash_revalidated': (
                        online_hash_revalidated if online_invoice_obj else None
                    ),
                    'offline_invoice_hash_revalidated': (
                        offline_hash_revalidated if offline_invoice_obj else None
                    ),
                    'hash_assertion_passed': hash_validation_ok,
                },
                'inventory_sync_validation': {
                    'product_sync': product_sync,
                    'inventory_snapshot': inventory_snapshot,
                    'sync_assertion_passed': inventory_sync_ok,
                },
                'error_retry_offline_validation': {
                    'offline_invoice': offline,
                    'unresolved_mra_api_errors': MRAAPIError.objects.filter(is_resolved=False).count(),
                    'pending_retry_jobs': SyncRetryQueue.objects.filter(status='pending').count(),
                    'failed_retry_jobs': SyncRetryQueue.objects.filter(status='failed').count(),
                    'resilience_assertion_passed': error_retry_ok,
                },
                'certification_assertions': assertions,
            },
            'compliance_notice': (
                'MRA certification can be revoked if production code drifts from compliant behavior. '
                'Re-run this package after every significant release and before go-live.'
            ),
        }
        item6_json = self._redact_sensitive(item6_json)

        item6_json_path = cert_dir / 'item-6-security-auth-accuracy-evidence-latest.json'
        item6_json_path.write_text(
            json.dumps(item6_json, indent=2, default=self._json_default),
            encoding='utf-8',
        )

        item6_md_path = cert_dir / 'item-6-security-auth-accuracy-evidence-latest.md'
        item6_md_path.write_text(
            self._build_item6_markdown(item6_json, item6_json_path),
            encoding='utf-8',
        )

        return {
            'json': item6_json_path,
            'technical_md': item6_md_path,
        }

    def _build_item2_markdown(self, report: dict[str, Any], api_json_path: Path) -> str:
        environment = report.get('environment', {})
        flow = report.get('functional_flow') or {}
        summary = report.get('summary') or {}
        tests = report.get('tests') or {}

        endpoint_lines = '\n'.join(
            f"- `{key}` -> `{value}`"
            for key, value in sorted(settings.MRA_EIS_ENDPOINTS.items())
        )

        generated_at = report.get('completed_at') or report.get('generated_at')

        return (
            "# MRA EIS Certification Package - Item 2\n\n"
            "## Scope\n"
            "Technical documentation and evidence for API usage, security controls, and offline-sync behavior.\n\n"
            f"Generated: `{generated_at}`\n\n"
            "## 1) Technical Documentation of EIS API Integration\n"
            "The backend integration is implemented in:\n"
            "- `backend/mra_eis/services/` (onboarding, config sync, inventory sync, invoice submission, offline sync)\n"
            "- `backend/mra_eis/views.py` and `backend/mra_eis/urls.py` (API exposure)\n"
            "- `backend/mra_eis/models.py` (compliance records, queue, audit and retry entities)\n\n"
            "Configured endpoint map:\n"
            f"{endpoint_lines}\n\n"
            "Execution environment used for this evidence:\n"
            f"- `DJANGO_SETTINGS_MODULE`: `{environment.get('django_settings_module')}`\n"
            f"- `database_engine`: `{environment.get('database_engine')}`\n"
            f"- `MRA_EIS_MODE`: `{environment.get('mra_eis_mode')}`\n"
            f"- `MRA_EIS_DRY_RUN`: `{environment.get('mra_eis_dry_run')}`\n"
            f"- `MRA_EIS_ALLOW_LIVE_SUBMISSION`: `{environment.get('mra_eis_allow_live_submission')}`\n\n"
            "## 2) Evidence of Correct API Usage (Input/Output + Error Handling)\n"
            f"Primary artifact: `{api_json_path}`\n\n"
            "Included evidence:\n"
            "- Terminal activation input/output example\n"
            "- Configuration sync request scope\n"
            "- Inventory mapping sync output\n"
            "- Online sale report payload and response\n"
            "- Offline sale report payload and response\n"
            "- Error queue metrics (unresolved API errors, retry jobs)\n\n"
            "Automated flow result:\n"
            f"- functional flow status: `{flow.get('status')}`\n"
            f"- test suite status: `{tests.get('status')}`\n"
            f"- test labels: `{', '.join(tests.get('labels', []))}`\n"
            f"- summary: pass `{summary.get('pass', 0)}`, warn `{summary.get('warn', 0)}`, fail `{summary.get('fail', 0)}`\n\n"
            "## 3) Security Measures Before Submission\n"
            "Implemented controls:\n"
            "- Request signing (HMAC) via `x-signature` in `MRAEISClient._hmac_sha512_base64`.\n"
            "- Terminal activation stores MRA-returned `secretKey` for request signing.\n"
            "- Production TAC activation `x-access-key` support in `MRAEISClient._build_headers`.\n"
            "- Token-based terminal auth (`Authorization: Bearer`).\n"
            "- Live-mode safeguards in `backend/core/settings.py` (disallow LIVE with dry-run, placeholder Product ID, or disabled submission).\n"
            "- Write-once style audit trail entities (`InvoiceAuditLog`, `TerminalAuditLog`, `OfflineAuditLog`).\n"
            "- Sensitive values are redacted in generated evidence artifacts.\n\n"
            "## 4) Offline Mode and Deferred Sync Handling\n"
            "Offline handling evidence includes:\n"
            "- Offline invoice creation and queueing (`OfflineInvoiceQueue`).\n"
            "- Ordered sync replay (`InvoiceService.sync_offline_invoices`).\n"
            "- Retry and failure metadata on queue entries.\n"
            "- Audit events for queue and sync lifecycle.\n\n"
            "Observed dry-run evidence in this run:\n"
            f"- offline queue status: `{(flow.get('offline_invoice') or {}).get('queue_status')}`\n"
            f"- offline invoice status: `{(flow.get('offline_invoice') or {}).get('status')}`\n"
            f"- sync summary: `{(flow.get('offline_invoice') or {}).get('sync_summary')}`\n\n"
            "## Reproducibility\n"
            "Regenerate this package with:\n"
            "```bash\n"
            "./scripts/mra-eis-dry-readiness.sh\n"
            "```\n"
        )

    def _build_item4_markdown(self, item4_payload: dict[str, Any], item4_json_path: Path) -> str:
        prep = (item4_payload.get('pre_integration_preparation') or {})
        business = prep.get('business_identification') or {}
        inventory = prep.get('inventory_upload_and_approval') or {}
        linkage = prep.get('branch_and_stock_terminal_linkage') or {}
        activation = prep.get('terminal_activation_and_reporting') or {}
        manual_actions = item4_payload.get('manual_actions_required') or []

        return (
            "# MRA EIS Certification Package - Item 4\n\n"
            "## Scope\n"
            "Pre-integration preparation evidence for portal registration, business identity, inventory approval flow, "
            "branch/stock linkage, and terminal activation reporting.\n\n"
            f"Primary artifact: `{item4_json_path}`\n\n"
            "## 1) Portal Registration Readiness\n"
            "- Developer Portal: manual confirmation required\n"
            "- Taxpayer Portal: manual confirmation required\n"
            "- Reason: registration proof is external to this repository and must be attached manually.\n\n"
            "## 2) Business Identification & Requirements\n"
            f"- Business: `{business.get('business_name')}`\n"
            f"- Business ID: `{business.get('business_id')}`\n"
            f"- TIN: `{business.get('tin')}`\n"
            f"- Email: `{business.get('email')}`\n"
            f"- Phone: `{business.get('phone')}`\n"
            f"- VAT Registered: `{business.get('vat_registered')}`\n"
            f"- MRA Enrolled: `{business.get('mra_enrolled')}`\n"
            f"- Missing fields: `{business.get('missing_fields')}`\n\n"
            "## 3) Inventory Upload and Approval Demonstration\n"
            f"- Total mappings: `{inventory.get('total_mappings')}`\n"
            f"- Approved mappings: `{inventory.get('approved_mappings')}`\n"
            f"- Synced mappings: `{inventory.get('synced_mappings')}`\n"
            "- Flow implementation:\n"
            "  - Mapping sync: `ProductMappingService.sync_inventory_mapping_to_mra`\n"
            "  - Config sync: `ConfigurationService.fetch_and_store_configuration`\n"
            "- Sample mapped items are included in the JSON artifact.\n\n"
            "## 4) Branch, Stock, and Terminal Linkage\n"
            f"- Branches defined: `{len(linkage.get('branches') or [])}`\n"
            f"- Terminals linked: `{len(linkage.get('terminals') or [])}`\n"
            "- Per-branch inventory/mapping summary is included in the JSON artifact.\n\n"
            "## 5) Terminal Activation and Reporting to MRA\n"
            "- Terminal activation request/response evidence is included.\n"
            "- Post-activation reporting evidence includes online and offline sale-report flows.\n"
            f"- Reporting endpoints: `{(activation.get('reporting_endpoints') or {})}`\n\n"
            "## Manual Actions Before Submission\n"
            + ''.join(f"- {entry}\n" for entry in manual_actions)
            + "\n## Reproducibility\n"
            "```bash\n"
            "./scripts/mra-eis-dry-readiness.sh\n"
            "```\n"
        )

    def _build_item5_markdown(self, item5_payload: dict[str, Any], item5_json_path: Path) -> str:
        section = (item5_payload.get('terminal_activation_flow') or {})
        activation = section.get('terminal_activation_evidence') or {}
        config_pull = section.get('configuration_pull_evidence') or {}
        reporting = section.get('transaction_reporting_evidence') or {}
        assertions = section.get('certification_assertions') or []
        endpoint_map = section.get('endpoint_map') or {}

        overall = 'pass' if all(item.get('status') == 'pass' for item in assertions) else 'fail'

        return (
            "# MRA EIS Certification Package - Item 5\n\n"
            "## Scope\n"
            "Terminal activation flow evidence for onboarding activation, post-approval configuration pull, and "
            "transaction reporting (real-time and queued).\n\n"
            f"Primary artifact: `{item5_json_path}`\n\n"
            f"Overall assertion status: `{overall}`\n\n"
            "## 1) Activate Terminals via Onboarding API\n"
            f"- Endpoint: `{endpoint_map.get('activate_terminal')}`\n"
            f"- Terminal ID: `{activation.get('terminal_id')}`\n"
            f"- Terminal status: `{activation.get('terminal_status')}`\n"
            f"- Dry run: `{activation.get('dry_run')}`\n"
            "- Full request/response payload evidence is in JSON artifact.\n\n"
            "## 2) Pull Configuration from MRA After Approval\n"
            f"- Endpoint: `{endpoint_map.get('get_latest_config')}`\n"
            f"- Sync status: `{config_pull.get('status')}`\n"
            f"- Sync log id: `{config_pull.get('sync_log_id')}`\n"
            f"- Config types: `{config_pull.get('config_types')}`\n\n"
            "## 3) Report Transaction Data (Real-time and Queued)\n"
            f"- Real-time endpoint: `{endpoint_map.get('report_sale')}`\n"
            f"- Queued endpoint: `{endpoint_map.get('report_sale_offline')}`\n"
            f"- Real-time invoice status: `{(reporting.get('real_time_reporting') or {}).get('status')}`\n"
            f"- Queued invoice status: `{(reporting.get('queued_reporting') or {}).get('status')}`\n"
            f"- Queue status: `{(reporting.get('queued_reporting') or {}).get('queue_status')}`\n\n"
            "## Certification Assertions\n"
            + ''.join(
                f"- `{item.get('requirement')}`: `{item.get('status')}` ({item.get('details')})\n"
                for item in assertions
            )
            + "\n## Reproducibility\n"
            "```bash\n"
            "./scripts/mra-eis-dry-readiness.sh\n"
            "```\n"
        )

    def _build_item6_markdown(self, item6_payload: dict[str, Any], item6_json_path: Path) -> str:
        section = (item6_payload.get('security_authentication_accuracy') or {})
        controls = section.get('security_controls') or {}
        invoice_validation = section.get('invoice_payload_validation') or {}
        inventory_validation = section.get('inventory_sync_validation') or {}
        resilience_validation = section.get('error_retry_offline_validation') or {}
        assertions = section.get('certification_assertions') or []
        notice = item6_payload.get('compliance_notice')

        total_pass = sum(1 for item in assertions if item.get('status') == 'pass')
        total_warn = sum(1 for item in assertions if item.get('status') == 'warn')
        total_fail = sum(1 for item in assertions if item.get('status') == 'fail')

        return (
            "# MRA EIS Certification Package - Item 6\n\n"
            "## Scope\n"
            "Security, authentication, payload accuracy, inventory sync correctness, and resilience validation.\n\n"
            f"Primary artifact: `{item6_json_path}`\n\n"
            f"Assertion summary: pass `{total_pass}`, warn `{total_warn}`, fail `{total_fail}`\n\n"
            "## 1) Secure Transmission and Authentication\n"
            f"- HTTPS base URL: `{controls.get('https_base_url')}`\n"
            f"- Signature fields present in invoice payloads: `{controls.get('signature_fields_present')}`\n"
            f"- Access key present: `{controls.get('access_key_present')}`\n"
            f"- Secret key present: `{controls.get('secret_key_present')}`\n"
            f"- Dry run mode: `{controls.get('dry_run_mode')}`\n\n"
            "## 2) Sales and Invoice Format Accuracy\n"
            f"- Online required fields present: `{invoice_validation.get('online_has_required_fields')}`\n"
            f"- Offline required fields present: `{invoice_validation.get('offline_has_required_fields')}`\n"
            f"- Format assertion passed: `{invoice_validation.get('format_assertion_passed')}`\n\n"
            "## 2b) Invoice Hash Validation\n"
            f"- Online hash valid: `{invoice_validation.get('online_invoice_hash_valid')}`\n"
            f"- Offline hash valid: `{invoice_validation.get('offline_invoice_hash_valid')}`\n"
            f"- Hash assertion passed: `{invoice_validation.get('hash_assertion_passed')}`\n\n"
            "## 3) Inventory and Stock Sync Correctness\n"
            f"- Sync assertion passed: `{inventory_validation.get('sync_assertion_passed')}`\n"
            f"- Synced mappings: `{(inventory_validation.get('inventory_snapshot') or {}).get('synced_mappings')}`\n"
            f"- Total mappings: `{(inventory_validation.get('inventory_snapshot') or {}).get('total_mappings')}`\n\n"
            "## 4) Errors, Retries, and Offline Resilience\n"
            f"- Resilience assertion passed: `{resilience_validation.get('resilience_assertion_passed')}`\n"
            f"- Unresolved API errors: `{resilience_validation.get('unresolved_mra_api_errors')}`\n"
            f"- Pending retry jobs: `{resilience_validation.get('pending_retry_jobs')}`\n"
            f"- Failed retry jobs: `{resilience_validation.get('failed_retry_jobs')}`\n\n"
            "## Certification Assertions\n"
            + ''.join(
                f"- `{item.get('requirement')}`: `{item.get('status')}` ({item.get('details')})\n"
                for item in assertions
            )
            + f"\n## Compliance Notice\n{notice}\n\n"
            + "## Reproducibility\n"
            "```bash\n"
            "./scripts/mra-eis-dry-readiness.sh\n"
            "```\n"
        )

    def _redact_sensitive(self, value: Any):
        return redact_sensitive_data(value)

    @staticmethod
    def _json_default(value: Any):
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        return str(value)
