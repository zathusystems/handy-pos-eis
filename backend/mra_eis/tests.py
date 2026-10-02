"""
MRA EIS Integration Tests
"""
from django.test import TestCase, TransactionTestCase, override_settings
from django.db import connection
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.conf import settings
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
import base64
import hashlib
import hmac
import json
import requests
import uuid
from unittest.mock import patch
from rest_framework.test import APIRequestFactory, force_authenticate

from business.models import Business, Branch, BusinessSettings
from inventory.models import InventoryItem, MRAProductMapping, PurchaseOrder, PurchaseOrderItem, Supplier
from mra_eis.models import (
    Terminal, TerminalActivationCode, FiscalInvoiceSequence, MRAConfiguration,
    MRAInvoice, OfflineInvoiceQueue, Receipt, InvoiceAuditLog,
    OfflineAuditLog, TerminalAuditLog, MRAAPIError, SyncRetryQueue
)
from mra_eis.services import (
    TerminalService, ConfigurationService, ProductMappingService,
    CorrectionService, InvoiceService, ReceiptService, RetryService,
    POSOrderSubmissionService, TransactionReconciliationService,
    MRAIntegrationError, MRACallResult, ReceiptLookupService, StockReceivingService,
    SupplierSyncService,
    extract_mra_response_errors,
)
from mra_eis.services.client import MRAEISClient
from mra_eis.views import TerminalViewSet
from pos_sessions.models import CreditNote, DebitNote, Order, OrderItem, VoidTransaction

User = get_user_model()


class TerminalCredentialSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='eis-credential-security@example.com',
            password='test123',
        )
        self.business = Business.objects.create(owner=self.user, name='Credential Security Business')
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main',
            address='123 Main St',
            city='Lilongwe',
            country='Malawi',
        )

    def test_terminal_credentials_are_encrypted_at_rest(self):
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-CREDENTIAL-001',
            device_serial='DEVICE-CREDENTIAL-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-CREDENTIAL-001',
            mra_api_key='terminal-api-secret',
            mra_token='terminal-jwt-token',
        )

        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT mra_api_key, mra_token FROM mra_eis_terminal WHERE terminal_id = %s',
                [terminal.terminal_id],
            )
            stored_api_key, stored_token = cursor.fetchone()

        self.assertNotIn('terminal-api-secret', stored_api_key)
        self.assertNotIn('terminal-jwt-token', stored_token)
        self.assertTrue(stored_api_key.startswith('mra-eis:v1:'))
        self.assertTrue(stored_token.startswith('mra-eis:v1:'))

        terminal.refresh_from_db()
        self.assertEqual(terminal.mra_api_key, 'terminal-api-secret')
        self.assertEqual(terminal.mra_token, 'terminal-jwt-token')

    def test_audits_and_api_errors_redact_credentials(self):
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-CREDENTIAL-002',
            device_serial='DEVICE-CREDENTIAL-002',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-CREDENTIAL-002',
            mra_api_key='terminal-api-secret',
        )
        audit = TerminalAuditLog.objects.create(
            terminal=terminal,
            action='activated',
            details={
                'response': {'jwtToken': 'jwt-secret', 'secretKey': 'api-secret'},
                'request_payload': {'tacCode': 'TAC-SECRET'},
            },
        )
        error = MRAAPIError.objects.create(
            terminal=terminal,
            error_type='invalid_request',
            error_message='authorization=Bearer super-secret token=jwt-secret',
        )

        self.assertEqual(audit.details['response']['jwtToken'], '[redacted]')
        self.assertEqual(audit.details['response']['secretKey'], '[redacted]')
        self.assertEqual(audit.details['request_payload']['tacCode'], '[redacted]')
        self.assertNotIn('super-secret', error.error_message)
        self.assertNotIn('jwt-secret', error.error_message)

    def test_fiscal_response_snapshots_redact_credentials(self):
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-CREDENTIAL-003',
            device_serial='DEVICE-CREDENTIAL-003',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-CREDENTIAL-003',
            mra_api_key='terminal-api-secret',
        )
        invoice = MRAInvoice.objects.create(
            business=self.business,
            branch=self.branch,
            terminal=terminal,
            invoice_number=1,
            seller_tin='1234567890',
            seller_name=self.business.name,
            items=[],
            net_amount=Decimal('0.00'),
            tax_amount=Decimal('0.00'),
            gross_amount=Decimal('0.00'),
            invoice_date=timezone.now(),
            mra_response={'response': {'accessToken': 'response-token'}},
        )

        self.assertEqual(invoice.mra_response['response']['accessToken'], '[redacted]')


class SupplierSyncServiceTests(TestCase):
    """MRA supplier list sync for goods receiving."""

    def setUp(self):
        self.user = User.objects.create_user(email='suppliers@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Supplier Business', tin='70267581')
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main Branch',
            address='123 Main St',
            city='Blantyre',
            country='Malawi',
        )
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-SUPPLIER-001',
            device_serial='DEVICE-SUPPLIER-001',
            mac_address='00-00-00-00-00-00',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-SUPPLIER-001',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sync_from_mra_creates_local_suppliers_with_eis_ids(self, mock_call):
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
            data={
                'data': [
                    {
                        'supplierId': 42,
                        'supplierTin': 'SUP123',
                        'supplierName': 'Test Supplier',
                        'supplierContactPerson': 'Jane Supplier',
                        'supplierContactEmail': 'supplier@example.com',
                        'supplierContactPhone': '0999999999',
                        'supplierAddress': '1 EIS Road',
                        'cityPlaceOfBusiness': 'Blantyre',
                        'regionState': 'Southern Region',
                        'country': 'Malawi',
                    }
                ]
            },
        )

        result = SupplierSyncService.sync_from_mra(
            business=self.business,
            terminal=self.terminal,
        )

        self.assertEqual(result['fetched'], 1)
        self.assertEqual(result['created'], 1)
        supplier = Supplier.objects.get(business=self.business, mra_supplier_id=42)
        self.assertEqual(supplier.name, 'Test Supplier')
        self.assertEqual(supplier.supplier_tin, 'SUP123')
        self.assertEqual(supplier.contact_person, 'Jane Supplier')
        self.assertEqual(supplier.email, 'supplier@example.com')
        self.assertEqual(supplier.phone, '0999999999')
        self.assertEqual(supplier.address, '1 EIS Road')
        self.assertEqual(supplier.city, 'Blantyre')
        self.assertEqual(supplier.region, 'Southern Region')
        self.assertEqual(supplier.country, 'Malawi')
        mock_call.assert_called_once()
        self.assertEqual(mock_call.call_args.args[0], 'get_suppliers')

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sync_from_mra_matches_existing_supplier_by_tin(self, mock_call):
        existing = Supplier.objects.create(
            business=self.business,
            name='Old Local Name',
            supplier_tin='SUP123',
        )
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
            data={'data': [{'supplierId': 77, 'supplierTin': 'SUP123', 'supplierName': 'Official Supplier'}]},
        )

        result = SupplierSyncService.sync_from_mra(
            business=self.business,
            terminal=self.terminal,
        )

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['updated'], 1)
        existing.refresh_from_db()
        self.assertEqual(existing.mra_supplier_id, 77)
        self.assertEqual(existing.name, 'Official Supplier')
        self.assertEqual(Supplier.objects.filter(business=self.business).count(), 1)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sync_from_mra_falls_back_to_get_when_post_is_not_allowed(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_suppliers' and kwargs.get('method') == 'POST':
                raise MRAIntegrationError(
                    '405 Method Not Allowed',
                    status_code=405,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
                    endpoint_key='get_suppliers',
                )
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
                data={'data': [{'supplierId': 88, 'supplierTin': 'GET123', 'supplierName': 'GET Supplier'}]},
            )

        mock_call.side_effect = fake_call

        result = SupplierSyncService.sync_from_mra(
            business=self.business,
            terminal=self.terminal,
        )

        self.assertEqual(result['fetched'], 1)
        self.assertEqual(result['created'], 1)
        self.assertEqual([call[2].get('method') for call in calls], ['POST', 'GET'])
        supplier = Supplier.objects.get(business=self.business, mra_supplier_id=88)
        self.assertEqual(supplier.name, 'GET Supplier')
        self.assertEqual(supplier.supplier_tin, 'GET123')


class MRAResponseParsingTests(TestCase):
    """MRA response parsing helpers."""

    def test_success_remark_is_not_treated_as_error(self):
        errors = extract_mra_response_errors(
            {'statusCode': 1, 'remark': 'Success', 'data': {'validationURL': 'https://validate'}}
        )

        self.assertEqual(errors, [])

    def test_failure_remark_is_treated_as_error(self):
        errors = extract_mra_response_errors(
            {'statusCode': -2, 'remark': 'TIN not found', 'data': None, 'errors': []}
        )

        self.assertEqual(errors, ['TIN not found'])

    def test_http_failure_remark_is_treated_as_error(self):
        errors = extract_mra_response_errors(
            {'httpStatusCode': 400, 'remark': 'TIN not found', 'data': None, 'errors': []}
        )

        self.assertEqual(errors, ['TIN not found'])

    def test_http_failure_raw_body_is_treated_as_error(self):
        errors = extract_mra_response_errors(
            {'httpStatusCode': 500, 'raw': 'Failed to send a transaction'}
        )

        self.assertEqual(errors, ['Failed to send a transaction'])


class StockReceivingServiceTests(TestCase):
    """EIS stock submission payloads for inventory receive-stock."""

    def setUp(self):
        StockReceivingService._adjustment_reason_cache.clear()
        self.user = User.objects.create_user(email='stock@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Stock Business', tin='70267581')
        BusinessSettings.objects.create(business=self.business, enable_eis=True)
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main Branch',
            address='123 Main St',
            city='Blantyre',
            country='Malawi',
        )
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-STOCK-001',
            device_serial='DEVICE-STOCK-001',
            mac_address='00-00-00-00-00-00',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-STOCK-001',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )
        self.supplier = Supplier.objects.create(
            business=self.business,
            name='Test Supplier',
            supplier_tin='SUP123',
            vat_registered=False,
        )
        self.inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Amazon Big Candy',
            category='Candy',
            item_type='sellable',
            stock_units=Decimal('0'),
            unit_type='unit',
            cost=Decimal('100.00'),
            price=Decimal('150.00'),
            barcode='2934309406073',
        )
        self.mapping = MRAProductMapping.objects.create(
            inventory_item=self.inventory_item,
            branch=self.branch,
            mra_product_code='2934309406073',
            mra_product_name='Amazon Big Candy',
            mra_tax_type='zero',
            mra_tax_rate=Decimal('0.00'),
            mra_unit_measure='unit',
            tax_calculation_method='exclusive',
            is_approved=True,
            approved_at=timezone.now(),
            mra_synced=True,
            last_synced_at=timezone.now(),
        )
        self.purchase_order = PurchaseOrder.objects.create(
            business=self.business,
            branch=self.branch,
            supplier=self.supplier,
            order_number=uuid.uuid4(),
            status='Received',
            total_items=1,
            total_cost=Decimal('300.00'),
            payment_status='Paid',
            amount_paid=Decimal('300.00'),
            amount_due=Decimal('0.00'),
            reference_number='DN-001',
            supplier_tin='SUP123',
            supplier_vat_registered=False,
            created_by='Cashier',
            received_date=timezone.now(),
        )
        self.purchase_item = PurchaseOrderItem.objects.create(
            purchase_order=self.purchase_order,
            inventory_item=self.inventory_item,
            quantity_ordered=Decimal('3.000'),
            quantity_received=Decimal('3.000'),
            quantity_remaining=Decimal('3.000'),
            cost_per_unit=Decimal('100.00'),
            total_cost=Decimal('300.00'),
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_RECEIVE_STOCK_USE_GOODS_RECEIVING=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_receive_stock_uses_goods_receiving_when_supplier_matches_eis(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_suppliers':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
                    data={'data': [{'supplierId': 42, 'supplierTin': 'SUP123', 'supplierName': 'Test Supplier'}]},
                )
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='https://dev-eis-api.mra.mw/api/v1/stock/submit-informal-purchase',
                data={'statusCode': 1, 'remark': 'Success', 'data': None, 'errors': []},
            )

        mock_call.side_effect = fake_call

        result = StockReceivingService.submit_purchase_item_receipt(self.purchase_item, Decimal('3.000'))

        self.assertTrue(result['submitted'])
        self.assertEqual(calls[1][0], 'submit_informal_purchase')
        payload = calls[1][1]
        self.assertEqual(payload['supplierId'], 42)
        self.assertEqual(payload['totalItems'], 1)
        self.assertEqual(payload['items'][0]['itemCode'], '2934309406073')
        self.assertEqual(payload['items'][0]['quantityReceived'], 3.0)
        self.assertEqual(payload['items'][0]['unitPrice'], 100.0)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_RECEIVE_STOCK_USE_GOODS_RECEIVING=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_receive_stock_supplier_lookup_falls_back_to_get_when_post_is_not_allowed(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_suppliers' and kwargs.get('method') == 'POST':
                raise MRAIntegrationError(
                    '405 Method Not Allowed',
                    status_code=405,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
                    endpoint_key='get_suppliers',
                )
            if endpoint_key == 'get_suppliers':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
                    data={'data': [{'supplierId': 42, 'supplierTin': 'SUP123', 'supplierName': 'Test Supplier'}]},
                )
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='https://dev-eis-api.mra.mw/api/v1/stock/submit-informal-purchase',
                data={'statusCode': 1, 'remark': 'Success', 'data': None, 'errors': []},
            )

        mock_call.side_effect = fake_call

        result = StockReceivingService.submit_purchase_item_receipt(self.purchase_item, Decimal('3.000'))

        self.assertTrue(result['submitted'])
        self.assertEqual([call[0] for call in calls], ['get_suppliers', 'get_suppliers', 'submit_informal_purchase'])
        self.assertEqual([calls[0][2].get('method'), calls[1][2].get('method')], ['POST', 'GET'])
        self.assertIsNone(calls[1][1])
        payload = calls[2][1]
        self.assertEqual(payload['supplierId'], 42)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_RECEIVE_STOCK_USE_GOODS_RECEIVING=False,
    )
    @patch.object(MRAEISClient, 'call')
    def test_receive_stock_does_not_fallback_to_adjustment_when_goods_receiving_disabled(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_suppliers':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
                    data={'data': [{'supplierId': 42, 'supplierTin': 'SUP123', 'supplierName': 'Test Supplier'}]},
                )
            self.fail(f'Purchase receipt should not call {endpoint_key} when goods receiving is disabled')

        mock_call.side_effect = fake_call

        result = StockReceivingService.submit_purchase_item_receipt(self.purchase_item, Decimal('3.000'))

        self.assertFalse(result['submitted'])
        self.assertEqual(result['reason'], 'goods_receiving_disabled')
        self.assertEqual(result['endpoint_key'], 'submit_informal_purchase')
        self.assertEqual(calls, [])

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_RECEIVE_STOCK_USE_GOODS_RECEIVING=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_receive_stock_b2b_transfer_source_records_batch_only(self, mock_call):
        self.purchase_order.eis_stock_receipt_source = PurchaseOrder.EIS_STOCK_RECEIPT_SOURCE_SUPPLIER
        self.purchase_order.save(update_fields=['eis_stock_receipt_source'])

        result = StockReceivingService.submit_purchase_item_receipt(self.purchase_item, Decimal('3.000'))

        self.assertFalse(result['submitted'])
        self.assertTrue(result['skipped'])
        self.assertEqual(result['reason'], 'already_posted_by_b2b_transfer')
        mock_call.assert_not_called()

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_RECEIVE_STOCK_USE_GOODS_RECEIVING=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_receive_stock_rejects_informal_purchase_quantity_below_mra_minimum(self, mock_call):
        result = StockReceivingService.submit_purchase_item_receipt(self.purchase_item, Decimal('0.500'))

        self.assertFalse(result['submitted'])
        self.assertEqual(result['reason'], 'invalid_informal_purchase_quantity')
        self.assertEqual(result['endpoint_key'], 'submit_informal_purchase')
        self.assertTrue(result['requires_action'])
        self.assertIn('quantity must be at least 1', result['error'])
        mock_call.assert_not_called()
        retry = SyncRetryQueue.objects.get(operation_type='submit_purchase_item_receipt')
        self.assertEqual(retry.payload['purchase_item_id'], str(self.purchase_item.id))
        self.assertEqual(retry.payload['quantity'], '0.500')
        self.assertTrue(MRAAPIError.objects.filter(error_code='invalid_informal_purchase_quantity').exists())

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_RECEIVE_STOCK_USE_GOODS_RECEIVING=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_receive_stock_rejects_informal_purchase_zero_unit_price(self, mock_call):
        self.purchase_item.cost_per_unit = Decimal('0.00')
        self.purchase_item.save(update_fields=['cost_per_unit'])

        result = StockReceivingService.submit_purchase_item_receipt(self.purchase_item, Decimal('3.000'))

        self.assertFalse(result['submitted'])
        self.assertEqual(result['reason'], 'invalid_informal_purchase_unit_price')
        self.assertEqual(result['endpoint_key'], 'submit_informal_purchase')
        self.assertTrue(result['requires_action'])
        self.assertIn('unit price must be at least 0.01', result['error'])
        mock_call.assert_not_called()
        retry = SyncRetryQueue.objects.get(operation_type='submit_purchase_item_receipt')
        self.assertEqual(retry.payload['purchase_item_id'], str(self.purchase_item.id))
        self.assertEqual(retry.payload['quantity'], '3.000')
        self.assertTrue(MRAAPIError.objects.filter(error_code='invalid_informal_purchase_unit_price').exists())

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_receive_stock_requires_mra_supplier_id_without_adjustment_fallback(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_suppliers':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/get-suppliers',
                    data={'data': []},
                )
            self.fail(f'Purchase receipt should not call {endpoint_key} when supplierId is missing')

        mock_call.side_effect = fake_call

        result = StockReceivingService.submit_purchase_item_receipt(self.purchase_item, Decimal('3.000'))

        self.assertFalse(result['submitted'])
        self.assertEqual(result['reason'], 'missing_mra_supplier_id')
        self.assertTrue(result['requires_action'])
        self.assertEqual(result['endpoint_key'], 'submit_informal_purchase')
        self.assertIn('supplierId is required', result['error'])
        self.assertEqual([call[0] for call in calls], ['get_suppliers'])
        retry = SyncRetryQueue.objects.get(operation_type='submit_purchase_item_receipt')
        self.assertEqual(retry.payload['purchase_item_id'], str(self.purchase_item.id))
        self.assertEqual(retry.payload['quantity'], '3.000')
        self.assertIn('supplierId is required', retry.last_error)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_adjustment_reason_fetch_falls_back_to_get_if_post_is_not_allowed(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_stock_adjustment_reasons' and kwargs.get('method') == 'POST':
                raise MRAIntegrationError(
                    '405 Method Not Allowed',
                    status_code=405,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/getStockAdjustmentReasons',
                    endpoint_key='get_stock_adjustment_reasons',
                )
            if endpoint_key == 'get_stock_adjustment_reasons':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/getStockAdjustmentReasons',
                    data={'data': [{'reason': 'Stock Increase'}, {'reason': 'Stock Decrease'}]},
                )
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='https://dev-eis-api.mra.mw/api/v1/stock/submit-adjustment',
                data={'statusCode': 1, 'remark': 'Success', 'data': None, 'errors': []},
            )

        mock_call.side_effect = fake_call

        result = StockReceivingService.submit_inventory_item_adjustment(
            business=self.business,
            branch=self.branch,
            inventory_item=self.inventory_item,
            quantity=Decimal('1.000'),
            adjustment_type='Increase',
            reason='Stock received through POS',
            remarks='fallback method test',
        )

        self.assertTrue(result['submitted'])
        self.assertEqual(calls[0][0], 'get_stock_adjustment_reasons')
        self.assertEqual(calls[0][2].get('method'), 'POST')
        self.assertEqual(calls[1][0], 'get_stock_adjustment_reasons')
        self.assertEqual(calls[1][2].get('method'), 'GET')
        self.assertEqual(calls[2][0], 'submit_stock_adjustment')

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_waste_decrease_uses_matching_official_adjustment_reason(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_stock_adjustment_reasons':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/getStockAdjustmentReasons',
                    data={'data': [{'name': 'Stock Decrease'}, {'name': 'Expired Stock'}]},
                )
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='https://dev-eis-api.mra.mw/api/v1/stock/submit-adjustment',
                data={'statusCode': 1, 'remark': 'Success', 'data': None, 'errors': []},
            )

        mock_call.side_effect = fake_call

        result = StockReceivingService.submit_inventory_item_adjustment(
            business=self.business,
            branch=self.branch,
            inventory_item=self.inventory_item,
            quantity=Decimal('1.000'),
            adjustment_type='Decrease',
            reason='Waste: Expired',
            remarks='Waste record WR-001',
        )

        self.assertTrue(result['submitted'])
        self.assertEqual(calls[0][0], 'get_stock_adjustment_reasons')
        self.assertEqual(calls[1][0], 'submit_stock_adjustment')
        payload = calls[1][1]
        self.assertEqual(payload['adjustmentType'], 'Decrease')
        self.assertEqual(payload['adjustmentReason'], 'Expired Stock')
        self.assertIn('Waste record WR-001', payload['taxpayerRemarks'])
        self.assertIn('POS reason: Waste: Expired', payload['taxpayerRemarks'])

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_reversal_and_void_increase_uses_official_increase_reason(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_stock_adjustment_reasons':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/getStockAdjustmentReasons',
                    data={'data': [{'name': 'Damaged Stock'}, {'name': 'Stock Increase'}, {'name': 'Stock Decrease'}]},
                )
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='https://dev-eis-api.mra.mw/api/v1/stock/submit-adjustment',
                data={'statusCode': 1, 'remark': 'Success', 'data': None, 'errors': []},
            )

        mock_call.side_effect = fake_call

        result = StockReceivingService.submit_inventory_item_adjustment(
            business=self.business,
            branch=self.branch,
            inventory_item=self.inventory_item,
            quantity=Decimal('1.000'),
            adjustment_type='Increase',
            reason='Waste reversed: Damaged',
            remarks='Waste record WR-001 deleted.',
        )

        self.assertTrue(result['submitted'])
        payload = calls[1][1]
        self.assertEqual(payload['adjustmentType'], 'Increase')
        self.assertEqual(payload['adjustmentReason'], 'Stock Increase')
        self.assertIn('POS reason: Waste reversed: Damaged', payload['taxpayerRemarks'])

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_purchase_correction_decrease_uses_official_decrease_reason(self, mock_call):
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_stock_adjustment_reasons':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='https://dev-eis-api.mra.mw/api/v1/stock/getStockAdjustmentReasons',
                    data={'data': [{'reason': 'Stock Increase'}, {'reason': 'Stock Decrease'}]},
                )
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='https://dev-eis-api.mra.mw/api/v1/stock/submit-adjustment',
                data={'statusCode': 1, 'remark': 'Success', 'data': None, 'errors': []},
            )

        mock_call.side_effect = fake_call

        result = StockReceivingService.submit_purchase_item_adjustment(
            purchase_item=self.purchase_item,
            quantity=Decimal('1.000'),
            adjustment_type='Decrease',
            reason='Receive stock batch removed',
        )

        self.assertTrue(result['submitted'])
        payload = calls[1][1]
        self.assertEqual(payload['adjustmentType'], 'Decrease')
        self.assertEqual(payload['adjustmentReason'], 'Stock Decrease')
        self.assertIn('POS reason: Receive stock batch removed', payload['taxpayerRemarks'])


class TerminalActivationTests(TestCase):
    """Test terminal activation flow"""

    def setUp(self):
        self.user = User.objects.create_user(email='test@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Test Business')
        self.branch = Branch.objects.create(business=self.business, name='Main Branch', address='123 Main St', city='Lilongwe', country='Malawi')

        # Create TAC
        self.tac = TerminalActivationCode.objects.create(
            business=self.business,
            code='TAC-TEST-001',
            status='unused',
            expires_at=timezone.now() + timedelta(days=30)
        )

    def test_terminal_activation_success(self):
        """Test successful terminal activation"""
        terminal = TerminalService.activate_terminal(
            business=self.business,
            branch=self.branch,
            tac_code='TAC-TEST-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            device_serial='DEVICE-001',
            mac_address='00:1A:2B:3C:4D:5E'
        )

        self.assertIsNotNone(terminal)
        self.assertEqual(terminal.status, 'pending_activation')
        self.assertEqual(terminal.pos_name, 'Handy POS')
        self.assertEqual(terminal.os_type, 'Web')

    def test_tac_marked_as_used(self):
        """Test TAC is marked as used after activation"""
        terminal = TerminalService.activate_terminal(
            business=self.business,
            branch=self.branch,
            tac_code='TAC-TEST-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            device_serial='DEVICE-001'
        )

        self.tac.refresh_from_db()
        self.assertEqual(self.tac.status, 'used')
        self.assertEqual(self.tac.used_by_terminal, terminal)

    def test_tac_reuse_prevented(self):
        """Test TAC cannot be reused"""
        # First activation
        TerminalService.activate_terminal(
            business=self.business,
            branch=self.branch,
            tac_code='TAC-TEST-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            device_serial='DEVICE-001'
        )

        # Try to reuse TAC
        with self.assertRaises(ValueError):
            TerminalService.activate_terminal(
                business=self.business,
                branch=self.branch,
                tac_code='TAC-TEST-001',
                pos_name='Handy POS 2',
                pos_version='1.0.0',
                os_type='Web',
                device_serial='DEVICE-002'
            )

    def test_expired_tac_rejected(self):
        """Test expired TAC is rejected"""
        expired_tac = TerminalActivationCode.objects.create(
            business=self.business,
            code='TAC-EXPIRED',
            status='unused',
            expires_at=timezone.now() - timedelta(days=1)
        )

        with self.assertRaises(ValueError):
            TerminalService.activate_terminal(
                business=self.business,
                branch=self.branch,
                tac_code='TAC-EXPIRED',
                pos_name='Handy POS',
                pos_version='1.0.0',
                os_type='Web',
                device_serial='DEVICE-001'
            )

    def test_same_branch_can_have_distinct_device_terminals(self):
        first = TerminalService._upsert_terminal(
            business=self.business,
            branch=self.branch,
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Windows',
            device_serial='DEVICE-A',
            mac_address='',
        )
        first.status = 'active'
        first.save(update_fields=['status', 'updated_at'])

        second = TerminalService._upsert_terminal(
            business=self.business,
            branch=self.branch,
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Android',
            device_serial='DEVICE-B',
            mac_address='',
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(
            Terminal.objects.filter(business=self.business, branch=self.branch).count(),
            2,
        )
        self.assertEqual(second.device_serial, 'DEVICE-B')

    @override_settings(MRA_EIS_PRODUCT_ID='HandyPOS')
    def test_activation_payload_allows_arbitrary_pos_product_id(self):
        """MRA sandbox accepts a non-empty POS product ID chosen by the POS vendor."""
        payload = TerminalService._build_activation_payload(
            tac_code='TAC-TEST-001',
            pos_version='1.0.0',
            os_type='Web',
            mac_address='00:1A:2B:3C:4D:5E',
        )

        self.assertEqual(payload['environment']['pos']['productID'], 'HandyPOS')

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    def test_activation_extracts_case_variant_credentials_for_confirmation(self):
        """Activation parsing should tolerate casing differences in MRA credential keys."""
        activation_response = {
            'statusCode': 1,
            'Data': {
                'ActivatedTerminal': {
                    'TerminalID': 'MRA-TERM-CASE-001',
                    'TaxpayerId': 70267581,
                    'TerminalPosition': 3,
                    'TerminalCredentials': {
                        'JWTToken': 'case-jwt-token',
                        'SecretKey': 'case-secret-key',
                    },
                },
            },
        }
        confirm_response = {'statusCode': 1, 'data': True}
        configuration_response = {
            'statusCode': 1,
            'data': {
                'globalConfiguration': {'versionNo': 1, 'taxrates': []},
                'terminalConfiguration': {'versionNo': 1},
                'taxpayerConfiguration': {'versionNo': 1, 'tin': self.business.tin},
            },
        }

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.side_effect = [
                MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/onboarding/activate-terminal',
                    data=activation_response,
                ),
                MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/configurations/get-latest-configuration',
                    data=configuration_response,
                ),
                MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/onboarding/terminal-activated-confirmation',
                    data=confirm_response,
                ),
            ]
            terminal = TerminalService.activate_terminal(
                business=self.business,
                branch=self.branch,
                tac_code='TAC-TEST-001',
                pos_name='Handy POS',
                pos_version='1.0.0',
                os_type='Web',
                device_serial='DEVICE-CASE-001',
            )

        self.assertEqual(terminal.status, 'active')
        self.assertEqual(terminal.mra_token, 'case-jwt-token')
        self.assertEqual(terminal.mra_api_key, 'case-secret-key')
        self.assertEqual(terminal.mra_taxpayer_id, 70267581)
        self.assertEqual(terminal.terminal_position, 3)
        self.assertEqual(mocked_call.call_count, 3)
        self.assertEqual(mocked_call.call_args_list[1].args[0], 'get_latest_config')
        self.assertEqual(mocked_call.call_args_list[2].args[0], 'confirm_terminal')

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    def test_activation_does_not_confirm_without_jwt_token(self):
        """Avoid sending a confirmation request that cannot include Authorization."""
        activation_response = {
            'statusCode': 1,
            'data': {
                'activatedTerminal': {
                    'terminalId': 'MRA-TERM-NO-JWT',
                    'terminalCredentials': {
                        'secretKey': 'secret-without-jwt',
                    },
                },
            },
        }

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/onboarding/activate-terminal',
                data=activation_response,
            )
            terminal = TerminalService.activate_terminal(
                business=self.business,
                branch=self.branch,
                tac_code='TAC-TEST-001',
                pos_name='Handy POS',
                pos_version='1.0.0',
                os_type='Web',
                device_serial='DEVICE-NO-JWT',
            )

        self.assertEqual(mocked_call.call_count, 1)
        self.assertEqual(terminal.mra_api_key, 'secret-without-jwt')
        self.assertEqual(terminal.mra_token, '')
        audit = terminal.audit_logs.filter(action='activated').first()
        self.assertIn('jwtToken', audit.details.get('error', ''))

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    def test_activation_extracts_jwt_token_from_authorization_header(self):
        """Some gateways may return the activation token in headers."""
        activation_response = {
            'statusCode': 1,
            'data': {
                'activatedTerminal': {
                    'terminalId': 'MRA-TERM-HEADER-JWT',
                    'terminalCredentials': {
                        'secretKey': 'header-secret-key',
                    },
                },
            },
        }
        confirm_response = {'statusCode': 1, 'data': True}
        configuration_response = {
            'statusCode': 1,
            'data': {
                'globalConfiguration': {'versionNo': 1, 'taxrates': []},
                'terminalConfiguration': {'versionNo': 1},
                'taxpayerConfiguration': {'versionNo': 1, 'tin': self.business.tin},
            },
        }

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.side_effect = [
                MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/onboarding/activate-terminal',
                    data=activation_response,
                    headers={'Authorization': 'Bearer header-jwt-token'},
                ),
                MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/configurations/get-latest-configuration',
                    data=configuration_response,
                ),
                MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/onboarding/terminal-activated-confirmation',
                    data=confirm_response,
                ),
            ]
            terminal = TerminalService.activate_terminal(
                business=self.business,
                branch=self.branch,
                tac_code='TAC-TEST-001',
                pos_name='Handy POS',
                pos_version='1.0.0',
                os_type='Web',
                device_serial='DEVICE-HEADER-JWT',
            )

        self.assertEqual(terminal.mra_token, 'header-jwt-token')
        self.assertEqual(terminal.status, 'active')
        self.assertEqual(mocked_call.call_args_list[1].args[0], 'get_latest_config')
        self.assertEqual(mocked_call.call_args_list[2].args[0], 'confirm_terminal')

    def test_refresh_token_fails_locally_when_jwt_missing(self):
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-NO-JWT',
            device_serial='DEVICE-NO-JWT',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-NO-JWT',
            mra_api_key='secret-without-token',
            mra_token='',
            status='pending_activation',
        )

        with self.assertRaisesMessage(MRAIntegrationError, 'Terminal JWT token is missing'):
            TerminalService.refresh_token(terminal)

    def test_reset_failed_activation_removes_local_terminal_and_unlocks_tac(self):
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-FAILED-ACTIVATION',
            device_serial='DEVICE-FAILED-ACTIVATION',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-FAILED-ACTIVATION',
            mra_api_key='secret-without-token',
            mra_token='',
            status='pending_activation',
        )
        self.tac.status = 'used'
        self.tac.used_by_terminal = terminal
        self.tac.used_at = timezone.now()
        self.tac.save(update_fields=['status', 'used_by_terminal', 'used_at', 'updated_at'])

        result = TerminalService.reset_failed_activation(terminal)

        self.assertEqual(result['status'], 'reset')
        self.assertFalse(Terminal.objects.filter(id=terminal.id).exists())
        self.tac.refresh_from_db()
        self.assertEqual(self.tac.status, 'unused')
        self.assertIsNone(self.tac.used_by_terminal)
        self.assertIsNone(self.tac.used_at)

    def test_reset_failed_activation_rejects_active_terminal_with_token(self):
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-ACTIVE-TOKEN',
            device_serial='DEVICE-ACTIVE-TOKEN',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-ACTIVE-TOKEN',
            mra_api_key='active-secret',
            mra_token='active-token',
            status='active',
        )

        with self.assertRaisesMessage(ValueError, 'Active terminals cannot be reset'):
            TerminalService.reset_failed_activation(terminal)

        self.assertTrue(Terminal.objects.filter(id=terminal.id).exists())

    def test_reset_failed_activation_rejects_terminal_with_invoices(self):
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-WITH-INVOICE',
            device_serial='DEVICE-WITH-INVOICE',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-WITH-INVOICE',
            mra_api_key='secret-without-token',
            mra_token='',
            status='pending_activation',
        )
        MRAInvoice.objects.create(
            business=self.business,
            branch=self.branch,
            terminal=terminal,
            invoice_number=1,
            seller_tin='100000000',
            seller_name='Test Business',
            items=[],
            net_amount=Decimal('0.00'),
            tax_amount=Decimal('0.00'),
            gross_amount=Decimal('0.00'),
            invoice_date=timezone.now(),
        )

        with self.assertRaisesMessage(ValueError, 'Cannot reset a terminal that already has fiscal invoices'):
            TerminalService.reset_failed_activation(terminal)

        self.assertTrue(Terminal.objects.filter(id=terminal.id).exists())


class TerminalBlockingComplianceTests(TransactionTestCase):
    """Terminal blocking and unblock handling required by MRA utilities."""

    def setUp(self):
        self.user = User.objects.create_user(email='blocking@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Blocking Business', tin='70267581')
        BusinessSettings.objects.create(business=self.business, enable_eis=True)
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main',
            address='123 Main St',
            city='Blantyre',
            country='Malawi',
        )
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-BLOCK-001',
            device_serial='DEVICE-BLOCK-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-BLOCK-001',
            mra_taxpayer_id=70267581,
            terminal_position=3,
            mra_api_key='test-terminal-secret',
            mra_token='test-terminal-jwt',
            status='active',
            is_online=True,
        )
        self._create_fresh_configurations()
        TerminalService.record_server_time_sync(
            self.terminal,
            server_time=timezone.now(),
            source='test_fixture',
        )

    def _create_fresh_configurations(self):
        now = timezone.now()
        configs = {
            'global_configuration': {
                'versionNo': 1,
                'taxrates': [{'id': 'NRT', 'rate': 0, 'name': 'Non Rated'}],
            },
            'terminal_configuration': {
                'versionNo': 1,
                'terminalSite': {'siteId': 'SITE-BLOCK-001', 'siteName': 'Main'},
            },
            'taxpayer_configuration': {
                'versionNo': 1,
                'tin': '70267581',
                'isVATRegistered': False,
                'activatedTaxRateIds': ['NRT'],
            },
            'system_settings': {
                'globalConfiguration': {'versionNo': 1},
                'terminalConfiguration': {'versionNo': 1},
                'taxpayerConfiguration': {'versionNo': 1},
            },
            'terminal_site_products': [
                {
                    'productCode': '2934309406073',
                    'description': 'Uncategorized | each',
                    'taxRateId': 'NRT',
                    'siteId': 'SITE-BLOCK-001',
                    'unitOfMeasure': 'each',
                }
            ],
        }
        for config_type, config_data in configs.items():
            MRAConfiguration.objects.create(
                business=self.business,
                config_type=config_type,
                config_version=f'{config_type}-v1',
                config_data=config_data,
                effective_from=now,
                fetched_from_mra_at=now,
                is_active=True,
            )

    def _create_ready_order(self):
        inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Amazon Big Candy',
            category='Candy',
            item_type='sellable',
            stock_units=Decimal('10.000'),
            unit_type='each',
            cost=Decimal('100.00'),
            price=Decimal('250.00'),
            barcode='2934309406073',
            product_code='2934309406073',
        )
        MRAProductMapping.objects.create(
            inventory_item=inventory_item,
            branch=self.branch,
            mra_product_code='2934309406073',
            mra_product_name='Uncategorized | each',
            mra_tax_type='zero',
            mra_tax_rate=Decimal('0.00'),
            mra_unit_measure='each',
            tax_calculation_method='inclusive',
            is_approved=True,
            approved_at=timezone.now(),
            mra_synced=True,
            last_synced_at=timezone.now(),
        )
        order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=901,
            status='Completed',
            payment_method='Cash',
            subtotal=Decimal('250.00'),
            total=Decimal('250.00'),
            net_amount=Decimal('250.00'),
            vat_amount=Decimal('0.00'),
            gross_amount=Decimal('250.00'),
        )
        OrderItem.objects.create(
            order=order,
            inventory_item_id=str(inventory_item.id),
            name=inventory_item.name,
            quantity=Decimal('1.000'),
            price=Decimal('250.00'),
            subtotal=Decimal('250.00'),
            tax_amount=Decimal('0.00'),
            total=Decimal('250.00'),
        )
        return order

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_blocking_message_marks_terminal_suspended(self, mock_call):
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/utilities/get-terminal-blocking-message',
            data={
                'statusCode': 1,
                'remark': 'Blocked',
                'data': {
                    'isBlocked': True,
                    'blockingReason': 'Offline invoices exceeded configured limit',
                    'blockedAt': '2026-05-25T08:00:00Z',
                },
                'errors': [],
            },
        )

        result = TerminalService.get_terminal_blocking_message(self.terminal)

        self.terminal.refresh_from_db()
        cached = TerminalService.get_cached_blocking_status(self.terminal)
        self.assertTrue(result['is_blocked'])
        self.assertEqual(self.terminal.status, 'suspended')
        self.assertIn('Offline invoices exceeded', cached['blocking_reason'])
        mock_call.assert_called_once_with(
            'get_terminal_blocking_message',
            payload={'terminalId': 'MRA-TERM-BLOCK-001'},
            method='POST',
            mutating=False,
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_unblock_status_reactivates_suspended_terminal(self, mock_call):
        self.terminal.status = 'suspended'
        self.terminal.save(update_fields=['status', 'updated_at'])
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/utilities/check-terminal-unblock-status',
            data={
                'statusCode': 1,
                'remark': 'Terminal unblocked',
                'data': {'isUnblocked': True},
                'errors': [],
            },
        )

        result = TerminalService.check_terminal_unblock_status(self.terminal)

        self.terminal.refresh_from_db()
        cached = TerminalService.get_cached_blocking_status(self.terminal)
        self.assertTrue(result['is_unblocked'])
        self.assertEqual(self.terminal.status, 'active')
        self.assertFalse(cached['is_blocked'])
        mock_call.assert_called_once_with(
            'check_terminal_unblock_status',
            payload={'terminalId': 'MRA-TERM-BLOCK-001'},
            method='POST',
            mutating=False,
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=False,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sale_checks_terminal_block_status_before_submission(self, mock_call):
        order = self._create_ready_order()
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={
                        'statusCode': 1,
                        'remark': 'Not blocked',
                        'data': {'isBlocked': False},
                        'errors': [],
                    },
                )
            if endpoint_key == 'report_sale':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/submit-sales-transaction',
                    data={
                        'statusCode': 1,
                        'remark': 'Submitted',
                        'data': {'validationURL': 'https://validate.example/receipt'},
                        'errors': [],
                    },
                )
            raise AssertionError(f'Unexpected endpoint {endpoint_key}')

        mock_call.side_effect = fake_call

        result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        self.assertEqual(result['eis_status'], 'SUBMITTED')
        self.assertEqual([call[0] for call in calls], ['get_terminal_blocking_message', 'report_sale'])

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sale_blocks_when_mra_reports_terminal_blocked_before_submission(self, mock_call):
        order = self._create_ready_order()
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/utilities/get-terminal-blocking-message',
            data={
                'statusCode': 1,
                'remark': 'Blocked',
                'data': {
                    'isBlocked': True,
                    'blockingReason': 'Terminal blocked by MRA test condition',
                },
                'errors': [],
            },
        )

        with self.assertRaisesMessage(MRAIntegrationError, 'MRA terminal is blocked'):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.status, 'suspended')
        self.assertFalse(order.fiscal_invoice_number)
        self.assertEqual(self.terminal.online_invoice_counter, 0)
        mock_call.assert_called_once_with(
            'get_terminal_blocking_message',
            payload={'terminalId': 'MRA-TERM-BLOCK-001'},
            method='POST',
            mutating=False,
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sale_uses_cached_terminal_state_when_block_check_network_fails(self, mock_call):
        order = self._create_ready_order()
        call_log = []

        def mra_call(endpoint_key, payload=None, **kwargs):
            call_log.append(endpoint_key)
            if endpoint_key in {
                'get_terminal_blocking_message',
                'get_last_online_transaction',
                'get_last_offline_transaction',
                'report_sale',
            }:
                raise MRAIntegrationError(
                    f'MRA request failed ({endpoint_key}): network timeout',
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    endpoint_key=endpoint_key,
                )
            raise AssertionError(f'Unexpected MRA endpoint {endpoint_key}')

        mock_call.side_effect = mra_call

        result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.terminal.refresh_from_db()
        self.assertEqual(result['endpoint'], 'report_sale_offline')
        self.assertEqual(result['response']['reason'], 'network_offline_fallback')
        self.assertTrue(order.fiscal_invoice_number)
        self.assertEqual(order.eis_status, 'PENDING')
        self.assertFalse(self.terminal.is_online)
        self.assertEqual(self.terminal.status, 'active')
        self.assertEqual(call_log, [
            'get_terminal_blocking_message',
            'get_last_online_transaction',
            'get_last_offline_transaction',
            'report_sale',
        ])

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_terminal_health_ping_marks_terminal_online(self, mock_call):
        self.terminal.is_online = False
        self.terminal.last_sync_at = None
        self.terminal.save(update_fields=['is_online', 'last_sync_at', 'updated_at'])
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/utilities/ping',
            data={'raw': 'Pong'},
            headers={'Date': 'Sun, 21 Jun 2026 09:30:00 GMT'},
        )

        result = TerminalService.check_terminal_health(self.terminal)

        self.terminal.refresh_from_db()
        self.assertTrue(result['is_online'])
        self.assertEqual(result['server_time'], '2026-06-21T09:30:00+00:00')
        self.assertEqual(result['server_time_source'], 'http_date_header')
        self.assertTrue(self.terminal.is_online)
        self.assertIsNotNone(self.terminal.last_sync_at)
        mock_call.assert_called_once_with(
            'ping',
            payload=None,
            method='POST',
            mutating=False,
            send_json=False,
            record_connectivity=False,
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_terminal_health_ping_falls_back_to_get_when_post_is_not_allowed(self, mock_call):
        self.terminal.is_online = False
        self.terminal.last_sync_at = None
        self.terminal.save(update_fields=['is_online', 'last_sync_at', 'updated_at'])

        def fake_call(endpoint_key, payload=None, **kwargs):
            if kwargs.get('method') == 'POST':
                raise MRAIntegrationError(
                    'MRA request failed (ping): 405 Method Not Allowed',
                    status_code=405,
                    endpoint='/api/v1/utilities/ping',
                    endpoint_key='ping',
                )
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/utilities/ping',
                data={'raw': 'Pong'},
            )

        mock_call.side_effect = fake_call

        result = TerminalService.check_terminal_health(self.terminal)

        self.terminal.refresh_from_db()
        self.assertTrue(result['is_online'])
        self.assertEqual(result['method'], 'GET')
        self.assertTrue(self.terminal.is_online)
        self.assertIsNotNone(self.terminal.last_sync_at)
        self.assertEqual(mock_call.call_count, 2)
        self.assertEqual(mock_call.call_args_list[0].kwargs['method'], 'POST')
        self.assertEqual(mock_call.call_args_list[1].kwargs['method'], 'GET')

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_terminal_health_ping_failure_marks_terminal_offline(self, mock_call):
        self.terminal.is_online = True
        self.terminal.save(update_fields=['is_online', 'updated_at'])
        mock_call.side_effect = MRAIntegrationError(
            'MRA request failed (ping): network down',
            endpoint='/api/v1/utilities/ping',
            endpoint_key='ping',
        )

        result = TerminalService.check_terminal_health(self.terminal)

        self.terminal.refresh_from_db()
        self.assertFalse(result['is_online'])
        self.assertFalse(self.terminal.is_online)
        self.assertIn('network down', result['error'])

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_FRESH_CONFIG_FOR_SALES=False,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sale_submission_hard_blocks_stale_config_even_if_env_best_effort(self, mock_call):
        stale_time = timezone.now() - timedelta(hours=72)
        MRAConfiguration.objects.filter(business=self.business).update(
            effective_from=stale_time,
            fetched_from_mra_at=stale_time,
        )
        order = self._create_ready_order()

        def fake_call(endpoint_key, payload=None, **kwargs):
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={
                        'statusCode': 1,
                        'remark': 'Not blocked',
                        'data': {'isBlocked': False},
                        'errors': [],
                    },
                )
            raise MRAIntegrationError(
                'configuration endpoint unavailable',
                endpoint='/api/v1/configuration/get-latest-configs',
                endpoint_key='get_latest_config',
            )

        mock_call.side_effect = fake_call

        with self.assertRaisesRegex(MRAIntegrationError, 'configuration refresh failed'):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        self.assertEqual([call.args[0] for call in mock_call.call_args_list], [
            'get_terminal_blocking_message',
            'get_latest_config',
        ])
        self.terminal.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.terminal.online_invoice_counter, 0)
        self.assertFalse(order.fiscal_invoice_number)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sale_response_should_block_fetches_official_blocking_reason(self, mock_call):
        order = self._create_ready_order()
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'get_terminal_blocking_message' and len(calls) == 1:
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={
                        'statusCode': 1,
                        'remark': 'Not blocked',
                        'data': {'isBlocked': False},
                        'errors': [],
                    },
                )
            if endpoint_key == 'report_sale':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/submit-sales-transaction',
                    data={
                        'statusCode': 1,
                        'remark': 'Submitted',
                        'data': {
                            'validationURL': 'https://validate.example/receipt',
                            'shouldBlockTerminal': True,
                        },
                        'errors': [],
                    },
                )
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={
                        'statusCode': 1,
                        'remark': 'Blocked',
                        'data': {
                            'isBlocked': True,
                            'blockingReason': 'MRA administrative block',
                            'blockedAt': '2026-05-25T08:00:00Z',
                        },
                        'errors': [],
                    },
                )
            raise AssertionError(f'Unexpected endpoint {endpoint_key}')

        mock_call.side_effect = fake_call

        result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        self.terminal.refresh_from_db()
        cached = TerminalService.get_cached_blocking_status(self.terminal)
        self.assertEqual(result['eis_status'], 'SUBMITTED')
        self.assertEqual(self.terminal.status, 'suspended')
        self.assertEqual(cached['blocking_reason'], 'MRA administrative block')
        self.assertEqual([call[0] for call in calls], [
            'get_terminal_blocking_message',
            'report_sale',
            'get_terminal_blocking_message',
        ])


class ConfigurationTests(TestCase):
    """Test configuration management"""

    def setUp(self):
        self.user = User.objects.create_user(email='test@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Test Business')

    def test_configuration_storage(self):
        """Test configuration is stored immutably"""
        config = MRAConfiguration.objects.create(
            business=self.business,
            config_type='tax_rules',
            config_version='1.0',
            config_data={'standard': 16.5, 'zero': 0, 'exempt': 0},
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True
        )

        self.assertIsNotNone(config)
        self.assertEqual(config.config_type, 'tax_rules')
        self.assertTrue(config.is_current())

    def test_store_configuration_response_is_idempotent_for_same_version(self):
        """Repeated MRA activation/sync responses with the same version should not crash."""
        first_response = {
            'data': {
                'globalConfiguration': {
                    'versionNo': 7,
                    'taxrates': [{'rate': 16.5}],
                }
            }
        }
        second_response = {
            'data': {
                'globalConfiguration': {
                    'versionNo': 7,
                    'taxrates': [{'rate': 16.5}],
                    'retryMarker': 'second-pass',
                }
            }
        }

        first = ConfigurationService.store_configuration_response(
            self.business,
            first_response,
            source='activation',
            config_types=['global_configuration'],
        )[0]
        second = ConfigurationService.store_configuration_response(
            self.business,
            second_response,
            source='activation',
            config_types=['global_configuration'],
        )[0]

        self.assertEqual(first.id, second.id)
        self.assertEqual(
            MRAConfiguration.objects.filter(
                business=self.business,
                config_type='global_configuration',
                config_version='7',
            ).count(),
            1,
        )
        second.refresh_from_db()
        self.assertTrue(second.is_active)
        self.assertIsNone(second.effective_to)
        self.assertEqual(second.config_data['retryMarker'], 'second-pass')

    def test_configuration_versioning(self):
        """Test configuration versioning"""
        config1 = MRAConfiguration.objects.create(
            business=self.business,
            config_type='tax_rules',
            config_version='1.0',
            config_data={'standard': 16.5},
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True
        )

        config2 = MRAConfiguration.objects.create(
            business=self.business,
            config_type='tax_rules',
            config_version='2.0',
            config_data={'standard': 17.0},
            effective_from=timezone.now() + timedelta(days=1),
            fetched_from_mra_at=timezone.now(),
            is_active=True
        )

        current = ConfigurationService.get_active_configuration(
            self.business,
            'tax_rules'
        )
        self.assertEqual(current.config_version, '1.0')

    def test_offline_limits_are_extracted_from_system_settings(self):
        """Offline policy should be parsed from MRA configuration payloads."""
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='system_settings',
            config_version='2026.02',
            config_data={
                'offlineLimit': {
                    'maxTransactionAgeInHours': 72,
                    'maxCummulativeAmount': '2500000.00',
                }
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )

        limits = ConfigurationService.get_offline_limits(self.business)
        self.assertEqual(limits.max_transaction_age_hours, 72)
        self.assertEqual(limits.max_cumulative_amount, Decimal('2500000.00'))
        self.assertIn('system_settings', str(limits.source))

    def test_latest_config_request_flag_accepts_nested_serialized_truthy_values(self):
        """MRA may serialize config refresh flags as strings/numbers or nest them."""
        self.assertTrue(
            ConfigurationService.response_requests_latest_config(
                {'data': {'meta': {'shouldDownloadLatestConfig': 'true'}}}
            )
        )
        self.assertTrue(
            ConfigurationService.response_requests_latest_config(
                {'response': {'should_download_latest_config': 1}}
            )
        )
        self.assertTrue(
            ConfigurationService.response_requests_latest_config(
                {'data': json.dumps({'shouldDownloadLatestConfig': 'yes'})}
            )
        )
        self.assertFalse(
            ConfigurationService.response_requests_latest_config(
                {'data': {'shouldDownloadLatestConfig': 'false'}}
            )
        )


class MRASwaggerContractTests(TestCase):
    """Guards for the live MRA EIS Swagger contract."""

    def setUp(self):
        self.user = User.objects.create_user(email='contract@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Contract Business')
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main',
            address='123 Main St',
            city='Lilongwe',
            country='Malawi',
        )
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-CONTRACT-001',
            device_serial='DEVICE-CONTRACT-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-CONTRACT-001',
            mra_api_key='test-key',
            mra_token='test-token',
            status='active',
            is_online=True,
        )

    def test_endpoint_map_matches_current_swagger_paths(self):
        expected_paths = {
            'activate_terminal': '/api/v1/onboarding/activate-terminal',
            'confirm_terminal': '/api/v1/onboarding/terminal-activated-confirmation',
            'get_latest_config': '/api/v1/configuration/get-latest-configs',
            'request_new_terminal_token': '/api/v1/configuration/request-new-terminal-token',
            'report_sale': '/api/v1/sales/submit-sales-transaction',
            'get_last_online_transaction': '/api/v1/sales/last-submitted-online-transaction',
            'get_last_offline_transaction': '/api/v1/sales/last-submitted-offline-transaction',
            'process_credit_debit_note': '/api/v1/sales/process-credit-debit-note',
            'get_invoice_by_number': '/api/v1/sales/get-invoice-by-number',
            'cancel_receipt': '/api/v1/sales/cancel-receipt',
            'get_void_receipts': '/api/v1/sales/get-void-receipts',
            'initial_inventory_upload': '/api/v1/utilities/taxpayer-initial-inventory-upload',
            'get_terminal_site_products': '/api/v1/utilities/get-terminal-site-products',
            'product_status': '/api/v1/utilities/product-status',
            'ping': '/api/v1/utilities/ping',
            'validate_vat5': '/api/v1/utilities/validate-vat5-certificate',
            'get_terminal_blocking_message': '/api/v1/utilities/get-terminal-blocking-message',
            'check_terminal_unblock_status': '/api/v1/utilities/check-terminal-unblock-status',
            'transfer_inventory': '/api/v1/stock/transfer-inventory',
            'warehouse_inventory': '/api/v1/stock/warehouse-inventory',
            'add_product': '/api/v1/stock/add-product',
            'get_hs_codes': '/api/v1/stock/get-hs-codes',
            'get_units_of_measure': '/api/v1/stock/get-units-of-measure',
        }

        for endpoint_key, expected_path in expected_paths.items():
            self.assertEqual(settings.MRA_EIS_ENDPOINTS.get(endpoint_key), expected_path)

    @patch.object(MRAEISClient, 'call')
    def test_receipt_lookup_service_uses_get_invoice_by_number_schema(self, mock_call):
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/sales/get-invoice-by-number',
            data={
                'statusCode': 1,
                'remark': 'Success',
                'data': {
                    'invoiceHeader': {'invoiceNumber': 'CuQ-D-JY4P-D'},
                    'invoiceLineItems': [],
                    'invoiceSummary': {'taxBreakDown': []},
                    'validationURL': 'https://validate.example/receipt',
                },
                'errors': [],
            },
        )

        result = ReceiptLookupService.lookup_invoice_by_number(self.terminal, 'CuQ-D-JY4P-D')

        self.assertTrue(result['found'])
        self.assertEqual(result['invoice_number'], 'CuQ-D-JY4P-D')
        mock_call.assert_called_once_with(
            'get_invoice_by_number',
            payload={'invoiceNumber': 'CuQ-D-JY4P-D'},
            method='POST',
            mutating=False,
        )

    @patch.object(MRAEISClient, 'call')
    def test_void_receipt_lookup_service_uses_get_void_receipts_schema(self, mock_call):
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/sales/get-void-receipts',
            data={
                'statusCode': 1,
                'remark': 'Success',
                'data': {
                    'items': [
                        {
                            'invoiceNumber': 'CuQ-D-JY4K-L',
                            'requestReason': 'returned',
                            'status': 'Pending',
                        }
                    ],
                    'page': 1,
                    'pageSize': 25,
                    'totalCount': 1,
                },
                'errors': [],
            },
        )

        result = ReceiptLookupService.get_void_receipts(
            self.terminal,
            invoice_number='CuQ-D-JY4K-L',
            status_value='1',
            start_date='2026-06-01T00:00:00.000Z',
            end_date='2026-06-30T23:59:59.999Z',
        )

        self.assertEqual(result['total_count'], 1)
        self.assertEqual(result['items'][0]['invoiceNumber'], 'CuQ-D-JY4K-L')
        mock_call.assert_called_once_with(
            'get_void_receipts',
            payload={
                'page': 1,
                'pageSize': 25,
                'invoiceNumber': 'CuQ-D-JY4K-L',
                'status': 1,
                'startDate': '2026-06-01T00:00:00.000Z',
                'endDate': '2026-06-30T23:59:59.999Z',
            },
            method='POST',
            mutating=False,
        )

    @patch('mra_eis.views.ReceiptLookupService.lookup_invoice_by_number')
    def test_terminal_lookup_invoice_action_is_exposed(self, mock_lookup):
        mock_lookup.return_value = {'found': True, 'invoice_number': 'CuQ-D-JY4P-D'}
        request = APIRequestFactory().post(
            f'/mra-eis/terminals/{self.terminal.id}/lookup_invoice/',
            {'invoiceNumber': 'CuQ-D-JY4P-D'},
            format='json',
        )
        force_authenticate(request, user=self.user)

        response = TerminalViewSet.as_view({'post': 'lookup_invoice'})(request, pk=self.terminal.id)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['found'])
        mock_lookup.assert_called_once()
        self.assertEqual(mock_lookup.call_args.args[0], self.terminal)
        self.assertEqual(mock_lookup.call_args.args[1], 'CuQ-D-JY4P-D')

    @patch('mra_eis.views.ReceiptLookupService.get_void_receipts')
    def test_terminal_get_void_receipts_action_is_exposed(self, mock_lookup):
        mock_lookup.return_value = {'items': [{'invoiceNumber': 'CuQ-D-JY4K-L'}], 'total_count': 1}
        request = APIRequestFactory().post(
            f'/mra-eis/terminals/{self.terminal.id}/get_void_receipts/',
            {
                'invoiceNumber': 'CuQ-D-JY4K-L',
                'status': '1',
                'startDate': '2026-06-01T00:00:00.000Z',
                'endDate': '2026-06-30T23:59:59.999Z',
            },
            format='json',
        )
        force_authenticate(request, user=self.user)

        response = TerminalViewSet.as_view({'post': 'get_void_receipts'})(request, pk=self.terminal.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total_count'], 1)
        mock_lookup.assert_called_once()
        self.assertEqual(mock_lookup.call_args.args[0], self.terminal)
        self.assertEqual(mock_lookup.call_args.kwargs['invoice_number'], 'CuQ-D-JY4K-L')
        self.assertEqual(mock_lookup.call_args.kwargs['status_value'], '1')

    def test_latest_config_uses_post_method(self):
        response_payload = {
            'data': {
                'globalConfiguration': {'versionNo': 1, 'taxrates': []},
                'terminalConfiguration': {'versionNo': 1},
                'taxpayerConfiguration': {'versionNo': 1},
            }
        }

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/configuration/get-latest-configs',
                data=response_payload,
            )
            ConfigurationService.fetch_and_store_configuration(
                self.business,
                terminal=self.terminal,
            )

        _, kwargs = mocked_call.call_args
        self.assertEqual(kwargs.get('method'), 'POST')
        self.assertEqual(mocked_call.call_args.args[0], 'get_latest_config')

    def test_activation_confirmation_headers_include_signature_and_token(self):
        client = MRAEISClient(terminal=self.terminal)
        headers = client._build_headers(
            'confirm_terminal',
            {'terminalId': self.terminal.mra_terminal_id},
            x_signature_text='TEST-TAC',
        )
        expected_signature = base64.b64encode(
            hmac.new(b'test-key', b'TEST-TAC', hashlib.sha512).digest()
        ).decode('utf-8')

        self.assertEqual(headers.get('Accept'), 'text/plain')
        self.assertEqual(headers.get('x-signature'), expected_signature)
        self.assertEqual(headers.get('Authorization'), 'Bearer test-token')
        self.assertNotIn('x-access-key', headers)
        self.assertNotIn('x-eis-message-hash', headers)

    @override_settings(MRA_EIS_ACCESS_KEY='vendor-access-key')
    def test_activation_headers_match_swagger_without_auth_headers(self):
        client = MRAEISClient(terminal=self.terminal)
        headers = client._build_headers(
            'activate_terminal',
            {'terminalActivationCode': 'TEST-TAC'},
        )

        self.assertEqual(headers.get('Accept'), 'text/plain')
        self.assertNotIn('x-access-key', headers)
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('x-signature', headers)
        self.assertNotIn('x-eis-message-hash', headers)

    def test_authenticated_headers_do_not_double_prefix_saved_bearer_token(self):
        for saved_token in (
            'Bearer test-token',
            'Bearer Bearer test-token',
            'Authorization: Bearer test-token',
        ):
            self.terminal.mra_token = saved_token
            client = MRAEISClient(terminal=self.terminal)
            headers = client._build_headers(
                'get_terminal_site_products',
                {'tin': '70267581', 'siteId': 'SITE-001'},
            )

            self.assertEqual(headers.get('Authorization'), 'Bearer test-token')

    def test_activation_headers_do_not_use_existing_terminal_bearer_token(self):
        client = MRAEISClient(terminal=self.terminal)
        headers = client._build_headers(
            'activate_terminal',
            {'terminalActivationCode': 'TEST-TAC'},
        )

        self.assertNotIn('Authorization', headers)

    def test_sale_submission_headers_use_bearer_authorization_and_message_hash(self):
        client = MRAEISClient(terminal=self.terminal)
        payload = {'invoiceHeader': {'invoiceNumber': 'TEST-1'}}
        headers = client._build_headers('report_sale', payload)
        canonical_payload = json.dumps(payload, separators=(',', ':'), sort_keys=True, default=str)
        expected_hash = base64.b64encode(
            hmac.new(b'test-key', canonical_payload.encode('utf-8'), hashlib.sha512).digest()
        ).decode('utf-8')

        self.assertEqual(headers.get('Accept'), 'text/plain')
        self.assertEqual(headers.get('Authorization'), 'Bearer test-token')
        self.assertEqual(headers.get('x-eis-message-hash'), expected_hash)
        self.assertNotIn('x-access-key', headers)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_RECORD_MESSAGE_HASH_EVIDENCE=True,
        MRA_EIS_LOG_MESSAGE_HASH_INPUT=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_authenticated_mra_call_sends_bearer_authorization_header(self, mocked_request):
        class SuccessResponse:
            ok = True
            status_code = 200
            reason = 'OK'
            content = b'{"statusCode":1,"data":[]}'
            text = '{"statusCode":1,"data":[]}'
            headers = {}

            @staticmethod
            def json():
                return {'statusCode': 1, 'data': []}

        mocked_request.return_value = SuccessResponse()
        client = MRAEISClient(terminal=self.terminal)

        client.call(
            'get_terminal_site_products',
            payload={'tin': '70267581', 'siteId': 'SITE-001'},
            method='POST',
            mutating=False,
        )

        request_headers = mocked_request.call_args.kwargs['headers']
        request_body = mocked_request.call_args.kwargs['data']
        self.assertEqual(request_headers.get('Accept'), 'text/plain')
        self.assertEqual(request_headers.get('Authorization'), 'Bearer test-token')
        self.assertIn('x-eis-message-hash', request_headers)
        audit = TerminalAuditLog.objects.get(
            terminal=self.terminal,
            action='mra_request_signed',
        )
        details = audit.details
        expected_input = json.dumps(
            {'tin': '70267581', 'siteId': 'SITE-001'},
            separators=(',', ':'),
            sort_keys=True,
            default=str,
        )
        self.assertNotIn('json', mocked_request.call_args.kwargs)
        self.assertEqual(request_body, expected_input)
        self.assertEqual(details['endpoint_key'], 'get_terminal_site_products')
        self.assertEqual(details['hash_algorithm'], 'HMAC-SHA512')
        self.assertEqual(details['hash_encoding'], 'base64')
        self.assertEqual(details['hash_input_source'], 'canonical_json')
        self.assertEqual(details['hash_input_text'], expected_input)
        self.assertEqual(
            details['hash_input_sha256'],
            hashlib.sha256(expected_input.encode('utf-8')).hexdigest(),
        )
        self.assertEqual(details['status_code'], 200)
        self.assertTrue(details['ok'])
        self.assertEqual(details['request_body_sha256'], details['hash_input_sha256'])
        self.assertTrue(details['request_body_matches_hash_input'])
        self.assertTrue(details['headers']['authorization_present'])
        self.assertTrue(details['headers']['x_eis_message_hash_present'])
        serialized_details = json.dumps(details, sort_keys=True)
        self.assertNotIn('test-token', serialized_details)
        self.assertNotIn('test-key', serialized_details)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_RECORD_MESSAGE_HASH_EVIDENCE=True,
        MRA_EIS_LOG_MESSAGE_HASH_INPUT=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_authenticated_mra_call_records_explicit_message_hash_text(self, mocked_request):
        class SuccessResponse:
            ok = True
            status_code = 200
            reason = 'OK'
            content = b'{"statusCode":1,"data":[]}'
            text = '{"statusCode":1,"data":[]}'
            headers = {}

            @staticmethod
            def json():
                return {'statusCode': 1, 'data': []}

        mocked_request.return_value = SuccessResponse()
        client = MRAEISClient(terminal=self.terminal)
        explicit_hash_input = 'report_sale|TEST-1|250.00'

        client.call(
            'report_sale',
            payload={'invoiceHeader': {'invoiceNumber': 'TEST-1'}},
            method='POST',
            message_hash_text=explicit_hash_input,
        )

        request_headers = mocked_request.call_args.kwargs['headers']
        request_body = mocked_request.call_args.kwargs['data']
        expected_hash = base64.b64encode(
            hmac.new(b'test-key', explicit_hash_input.encode('utf-8'), hashlib.sha512).digest()
        ).decode('utf-8')
        self.assertEqual(request_headers.get('x-eis-message-hash'), expected_hash)
        self.assertNotIn('json', mocked_request.call_args.kwargs)
        self.assertEqual(request_body, '{"invoiceHeader":{"invoiceNumber":"TEST-1"}}')
        audit = TerminalAuditLog.objects.get(
            terminal=self.terminal,
            action='mra_request_signed',
        )
        self.assertEqual(audit.details['hash_input_source'], 'explicit_message_hash_text')
        self.assertEqual(audit.details['hash_input_text'], explicit_hash_input)
        self.assertFalse(audit.details['requires_mra_confirmation'])
        self.assertFalse(audit.details['request_body_matches_hash_input'])

    @override_settings(MRA_EIS_MESSAGE_HASH_INPUT_MODE='compact_json')
    def test_message_hash_mode_can_use_compact_json_without_sorting(self):
        client = MRAEISClient(terminal=self.terminal)
        payload = {'b': 1, 'a': 2}
        headers = client._build_headers('report_sale', payload)
        expected_input = json.dumps(payload, separators=(',', ':'), sort_keys=False, default=str)
        expected_hash = base64.b64encode(
            hmac.new(b'test-key', expected_input.encode('utf-8'), hashlib.sha512).digest()
        ).decode('utf-8')

        self.assertEqual(headers.get('x-eis-message-hash'), expected_hash)

    @override_settings(
        MRA_EIS_ACCESS_KEY='',
        MRA_EIS_IS_LIVE=True,
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_production_activation_call_matches_swagger_without_access_key(self, mocked_request):
        class SuccessResponse:
            ok = True
            status_code = 200
            reason = 'OK'
            content = b'{"statusCode":1,"data":{"terminalId":"MRA-TERM-CONTRACT-001"}}'
            text = '{"statusCode":1,"data":{"terminalId":"MRA-TERM-CONTRACT-001"}}'
            headers = {}

            @staticmethod
            def json():
                return {'statusCode': 1, 'data': {'terminalId': 'MRA-TERM-CONTRACT-001'}}

        mocked_request.return_value = SuccessResponse()
        client = MRAEISClient(terminal=self.terminal)

        client.call(
            'activate_terminal',
            payload={'terminalActivationCode': 'TEST-TAC'},
            method='POST',
            mutating=True,
        )

        request_headers = mocked_request.call_args.kwargs['headers']
        self.assertNotIn('x-access-key', request_headers)
        self.assertNotIn('Authorization', request_headers)

    @override_settings(
        MRA_EIS_ACCESS_KEY='',
        MRA_EIS_IS_LIVE=False,
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_test_mode_activation_call_matches_swagger_without_access_key(self, mocked_request):
        class SuccessResponse:
            ok = True
            status_code = 200
            reason = 'OK'
            content = b'{"statusCode":1,"data":{"terminalId":"MRA-TERM-CONTRACT-001"}}'
            text = '{"statusCode":1,"data":{"terminalId":"MRA-TERM-CONTRACT-001"}}'
            headers = {}

            @staticmethod
            def json():
                return {'statusCode': 1, 'data': {'terminalId': 'MRA-TERM-CONTRACT-001'}}

        mocked_request.return_value = SuccessResponse()
        client = MRAEISClient(terminal=self.terminal)

        client.call(
            'activate_terminal',
            payload={'terminalActivationCode': 'TEST-TAC'},
            method='POST',
            mutating=True,
        )

        request_headers = mocked_request.call_args.kwargs['headers']
        self.assertNotIn('x-access-key', request_headers)
        self.assertNotIn('Authorization', request_headers)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_live_sale_call_requires_bearer_token_before_http(self, mocked_request):
        self.terminal.mra_token = ''
        client = MRAEISClient(terminal=self.terminal)

        with self.assertRaises(MRAIntegrationError) as context:
            client.call(
                'report_sale',
                payload={'invoiceHeader': {'invoiceNumber': 'TEST-1'}},
                method='POST',
                mutating=True,
            )

        self.assertIn('Bearer authorization token', str(context.exception))
        mocked_request.assert_not_called()

    @override_settings(
        MRA_EIS_SECRET_KEY='',
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_live_sale_call_requires_terminal_secret_before_http(self, mocked_request):
        self.terminal.mra_api_key = ''
        client = MRAEISClient(terminal=self.terminal)

        with self.assertRaises(MRAIntegrationError) as context:
            client.call(
                'report_sale',
                payload={'invoiceHeader': {'invoiceNumber': 'TEST-1'}},
                method='POST',
                mutating=True,
            )

        self.assertIn('x-eis-message-hash', str(context.exception))
        mocked_request.assert_not_called()

    def test_last_transaction_request_uses_empty_post_body(self):
        class SuccessResponse:
            ok = True
            status_code = 200
            reason = 'OK'
            content = b'{"statusCode":1,"data":{}}'
            text = '{"statusCode":1,"data":{}}'
            headers = {}

            @staticmethod
            def json():
                return {'statusCode': 1, 'data': {}}

        with override_settings(
            MRA_EIS_DRY_RUN=False,
            MRA_EIS_ENABLE_HTTP_CALLS=True,
            MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        ), patch('mra_eis.services.client.requests.request') as mocked_request:
            mocked_request.return_value = SuccessResponse()
            client = MRAEISClient(terminal=self.terminal)
            client.call(
                'get_last_online_transaction',
                payload=None,
                method='POST',
                mutating=False,
                send_json=False,
            )

        request_kwargs = mocked_request.call_args.kwargs
        self.assertEqual(request_kwargs['method'], 'POST')
        self.assertNotIn('json', request_kwargs)
        self.assertNotIn('data', request_kwargs)
        expected_hash = base64.b64encode(
            hmac.new(b'test-key', b'', hashlib.sha512).digest()
        ).decode('utf-8')
        self.assertEqual(request_kwargs['headers'].get('x-eis-message-hash'), expected_hash)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_client_wraps_non_dict_json_response_bodies(self, mocked_request):
        class PlainTextJSONResponse:
            ok = False
            status_code = 400
            reason = 'Bad Request'
            content = b'"plain failure"'
            text = '"plain failure"'
            headers = {}

            @staticmethod
            def json():
                return 'plain failure'

        mocked_request.return_value = PlainTextJSONResponse()
        client = MRAEISClient(terminal=self.terminal)

        with self.assertRaises(MRAIntegrationError) as context:
            client.call(
                'report_sale_offline',
                payload={'invoiceHeader': {}},
                method='POST',
                mutating=True,
            )

        self.assertEqual(context.exception.response_data, {'raw': 'plain failure'})
        self.assertIn('plain failure', str(context.exception))

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_client_marks_terminal_online_after_mra_http_response(self, mocked_request):
        class SuccessResponse:
            ok = True
            status_code = 200
            reason = 'OK'
            content = b'{}'
            text = '{}'
            headers = {}

            @staticmethod
            def json():
                return {}

        self.terminal.is_online = False
        self.terminal.last_sync_at = None
        self.terminal.save(update_fields=['is_online', 'last_sync_at', 'updated_at'])
        mocked_request.return_value = SuccessResponse()

        client = MRAEISClient(terminal=self.terminal)
        client.call('get_latest_config', payload={}, method='POST', mutating=False)

        self.terminal.refresh_from_db()
        self.assertTrue(self.terminal.is_online)
        self.assertIsNotNone(self.terminal.last_sync_at)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch('mra_eis.services.client.requests.request')
    def test_client_marks_terminal_offline_on_network_exception(self, mocked_request):
        self.terminal.is_online = True
        self.terminal.save(update_fields=['is_online', 'updated_at'])
        mocked_request.side_effect = requests.ConnectionError('network down')

        client = MRAEISClient(terminal=self.terminal)
        with self.assertRaises(MRAIntegrationError):
            client.call('get_latest_config', payload={}, method='POST', mutating=False)

        self.terminal.refresh_from_db()
        self.assertFalse(self.terminal.is_online)


class CorrectionServiceContractTests(TestCase):
    """Credit/debit/void corrections should use the official MRA EIS endpoints."""

    def setUp(self):
        self.user = User.objects.create_user(email='corrections@example.com', password='test123')
        self.business = Business.objects.create(
            owner=self.user,
            name='Correction Business',
            tin='2005000001',
            vat_registered=True,
            mra_taxpayer_type='VAT',
        )
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main',
            address='123 Main St',
            city='Lilongwe',
            country='Malawi',
            mra_site_id='SITE-CORR-001',
        )
        BusinessSettings.objects.create(
            business=self.business,
            enable_eis=True,
            block_sales_if_eis_down=True,
        )
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-CORR-001',
            device_serial='DEVICE-CORR-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-CORR-001',
            mra_api_key='test-key',
            mra_token='test-token',
            status='active',
            is_online=True,
        )
        self.order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=1,
            status='Completed',
            payment_method='Cash',
            subtotal=Decimal('100.00'),
            total=Decimal('116.50'),
            tax_rate_value=Decimal('16.50'),
            tax_type='VAT_STANDARD',
            vat_amount=Decimal('16.50'),
            net_amount=Decimal('100.00'),
            gross_amount=Decimal('116.50'),
            fiscal_invoice_number='FISCAL-0001',
            eis_status='SUBMITTED',
            eis_submitted_at=timezone.now(),
        )
        self.inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Test Product',
            category='General',
            item_type='sellable',
            price=Decimal('100.00'),
            stock_units=Decimal('10.000'),
            unit_type='unit',
            status='In Stock',
        )
        MRAProductMapping.objects.create(
            inventory_item=self.inventory_item,
            branch=self.branch,
            mra_product_code='MRA-PROD-001',
            mra_product_name='Test Product',
            mra_tax_type='standard',
            mra_tax_rate=Decimal('16.50'),
            mra_unit_measure='unit',
            tax_calculation_method='exclusive',
            is_approved=True,
            approved_at=timezone.now(),
            mra_synced=True,
            last_synced_at=timezone.now(),
        )
        OrderItem.objects.create(
            order=self.order,
            inventory_item_id=str(self.inventory_item.id),
            name='Test Product',
            quantity=Decimal('1.000'),
            price=Decimal('100.00'),
            mra_product_code='MRA-PROD-001',
            tax_rate=Decimal('16.50'),
            tax_type='standard',
            subtotal=Decimal('100.00'),
            tax_amount=Decimal('16.50'),
            total=Decimal('116.50'),
        )

    def assert_adjustment_payload_matches_swagger(self, payload):
        request_keys = {'invoiceHeader', 'invoiceLineItems', 'invoiceSummary', 'reasonForAdjustment'}
        header_keys = {
            'invoiceNumber',
            'invoiceDateTime',
            'sellerTIN',
            'buyerTIN',
            'buyerName',
            'buyerAuthorizationCode',
            'siteId',
            'globalConfigVersion',
            'taxpayerConfigVersion',
            'terminalConfigVersion',
            'isExport',
            'isReliefSupply',
            'vat5CertificateDetails',
            'paymentMethod',
        }
        line_item_keys = {
            'id',
            'productCode',
            'description',
            'unitPrice',
            'quantity',
            'discount',
            'total',
            'totalVAT',
            'taxRateId',
            'isProduct',
        }
        summary_keys = {
            'taxBreakDown',
            'levyBreakDown',
            'totalVAT',
            'offlineSignature',
            'invoiceTotal',
            'amountTendered',
        }
        disallowed_request_keys = {
            'creditNoteNumber',
            'debitNoteNumber',
            'noteNumber',
            'noteType',
            'originalInvoiceNumber',
            'originalReceiptNumber',
            'receiptNumber',
            'handyPosMetadata',
        }
        self.assertLessEqual(set(payload), request_keys)
        self.assertFalse(disallowed_request_keys.intersection(payload))
        self.assertLessEqual(set(payload['invoiceHeader']), header_keys)
        self.assertLessEqual(set(payload['invoiceSummary']), summary_keys)
        self.assertGreater(len(payload['invoiceLineItems']), 0)
        for line_item in payload['invoiceLineItems']:
            self.assertLessEqual(set(line_item), line_item_keys)
        self.assertEqual(payload['invoiceHeader']['invoiceNumber'], self.order.fiscal_invoice_number)

    def test_credit_note_uses_process_credit_debit_note_endpoint(self):
        credit_note = CreditNote.objects.create(
            business=self.business,
            branch=self.branch,
            original_order=self.order,
            credit_note_number='CN-TEST-001',
            reason='refund',
            description='Customer returned part of the sale',
            credit_amount=Decimal('20.00'),
            vat_amount=Decimal('3.30'),
            total_credit=Decimal('23.30'),
            created_by=self.user,
        )

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/sales/process-credit-debit-note',
                data={
                    'data': {
                        'invoiceNumber': 'CN-FISCAL-001',
                        'originalInvoiceNumber': 'FISCAL-0001',
                        'noteType': 'CreditNote',
                        'validationUrl': 'https://validate/credit',
                    }
                },
            )
            result = CorrectionService.submit_credit_note(credit_note)

        self.assertEqual(mocked_call.call_args.args[0], 'process_credit_debit_note')
        payload = mocked_call.call_args.kwargs['payload']
        self.assert_adjustment_payload_matches_swagger(payload)
        self.assertEqual(payload['invoiceHeader']['invoiceNumber'], 'FISCAL-0001')
        self.assertIn('reasonForAdjustment', payload)
        self.assertEqual(payload['invoiceSummary']['invoiceTotal'], 93.2)
        self.assertEqual(result['eis_status'], 'SUBMITTED')
        self.assertEqual(result['original_invoice_number'], 'FISCAL-0001')
        self.assertEqual(result['note_type'], 'CreditNote')
        credit_note.refresh_from_db()
        self.assertEqual(credit_note.fiscal_credit_number, 'CN-FISCAL-001')
        self.assertTrue(credit_note.is_fiscal_locked)

    def test_debit_note_uses_process_credit_debit_note_endpoint(self):
        debit_note = DebitNote.objects.create(
            business=self.business,
            branch=self.branch,
            original_order=self.order,
            debit_note_number='DN-TEST-001',
            description='Undercharge correction',
            additional_amount=Decimal('10.00'),
            vat_amount=Decimal('1.65'),
            total_debit=Decimal('11.65'),
            created_by=self.user,
        )

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/sales/process-credit-debit-note',
                data={
                    'data': {
                        'invoiceNumber': 'DN-FISCAL-001',
                        'originalInvoiceNumber': 'FISCAL-0001',
                        'noteType': 'DebitNote',
                        'validationUrl': 'https://validate/debit',
                    }
                },
            )
            result = CorrectionService.submit_debit_note(debit_note)

        self.assertEqual(mocked_call.call_args.args[0], 'process_credit_debit_note')
        payload = mocked_call.call_args.kwargs['payload']
        self.assert_adjustment_payload_matches_swagger(payload)
        self.assertEqual(payload['invoiceHeader']['invoiceNumber'], 'FISCAL-0001')
        self.assertEqual(payload['invoiceSummary']['invoiceTotal'], 128.15)
        self.assertEqual(result['eis_status'], 'SUBMITTED')
        self.assertEqual(result['original_invoice_number'], 'FISCAL-0001')
        self.assertEqual(result['note_type'], 'DebitNote')
        debit_note.refresh_from_db()
        self.assertEqual(debit_note.fiscal_debit_number, 'DN-FISCAL-001')
        self.assertTrue(debit_note.is_fiscal_locked)

    def test_void_transaction_uses_cancel_receipt_endpoint(self):
        void_transaction = VoidTransaction.objects.create(
            business=self.business,
            branch=self.branch,
            original_order=self.order,
            void_number='VOID-TEST-001',
            void_reason='customer_request',
            reason_description='Customer cancelled the fiscal receipt',
            supporting_documents=[
                'RETURN-SLIP-001',
                {'documentNumber': 'MANAGER-APPROVAL-9', 'documentType': 'approval'},
            ],
            voided_amount=Decimal('100.00'),
            voided_vat=Decimal('16.50'),
            refund_method='cash',
            refund_amount=Decimal('116.50'),
            refund_processed=True,
            refund_processed_at=timezone.now(),
            refund_processed_by=self.user,
            created_by=self.user,
        )
        calls = []

        def fake_call(endpoint_key, payload=None, **kwargs):
            calls.append((endpoint_key, payload, kwargs))
            if endpoint_key == 'cancel_receipt':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/cancel-receipt',
                    data={'data': {'invoiceNumber': 'VOID-FISCAL-001', 'approvalStatus': 'Approved'}},
                )
            if endpoint_key == 'get_stock_adjustment_reasons':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/stock/getStockAdjustmentReasons',
                    data={'data': [{'name': 'Stock Increase'}, {'name': 'Stock Decrease'}]},
                )
            if endpoint_key == 'submit_stock_adjustment':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/stock/submit-adjustment',
                    data={'statusCode': 1, 'remark': 'Success', 'data': None, 'errors': []},
                )
            self.fail(f'Unexpected MRA endpoint call: {endpoint_key}')

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=fake_call):
            result = CorrectionService.submit_void_transaction(void_transaction)

        self.assertEqual([call[0] for call in calls], [
            'cancel_receipt',
            'get_stock_adjustment_reasons',
            'submit_stock_adjustment',
        ])
        payload = calls[0][1]
        self.assertEqual(payload['receiptNumber'], 'FISCAL-0001')
        self.assertEqual(payload['reason'], 'Customer cancelled the fiscal receipt')
        self.assertIsInstance(payload['supportingDocuments'], str)
        decoded_supporting_document = base64.b64decode(payload['supportingDocuments']).decode('utf-8')
        self.assertIn('RETURN-SLIP-001', decoded_supporting_document)
        self.assertIn('MANAGER-APPROVAL-9', decoded_supporting_document)
        stock_payload = calls[2][1]
        self.assertEqual(stock_payload['barcode'], 'MRA-PROD-001')
        self.assertEqual(stock_payload['quantity'], 1.0)
        self.assertEqual(stock_payload['adjustmentType'], 'Increase')
        self.assertEqual(stock_payload['adjustmentReason'], 'Stock Increase')
        self.assertIn('Void receipt FISCAL-0001', stock_payload['taxpayerRemarks'])
        self.assertEqual(result['eis_status'], 'ACCEPTED')
        self.assertEqual(len(result['stock_adjustments']), 1)
        void_transaction.refresh_from_db()
        self.assertEqual(void_transaction.fiscal_void_number, 'VOID-FISCAL-001')
        self.assertTrue(void_transaction.is_fiscal_locked)
        self.assertIn('stock_adjustments', void_transaction.qr_code_payload)


    def test_credit_note_retryable_failure_is_queued_pending(self):
        credit_note = CreditNote.objects.create(
            business=self.business,
            branch=self.branch,
            original_order=self.order,
            credit_note_number='CN-RETRY-001',
            reason='refund',
            description='Customer returned part of the sale',
            credit_amount=Decimal('20.00'),
            vat_amount=Decimal('3.30'),
            total_credit=Decimal('23.30'),
            created_by=self.user,
        )
        failure = MRAIntegrationError(
            'MRA request failed (process_credit_debit_note): 500 Internal Server Error',
            status_code=500,
            endpoint='/api/v1/sales/process-credit-debit-note',
            endpoint_key='process_credit_debit_note',
        )

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=failure):
            result = CorrectionService.submit_credit_note(credit_note)

        self.assertTrue(result['queued'])
        self.assertEqual(result['eis_status'], 'PENDING')
        retry = SyncRetryQueue.objects.get(
            operation_type='submit_credit_note',
            payload={'credit_note_id': str(credit_note.id)},
        )
        self.assertEqual(retry.status, 'pending')
        self.assertIn('500 Internal Server Error', retry.last_error)
        api_error = MRAAPIError.objects.get(terminal=self.terminal)
        self.assertEqual(api_error.error_type, 'server_error')
        self.assertEqual(api_error.error_code, '500')
        credit_note.refresh_from_db()
        self.assertEqual(credit_note.eis_status, 'PENDING')
        self.assertTrue(credit_note.is_dirty)
        self.assertFalse(credit_note.is_fiscal_locked)
        self.assertIn(str(retry.id), credit_note.qr_code_payload)

    def test_debit_note_bad_request_failure_is_not_queued(self):
        debit_note = DebitNote.objects.create(
            business=self.business,
            branch=self.branch,
            original_order=self.order,
            debit_note_number='DN-BAD-REQUEST-001',
            description='Undercharge correction',
            additional_amount=Decimal('10.00'),
            vat_amount=Decimal('1.65'),
            total_debit=Decimal('11.65'),
            created_by=self.user,
        )
        failure = MRAIntegrationError(
            'MRA request failed (process_credit_debit_note): 400 Bad Request',
            status_code=400,
            endpoint='/api/v1/sales/process-credit-debit-note',
            endpoint_key='process_credit_debit_note',
        )

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=failure):
            with self.assertRaises(MRAIntegrationError):
                CorrectionService.submit_debit_note(debit_note)

        self.assertFalse(SyncRetryQueue.objects.filter(operation_type='submit_debit_note').exists())
        api_error = MRAAPIError.objects.get(terminal=self.terminal)
        self.assertEqual(api_error.error_type, 'invalid_request')
        self.assertEqual(api_error.error_code, '400')
        debit_note.refresh_from_db()
        self.assertEqual(debit_note.eis_status, 'PENDING')
        self.assertTrue(debit_note.is_dirty)
        self.assertFalse(debit_note.is_fiscal_locked)


    def test_retry_worker_does_not_create_duplicate_correction_jobs(self):
        credit_note = CreditNote.objects.create(
            business=self.business,
            branch=self.branch,
            original_order=self.order,
            credit_note_number='CN-REPLAY-001',
            reason='refund',
            description='Customer returned part of the sale',
            credit_amount=Decimal('20.00'),
            vat_amount=Decimal('3.30'),
            total_credit=Decimal('23.30'),
            created_by=self.user,
        )
        retry = RetryService.queue_retry(
            self.terminal,
            'submit_credit_note',
            {'credit_note_id': str(credit_note.id)},
        )
        failure = MRAIntegrationError(
            'MRA request failed (process_credit_debit_note): 500 Internal Server Error',
            status_code=500,
            endpoint='/api/v1/sales/process-credit-debit-note',
            endpoint_key='process_credit_debit_note',
        )

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=failure):
            RetryService.process_retry_queue()

        retries = SyncRetryQueue.objects.filter(
            operation_type='submit_credit_note',
            payload={'credit_note_id': str(credit_note.id)},
        )
        self.assertEqual(retries.count(), 1)
        retry.refresh_from_db()
        self.assertEqual(retry.status, 'pending')
        self.assertEqual(retry.attempt_count, 1)
        self.assertIn('500 Internal Server Error', retry.last_error)

    def test_void_retryable_failure_is_queued_pending_without_eis_stock_adjustment(self):
        void_transaction = VoidTransaction.objects.create(
            business=self.business,
            branch=self.branch,
            original_order=self.order,
            void_number='VOID-RETRY-001',
            void_reason='customer_request',
            reason_description='Customer cancelled the fiscal receipt',
            voided_amount=Decimal('100.00'),
            voided_vat=Decimal('16.50'),
            refund_method='cash',
            refund_amount=Decimal('116.50'),
            refund_processed=True,
            refund_processed_at=timezone.now(),
            refund_processed_by=self.user,
            created_by=self.user,
        )
        failure = MRAIntegrationError(
            'MRA request failed (cancel_receipt): 503 Service Unavailable',
            status_code=503,
            endpoint='/api/v1/sales/cancel-receipt',
            endpoint_key='cancel_receipt',
        )

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=failure) as mocked_call:
            result = CorrectionService.submit_void_transaction(void_transaction)

        self.assertEqual(mocked_call.call_count, 1)
        self.assertTrue(result['queued'])
        self.assertEqual(result['stock_adjustments'], [])
        retry = SyncRetryQueue.objects.get(
            operation_type='submit_void_transaction',
            payload={'void_transaction_id': str(void_transaction.id)},
        )
        self.assertEqual(retry.status, 'pending')
        self.assertIn('503 Service Unavailable', retry.last_error)
        api_error = MRAAPIError.objects.get(terminal=self.terminal)
        self.assertEqual(api_error.error_type, 'server_error')
        self.assertEqual(api_error.error_code, '503')
        void_transaction.refresh_from_db()
        self.assertEqual(void_transaction.eis_status, 'PENDING')
        self.assertTrue(void_transaction.is_dirty)
        self.assertFalse(void_transaction.is_fiscal_locked)
        self.assertIn(str(retry.id), void_transaction.qr_code_payload)


class ProductMappingTests(TestCase):
    """Test product mapping"""

    def setUp(self):
        self.user = User.objects.create_user(email='test@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Test Business')
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main',
            address='123 Main St',
            city='Lilongwe',
            country='Malawi',
        )
        self.inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Coca Cola 500ml',
            category='Beverages',
            item_type='sellable',
            price=Decimal('2500.00'),
            stock_units=Decimal('24.000'),
            unit_type='unit',
            status='In Stock',
        )

    def test_product_mapping_creation(self):
        """Test product mapping creation"""
        mapping = ProductMappingService.create_product_mapping(
            business=self.business,
            inventory_item_id=str(self.inventory_item.id),
            product_name='Coca Cola 500ml',
            mra_product_code='BEVERAGE-001',
            mra_product_name='Soft Drink',
            tax_category='standard',
            approved_price=Decimal('2500.00'),
            tax_rate=Decimal('16.50')
        )

        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.mra_product_code, 'BEVERAGE-001')
        self.assertTrue(mapping.is_approved)

    def test_product_validation_for_sale(self):
        """Test product validation for sale"""
        mapping = ProductMappingService.create_product_mapping(
            business=self.business,
            inventory_item_id=str(self.inventory_item.id),
            product_name='Coca Cola 500ml',
            mra_product_code='BEVERAGE-001',
            mra_product_name='Soft Drink',
            tax_category='standard',
            approved_price=Decimal('2500.00'),
            tax_rate=Decimal('16.50')
        )

        # Should validate successfully
        validated = ProductMappingService.validate_product_for_sale(
            self.business,
            str(self.inventory_item.id)
        )
        self.assertEqual(validated.mra_product_code, 'BEVERAGE-001')

    def test_unapproved_product_rejected(self):
        """Test unapproved product is rejected"""
        with self.assertRaises(ValueError):
            ProductMappingService.validate_product_for_sale(
                self.business,
                'item-nonexistent'
            )

    def test_initial_inventory_payload_matches_swagger_camel_case(self):
        """Initial inventory upload should emit the live Swagger field names."""
        payload = ProductMappingService.build_initial_inventory_payload(
            tin='2005000001',
            is_last_batch=True,
            products=[
                {
                    'BarCode': 'SKU-001',
                    'ProductName': 'Coca Cola 500ml',
                    'ProductDescription': 'Soft drink',
                    'QuantityInStock': '24',
                    'UnitPrice': '2500.00',
                    'CostPrice': '1800.00',
                    'SellingPrice': '2500.00',
                }
            ],
        )

        self.assertEqual(set(payload.keys()), {'tin', 'isLastBatch', 'products'})
        self.assertEqual(payload['tin'], '2005000001')
        self.assertTrue(payload['isLastBatch'])
        self.assertEqual(payload['products'][0]['barCode'], 'SKU-001')
        self.assertEqual(payload['products'][0]['quantityInStock'], 24.0)
        self.assertIsNone(payload['products'][0]['reorderLevel'])

    @patch.object(MRAEISClient, 'call')
    def test_add_product_to_mra_uses_swagger_schema(self, mock_call):
        """MRA add-product should submit only the official Swagger fields."""
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-ADD-PRODUCT-001',
            device_serial='DEVICE-ADD-PRODUCT-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-ADD-PRODUCT-001',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/stock/add-product',
            data={
                'statusCode': 1,
                'remark': 'Product added',
                'data': {
                    'productId': 123,
                    'barcode': '2934309406073',
                    'hsCode': '17049000',
                    'taxRateId': 'A',
                    'name': 'Amazon Big Candy',
                    'description': 'Amazon Big Candy',
                    'uom': 'each',
                },
                'errors': [],
            },
        )

        result = ProductMappingService.add_product_to_mra(
            business=self.business,
            terminal=terminal,
            product={
                'barcode': '2934309406073',
                'hsCode': '17049000',
                'name': 'Amazon Big Candy',
                'description': 'Amazon Big Candy',
                'uom': 'each',
                'ignored': 'not sent',
            },
        )

        self.assertTrue(result['submitted'])
        self.assertTrue(result['requires_pull_from_mra'])
        mock_call.assert_called_once_with(
            'add_product',
            payload={
                'barcode': '2934309406073',
                'hsCode': '17049000',
                'name': 'Amazon Big Candy',
                'description': 'Amazon Big Candy',
                'uom': 'each',
            },
            method='POST',
            mutating=True,
        )

    @patch.object(MRAEISClient, 'call')
    def test_add_product_to_mra_requires_swagger_required_fields(self, mock_call):
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-ADD-PRODUCT-002',
            device_serial='DEVICE-ADD-PRODUCT-002',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-ADD-PRODUCT-002',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )

        with self.assertRaisesMessage(ValueError, 'Missing required MRA product field'):
            ProductMappingService.add_product_to_mra(
                business=self.business,
                terminal=terminal,
                product={'name': 'No HS code'},
            )
        mock_call.assert_not_called()

    @override_settings(MRA_EIS_INITIAL_INVENTORY_BATCH_SIZE=2)
    @patch.object(MRAEISClient, 'call')
    def test_initial_inventory_submission_batches_products_and_marks_only_final_batch(self, mock_call):
        """Large initial inventory uploads should be split before hitting MRA."""
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-INITIAL-BATCH-001',
            device_serial='DEVICE-INITIAL-BATCH-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-INITIAL-BATCH-001',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/utilities/taxpayer-initial-inventory-upload',
            data={'statusCode': 1, 'remark': 'Success', 'data': None, 'errors': []},
        )
        products = [
            {
                'barCode': f'SKU-{index:03d}',
                'productName': f'Product {index}',
                'productDescription': f'Product {index}',
                'quantityInStock': '1',
                'unitPrice': '10.00',
                'costPrice': '8.00',
                'sellingPrice': '10.00',
            }
            for index in range(1, 6)
        ]

        result = ProductMappingService.submit_initial_inventory(
            business=self.business,
            terminal=terminal,
            tin='2005000001',
            products=products,
            is_last_batch=True,
        )

        self.assertEqual(result['product_count'], 5)
        self.assertEqual(result['batch_size'], 2)
        self.assertEqual(result['batch_count'], 3)
        self.assertEqual([batch['product_count'] for batch in result['batches']], [2, 2, 1])
        self.assertEqual([batch['is_last_batch'] for batch in result['batches']], [False, False, True])
        self.assertEqual(mock_call.call_count, 3)
        submitted_payloads = [call.kwargs['payload'] for call in mock_call.call_args_list]
        self.assertEqual([len(payload['products']) for payload in submitted_payloads], [2, 2, 1])
        self.assertEqual([payload['isLastBatch'] for payload in submitted_payloads], [False, False, True])

    def test_import_initial_inventory_creates_pos_item_and_ready_mapping_from_mra_catalog(self):
        """Imported stock is sale-ready only when MRA catalog confirms the product."""
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-IMPORT-001',
            device_serial='DEVICE-IMPORT-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-IMPORT-001',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='terminal_site_products',
            config_version='1',
            config_data={
                'products': [
                    {
                        'barCode': '6000000000012',
                        'productName': 'Fanta 500ml',
                        'taxType': 'standard',
                        'taxRate': '16.5',
                        'unitMeasure': 'unit',
                        'isApproved': True,
                    }
                ]
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )

        result = ProductMappingService.import_initial_inventory_to_pos(
            business=self.business,
            terminal=terminal,
            products=[
                {
                    'BarCode': '6000000000012',
                    'ProductName': 'Fanta 500ml',
                    'ProductDescription': 'Soft drink',
                    'QuantityInStock': '12',
                    'UnitPrice': '1500.00',
                    'CostPrice': '900.00',
                    'SellingPrice': '1500.00',
                    'ReorderLevel': '3',
                }
            ],
        )

        self.assertEqual(result['created'], 1)
        self.assertEqual(result['mra_catalog_matches'], 1)
        self.assertEqual(result['mappings_sale_ready'], 1)
        self.assertEqual(result['mappings_pending'], 0)
        item = InventoryItem.objects.get(name='Fanta 500ml', branch=self.branch)
        self.assertEqual(item.item_type, 'sellable')
        self.assertEqual(item.stock_units, Decimal('12.000'))
        self.assertEqual(item.price, Decimal('1500.00'))
        self.assertTrue(item.on_menu)
        self.assertTrue(item.price_locked)

        mapping = MRAProductMapping.objects.get(inventory_item=item)
        self.assertEqual(mapping.mra_product_code, '6000000000012')
        self.assertTrue(mapping.is_approved)
        self.assertTrue(mapping.mra_synced)
        self.assertIsNotNone(mapping.approved_at)

    def test_import_initial_inventory_updates_existing_pos_item_by_barcode(self):
        """Re-importing uploaded stock should update local stock and reuse the item."""
        self.inventory_item.barcode = '6000000000098'
        self.inventory_item.save(update_fields=['barcode', 'updated_at'])
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-IMPORT-002',
            device_serial='DEVICE-IMPORT-002',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-IMPORT-002',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )

        result = ProductMappingService.import_initial_inventory_to_pos(
            business=self.business,
            terminal=terminal,
            products=[
                {
                    'BarCode': '6000000000098',
                    'ProductName': 'Coca Cola 500ml',
                    'ProductDescription': 'Soft drink',
                    'QuantityInStock': '36',
                    'UnitPrice': '2500.00',
                    'CostPrice': '1800.00',
                    'SellingPrice': '2500.00',
                    'MRATaxType': 'standard',
                    'MRATaxRate': '16.5',
                }
            ],
        )

        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['mappings_sale_ready'], 0)
        self.assertEqual(result['mappings_pending'], 1)
        self.assertEqual(InventoryItem.objects.filter(branch=self.branch, name='Coca Cola 500ml').count(), 1)
        self.inventory_item.refresh_from_db()
        self.assertEqual(self.inventory_item.stock_units, Decimal('36.000'))

        mapping = MRAProductMapping.objects.get(inventory_item=self.inventory_item)
        self.assertEqual(mapping.mra_product_code, '6000000000098')
        self.assertEqual(mapping.mra_tax_rate, Decimal('16.50'))
        self.assertFalse(mapping.is_approved)
        self.assertFalse(mapping.mra_synced)
        with self.assertRaises(ValueError):
            ProductMappingService.validate_product_for_sale(self.business, str(self.inventory_item.id))

    def test_pull_approved_products_creates_inventory_from_mra_site_catalog(self):
        """Approved products from MRA portal should pull into POS inventory."""
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-PULL-001',
            device_serial='DEVICE-PULL-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-PULL-001',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='global_configuration',
            config_version='tax-rates-1',
            config_data={
                'taxrates': [
                    {'id': 'A', 'rate': '16.5'},
                ],
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='terminal_site_products',
            config_version='site-products-1',
            config_data={
                'data': [
                    {
                        'productCode': 'MWK-PROD-001',
                        'productName': 'Approved Portal Product',
                        'description': 'Mapped in MRA portal',
                        'quantity': '15',
                        'unitOfMeasure': 'Bottle',
                        'price': '2750.00',
                        'siteId': 10,
                        'minimumStockLevel': '2',
                        'taxRateId': 'A',
                        'levies': [
                            {'levyTypeId': 'ENV', 'levyRate': '2.50'},
                        ],
                        'isProduct': True,
                    }
                ]
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )

        result = ProductMappingService.pull_approved_products_to_inventory(
            business=self.business,
            terminal=terminal,
            refresh_from_mra=False,
        )

        self.assertEqual(result['product_count'], 1)
        self.assertEqual(result['created'], 1)
        item = InventoryItem.objects.get(product_code='MWK-PROD-001')
        self.assertEqual(item.name, 'Approved Portal Product')
        self.assertEqual(item.stock_units, Decimal('15.000'))
        self.assertEqual(item.price, Decimal('2750.00'))
        self.assertTrue(item.price_locked)
        self.assertTrue(item.tax_locked)

        mapping = MRAProductMapping.objects.get(inventory_item=item)
        self.assertEqual(mapping.mra_product_code, 'MWK-PROD-001')
        self.assertEqual(mapping.mra_tax_rate, Decimal('16.50'))
        self.assertEqual(mapping.mra_levies, [{'levyTypeId': 'ENV', 'levyRate': 2.5}])
        self.assertTrue(mapping.is_approved)
        self.assertTrue(mapping.mra_synced)
        ProductMappingService.validate_product_for_sale(self.business, str(item.id))

    def test_pull_approved_products_is_scoped_to_terminal_branch_site(self):
        """A branch must not import products belonging to another EIS site."""
        self.branch.mra_site_id = 'SITE-BRANCH-A'
        self.branch.mra_branch_code = 'SITE-BRANCH-A'
        self.branch.save(update_fields=['mra_site_id', 'mra_branch_code', 'updated_at'])
        terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-PULL-SITE-001',
            device_serial='DEVICE-PULL-SITE-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-PULL-SITE-001',
            mra_api_key='secret',
            mra_token='token',
            status='active',
        )
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='terminal_site_products',
            config_version='site-products-scoped',
            config_data={
                'data': [
                    {
                        'productCode': 'SITE-A-PRODUCT',
                        'productName': 'Branch A Product',
                        'quantity': '2',
                        'unitOfMeasure': 'each',
                        'price': '100.00',
                        'siteId': 'SITE-BRANCH-A',
                        'isProduct': True,
                    },
                    {
                        'productCode': 'SITE-B-PRODUCT',
                        'productName': 'Branch B Product',
                        'quantity': '3',
                        'unitOfMeasure': 'each',
                        'price': '200.00',
                        'siteId': 'SITE-BRANCH-B',
                        'isProduct': True,
                    },
                ]
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )

        result = ProductMappingService.pull_approved_products_to_inventory(
            business=self.business,
            terminal=terminal,
            refresh_from_mra=False,
        )

        self.assertEqual(result['product_count'], 1)
        self.assertTrue(InventoryItem.objects.filter(product_code='SITE-A-PRODUCT').exists())
        self.assertFalse(InventoryItem.objects.filter(product_code='SITE-B-PRODUCT').exists())


class InvoiceTests(TransactionTestCase):
    """Test invoice creation and submission"""

    def setUp(self):
        self.user = User.objects.create_user(email='test@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Test Business')
        self.branch = Branch.objects.create(business=self.business, name='Main', address='123 Main St', city='Lilongwe', country='Malawi')

        # Create terminal
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-001',
            device_serial='DEVICE-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-001',
            mra_api_key='test-key',
            status='active',
            is_online=True
        )

    def test_invoice_creation(self):
        """Test invoice creation"""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('2'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True
        )

        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.status, 'draft')
        self.assertEqual(invoice.invoice_number, 1)
        self.assertEqual(invoice.net_amount, Decimal('5000.00'))
        self.assertEqual(invoice.tax_amount, Decimal('825.00'))
        self.assertEqual(invoice.gross_amount, Decimal('5825.00'))

    def test_sales_payload_uses_swagger_number_types(self):
        """Sales invoice amounts should be JSON numbers, not numeric strings."""
        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=[
                {
                    'mra_product_code': 'BEVERAGE-001',
                    'name': 'Coca Cola 500ml',
                    'quantity': Decimal('2'),
                    'unit_price': Decimal('2500.00'),
                    'tax_rate': Decimal('16.50'),
                    'tax_category': 'standard',
                    'lineNetAmount': Decimal('5000.00'),
                    'lineTaxAmount': Decimal('825.00'),
                }
            ],
            is_online=True,
        )

        payload = InvoiceService._build_mra_invoice_payload(invoice)
        line_item = payload['invoiceLineItems'][0]
        summary = payload['invoiceSummary']

        self.assertIsInstance(line_item['unitPrice'], float)
        self.assertIsInstance(line_item['quantity'], float)
        self.assertIsInstance(line_item['total'], float)
        self.assertIsInstance(line_item['totalVAT'], float)
        self.assertIsInstance(summary['totalVAT'], float)
        self.assertIsInstance(summary['invoiceTotal'], float)
        self.assertNotIn('offlineSignature', summary)

    def test_invoice_signature_generation(self):
        """Test invoice signature is generated"""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True
        )

        self.assertIsNotNone(invoice.invoice_signature)
        self.assertEqual(len(invoice.invoice_signature), 64)  # SHA256 hex length

    def test_online_invoice_hash_validation_passes(self):
        """Online invoice hash validation should pass for untouched invoice."""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True,
        )

        self.assertTrue(InvoiceService.verify_invoice_hash(invoice))

    def test_online_invoice_hash_validation_detects_tamper(self):
        """Online invoice hash validation should fail if signed content is changed."""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True,
        )

        invoice.items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Tampered Item',
                'quantity': '1.000',
                'unit_price': '2500.00',
                'tax_rate': '16.50',
                'tax_category': 'standard',
            }
        ]
        invoice.save(update_fields=['items', 'updated_at'])

        self.assertFalse(InvoiceService.verify_invoice_hash(invoice))

    def test_offline_invoice_hash_validation_passes(self):
        """Offline invoice hash validation should pass for untouched invoice."""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=False,
        )

        self.assertTrue(InvoiceService.verify_invoice_hash(invoice))

    def test_sequential_invoice_numbering(self):
        """Test invoices are numbered sequentially"""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice1 = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True
        )

        invoice2 = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True
        )

        self.assertEqual(invoice1.invoice_number, 1)
        self.assertEqual(invoice2.invoice_number, 2)

    def test_tax_breakdown(self):
        """Test tax breakdown calculation"""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            },
            {
                'mra_product_code': 'FOOD-001',
                'name': 'Bread',
                'quantity': Decimal('1'),
                'unit_price': Decimal('1000.00'),
                'tax_rate': Decimal('0'),
                'tax_category': 'zero',
            }
        ]

        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True
        )

        self.assertEqual(Decimal(str(invoice.tax_breakdown['standard'])), Decimal('412.50'))
        self.assertEqual(Decimal(str(invoice.tax_breakdown['zero'])), Decimal('0'))


class OfflineInvoiceTests(TransactionTestCase):
    """Test offline invoice queuing and sync"""

    def setUp(self):
        self.user = User.objects.create_user(email='test@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Test Business')
        self.branch = Branch.objects.create(business=self.business, name='Main', address='123 Main St', city='Lilongwe', country='Malawi')

        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-001',
            device_serial='DEVICE-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-001',
            mra_api_key='test-key',
            status='active',
            is_online=False
        )

    def test_offline_invoice_queuing(self):
        """Test offline invoice is queued"""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=False
        )

        queue_entry = InvoiceService.queue_offline_invoice(invoice)

        self.assertIsNotNone(queue_entry)
        self.assertEqual(queue_entry.status, 'queued')
        self.assertEqual(queue_entry.queue_position, 1)
        self.assertEqual(invoice.status, 'offline_queued')

    def test_offline_queue_ordering(self):
        """Test offline queue maintains order"""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice1 = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=False
        )
        queue1 = InvoiceService.queue_offline_invoice(invoice1)

        invoice2 = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=False
        )
        queue2 = InvoiceService.queue_offline_invoice(invoice2)

        self.assertEqual(queue1.queue_position, 1)
        self.assertEqual(queue2.queue_position, 2)

    def test_sync_offline_invoices_rejects_expired_offline_transaction(self):
        """Queued offline invoices older than MRA limit should fail before submission."""
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='system_settings',
            config_version='1.0',
            config_data={
                'offlineLimit': {
                    'maxTransactionAgeInHours': 1,
                    'maxCummulativeAmount': '9999999.00',
                }
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )

        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]
        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=False,
        )
        queue_entry = InvoiceService.queue_offline_invoice(invoice)
        invoice.invoice_date = timezone.now() - timedelta(hours=2)
        invoice.save(update_fields=['invoice_date'])

        self.terminal.is_online = True
        self.terminal.save(update_fields=['is_online'])
        result = InvoiceService.sync_offline_invoices(self.terminal)

        queue_entry.refresh_from_db()
        self.assertEqual(result['synced'], 0)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['expired'], 1)
        self.assertEqual(queue_entry.status, 'expired')
        self.assertIn('age exceeds configured limit', queue_entry.last_sync_error.lower())

        second_result = InvoiceService.sync_offline_invoices(self.terminal)
        self.assertEqual(second_result['synced'], 0)
        self.assertEqual(second_result['failed'], 0)
        self.assertEqual(second_result['expired'], 0)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sync_offline_invoices_blocks_remote_sequence_mismatch(self, mock_call):
        """Replay must not submit when MRA last-offline sequence disagrees with local queue."""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]
        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=False,
        )
        queue_entry = InvoiceService.queue_offline_invoice(invoice)
        self.terminal.offline_invoice_counter = 5
        self.terminal.save(update_fields=['offline_invoice_counter', 'updated_at'])
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/sales/last-submitted-offline-transaction',
            data={
                'statusCode': 1,
                'data': {
                    'invoiceHeader': {'invoiceNumber': 'A-A-A-C'},
                },
                'errors': [],
            },
        )

        result = InvoiceService.sync_offline_invoices(self.terminal)

        queue_entry.refresh_from_db()
        invoice.refresh_from_db()
        self.assertTrue(result['blocked'])
        self.assertEqual(result['synced'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(queue_entry.status, 'failed')
        self.assertIn('expected next offline sequence 3', queue_entry.last_sync_error)
        self.assertEqual(invoice.status, 'offline_queued')
        mock_call.assert_called_once_with(
            'get_last_offline_transaction',
            payload=None,
            method='POST',
            mutating=False,
        )
        audit = OfflineAuditLog.objects.filter(
            terminal=self.terminal,
            event_type='sync_failed',
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.details['reason'], 'offline_replay_sequence_guard')

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_sync_offline_invoices_blocks_local_queue_sequence_gap(self, mock_call):
        """Replay must not submit when queued offline invoices are not contiguous."""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]
        invoice1 = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=False,
        )
        queue1 = InvoiceService.queue_offline_invoice(invoice1)
        invoice2 = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=False,
        )
        queue2 = InvoiceService.queue_offline_invoice(invoice2)
        invoice2.invoice_number = 3
        invoice2.save(update_fields=['invoice_number', 'updated_at'])
        self.terminal.offline_invoice_counter = 3
        self.terminal.save(update_fields=['offline_invoice_counter', 'updated_at'])
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/sales/last-submitted-offline-transaction',
            data={'statusCode': 1, 'data': {}, 'errors': []},
        )

        result = InvoiceService.sync_offline_invoices(self.terminal)

        queue1.refresh_from_db()
        queue2.refresh_from_db()
        self.assertTrue(result['blocked'])
        self.assertEqual(result['synced'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(queue1.status, 'failed')
        self.assertEqual(queue2.status, 'queued')
        self.assertIn('position 2', result['error'])
        mock_call.assert_called_once_with(
            'get_last_offline_transaction',
            payload=None,
            method='POST',
            mutating=False,
        )


class POSOfflineComplianceTests(TransactionTestCase):
    """Test MRA offline compliance rules on POS order submission flow."""

    def setUp(self):
        self.user = User.objects.create_user(email='pos@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='POS Compliance Business')
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main',
            address='123 Main St',
            city='Lilongwe',
            country='Malawi',
        )
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-POS-001',
            device_serial='DEVICE-POS-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-POS-001',
            mra_api_key='test-terminal-secret',
            status='active',
            is_online=False,
        )
        self._create_fresh_sales_configurations()
        TerminalService.record_server_time_sync(
            self.terminal,
            server_time=timezone.now(),
            checked_at=timezone.now(),
            source='test_setup',
        )

    def _create_fresh_sales_configurations(
        self,
        *,
        offline_limit: dict | None = None,
        global_configuration: dict | None = None,
        taxpayer_configuration: dict | None = None,
    ):
        now = timezone.now()
        offline_limit = offline_limit or {
            'maxTransactionAgeInHours': 72,
            'maxCummulativeAmount': '3000000.00',
        }
        global_configuration = global_configuration or {
            'versionNo': 1,
            'taxrates': [
                {'id': 'NRT', 'name': 'Non Rated', 'rate': 0},
            ],
        }
        terminal_configuration = {
            'versionNo': 1,
            'terminalSite': {'siteId': 'SITE-POS-001', 'siteName': 'Main'},
            'offlineLimit': offline_limit,
        }
        taxpayer_configuration = taxpayer_configuration or {
            'versionNo': 1,
            'tin': '70267581',
            'isVATRegistered': False,
            'activatedTaxRateIds': ['NRT'],
        }
        MRAConfiguration.objects.update_or_create(
            business=self.business,
            config_type='system_settings',
            config_version='fresh-sales-config',
            defaults={
                'config_data': {
                    'globalConfiguration': global_configuration,
                    'terminalConfiguration': terminal_configuration,
                    'taxpayerConfiguration': taxpayer_configuration,
                },
                'effective_from': now,
                'fetched_from_mra_at': now,
                'is_active': True,
            },
        )
        for config_type, config_data in [
            ('global_configuration', global_configuration),
            ('terminal_configuration', terminal_configuration),
            ('taxpayer_configuration', taxpayer_configuration),
        ]:
            MRAConfiguration.objects.update_or_create(
                business=self.business,
                config_type=config_type,
                config_version='fresh-sales-config',
                defaults={
                    'config_data': config_data,
                    'effective_from': now,
                    'fetched_from_mra_at': now,
                    'is_active': True,
                },
            )

    def _create_pos_order(self, *, order_number: int, amount: Decimal):
        from pos_sessions.models import Order, OrderItem

        inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name=f'Test Item {order_number}',
            category='General',
            item_type='sellable',
            price=amount,
            stock_units=Decimal('10.000'),
            unit_type='unit',
            status='In Stock',
        )
        MRAProductMapping.objects.create(
            inventory_item=inventory_item,
            branch=self.branch,
            mra_product_code=f'MRA-ITEM-{order_number}',
            mra_product_name=inventory_item.name,
            mra_tax_type='zero',
            mra_tax_rate=Decimal('0.00'),
            mra_unit_measure='unit',
            tax_calculation_method='inclusive',
            is_approved=True,
            approved_at=timezone.now(),
            mra_synced=True,
            last_synced_at=timezone.now(),
        )
        order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=order_number,
            status='Completed',
            payment_method='Cash',
            subtotal=amount,
            total=amount,
            net_amount=amount,
            vat_amount=Decimal('0'),
            gross_amount=amount,
        )
        OrderItem.objects.create(
            order=order,
            inventory_item_id=str(inventory_item.id),
            name=inventory_item.name,
            quantity=Decimal('1'),
            price=amount,
            subtotal=amount,
            tax_amount=Decimal('0'),
            total=amount,
        )
        return order

    @override_settings(MRA_EIS_ENFORCE_TERMINAL_DEVICE_BINDING=True)
    def test_device_binding_blocks_unactivated_device_sale(self):
        order = self._create_pos_order(order_number=3010, amount=Decimal('100.00'))

        with self.assertRaisesMessage(MRAIntegrationError, 'not activated as an MRA EIS terminal'):
            POSOrderSubmissionService.prepare_pos_order_submission(
                order,
                force_online=True,
                request_device_serial='DEVICE-NOT-ACTIVATED',
                enforce_device_binding=True,
            )

    @override_settings(MRA_EIS_ENFORCE_TERMINAL_DEVICE_BINDING=True)
    def test_device_binding_selects_matching_branch_terminal(self):
        second_terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-POS-002',
            device_serial='DEVICE-POS-002',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Android',
            mra_terminal_id='MRA-TERM-POS-002',
            mra_api_key='test-terminal-secret-2',
            mra_token='test-terminal-token-2',
            status='active',
            is_online=True,
        )
        order = self._create_pos_order(order_number=3011, amount=Decimal('100.00'))

        resolved = POSOrderSubmissionService._resolve_order_terminal(
            order,
            request_device_serial='DEVICE-POS-002',
            enforce_device_binding=True,
        )

        self.assertEqual(resolved.id, second_terminal.id)

    def test_fiscal_invoice_number_uses_mra_activation_identity(self):
        """MRA invoice number identity must come from activation, not visible TIN/site ID."""
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(update_fields=['mra_taxpayer_id', 'terminal_position'])

        order = self._create_pos_order(order_number=4999, amount=Decimal('100.00'))

        fiscal_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
            order,
            self.terminal,
            is_online=True,
        )
        parts = fiscal_number.split('-')

        self.assertEqual(parts[0], POSOrderSubmissionService._base10_to_mra_base64(70267581))
        self.assertEqual(parts[1], POSOrderSubmissionService._base10_to_mra_base64(3))
        self.assertEqual(parts[3], POSOrderSubmissionService._base10_to_mra_base64(1))

    def test_online_and_offline_fiscal_numbers_share_daily_sequence(self):
        """Online and offline fiscal numbers must not both use count 1 on the same day."""
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(update_fields=['mra_taxpayer_id', 'terminal_position'])

        online_order = self._create_pos_order(order_number=5101, amount=Decimal('100.00'))
        offline_order = self._create_pos_order(order_number=5102, amount=Decimal('100.00'))

        online_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
            online_order,
            self.terminal,
            is_online=True,
        )
        offline_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
            offline_order,
            self.terminal,
            is_online=False,
        )

        online_parts = online_number.split('-')
        offline_parts = offline_number.split('-')
        julian_date = POSOrderSubmissionService._to_julian_date(online_order.created_at)

        self.assertEqual(online_parts[2], offline_parts[2])
        self.assertEqual(online_parts[3], POSOrderSubmissionService._base10_to_mra_base64(1))
        self.assertEqual(offline_parts[3], POSOrderSubmissionService._base10_to_mra_base64(2))
        self.assertEqual(
            FiscalInvoiceSequence.objects.get(terminal=self.terminal, julian_date=julian_date).last_sequence,
            2,
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_fiscal_sequence_recovers_remote_last_submitted_before_sale(self, mock_call):
        """If MRA is ahead of local DB, the next issued number must follow MRA."""
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.mra_token = 'test-terminal-jwt'
        self.terminal.save(update_fields=['mra_taxpayer_id', 'terminal_position', 'mra_token'])

        order = self._create_pos_order(order_number=5105, amount=Decimal('100.00'))
        julian_date = POSOrderSubmissionService._to_julian_date(order.created_at)
        remote_online_number = '-'.join(
            [
                POSOrderSubmissionService._base10_to_mra_base64(70267581),
                POSOrderSubmissionService._base10_to_mra_base64(3),
                POSOrderSubmissionService._base10_to_mra_base64(julian_date),
                POSOrderSubmissionService._base10_to_mra_base64(4),
            ]
        )
        remote_offline_number = '-'.join(
            [
                POSOrderSubmissionService._base10_to_mra_base64(70267581),
                POSOrderSubmissionService._base10_to_mra_base64(3),
                POSOrderSubmissionService._base10_to_mra_base64(julian_date),
                POSOrderSubmissionService._base10_to_mra_base64(7),
            ]
        )

        def mra_call(endpoint_key, payload=None, **kwargs):
            if endpoint_key == 'get_last_online_transaction':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/last-submitted-online-transaction',
                    data={'statusCode': 1, 'data': {'invoiceHeader': {'invoiceNumber': remote_online_number}}, 'errors': []},
                )
            if endpoint_key == 'get_last_offline_transaction':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/last-submitted-offline-transaction',
                    data={'statusCode': 1, 'data': {'invoiceHeader': {'invoiceNumber': remote_offline_number}}, 'errors': []},
                )
            raise AssertionError(f'Unexpected MRA endpoint {endpoint_key}')

        mock_call.side_effect = mra_call

        fiscal_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
            order,
            self.terminal,
            is_online=True,
        )

        self.assertEqual(
            fiscal_number.rsplit('-', 1)[-1],
            POSOrderSubmissionService._base10_to_mra_base64(8),
        )
        self.assertEqual(
            FiscalInvoiceSequence.objects.get(terminal=self.terminal, julian_date=julian_date).last_sequence,
            8,
        )
        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.online_invoice_counter, 8)
        self.assertEqual(self.terminal.offline_invoice_counter, 8)
        self.assertEqual(
            [call.args[0] for call in mock_call.call_args_list],
            ['get_last_online_transaction', 'get_last_offline_transaction'],
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_fiscal_sequence_recovery_network_failure_uses_local_sequence(self, mock_call):
        """When MRA is unreachable, offline issuing can continue from local sequence."""
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.mra_token = 'test-terminal-jwt'
        self.terminal.save(update_fields=['mra_taxpayer_id', 'terminal_position', 'mra_token'])
        order = self._create_pos_order(order_number=5106, amount=Decimal('100.00'))
        julian_date = POSOrderSubmissionService._to_julian_date(order.created_at)
        mock_call.side_effect = MRAIntegrationError(
            'MRA request failed (get_last_online_transaction): network timeout',
            endpoint_key='get_last_online_transaction',
        )

        fiscal_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
            order,
            self.terminal,
            is_online=True,
        )

        self.assertEqual(
            fiscal_number.rsplit('-', 1)[-1],
            POSOrderSubmissionService._base10_to_mra_base64(1),
        )
        self.assertEqual(
            FiscalInvoiceSequence.objects.get(terminal=self.terminal, julian_date=julian_date).last_sequence,
            1,
        )
        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.online_invoice_counter, 1)
        self.assertEqual(self.terminal.offline_invoice_counter, 1)

    def test_fiscal_sequence_resets_per_julian_date(self):
        """MRA fiscal count is daily, so the next Julian date starts back at 1."""
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(update_fields=['mra_taxpayer_id', 'terminal_position'])

        today_order = self._create_pos_order(order_number=5103, amount=Decimal('100.00'))
        tomorrow_order = self._create_pos_order(order_number=5104, amount=Decimal('100.00'))
        tomorrow_order.created_at = today_order.created_at + timedelta(days=1)
        tomorrow_order.save(update_fields=['created_at'])

        today_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
            today_order,
            self.terminal,
            is_online=True,
        )
        tomorrow_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
            tomorrow_order,
            self.terminal,
            is_online=True,
        )

        today_parts = today_number.split('-')
        tomorrow_parts = tomorrow_number.split('-')

        self.assertNotEqual(today_parts[2], tomorrow_parts[2])
        self.assertEqual(today_parts[3], POSOrderSubmissionService._base10_to_mra_base64(1))
        self.assertEqual(tomorrow_parts[3], POSOrderSubmissionService._base10_to_mra_base64(1))

    def test_fiscal_invoice_number_backfills_identity_from_activation_audit(self):
        """Existing terminals should recover activation identity from their audit log."""
        TerminalAuditLog.objects.create(
            terminal=self.terminal,
            action='activated',
            details={
                'response': {
                    'data': {
                        'activatedTerminal': {
                            'taxpayerId': 11223344,
                            'terminalPosition': 7,
                        }
                    }
                }
            },
        )

        order = self._create_pos_order(order_number=5000, amount=Decimal('100.00'))

        fiscal_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
            order,
            self.terminal,
            is_online=True,
        )
        parts = fiscal_number.split('-')
        self.terminal.refresh_from_db()

        self.assertEqual(parts[0], POSOrderSubmissionService._base10_to_mra_base64(11223344))
        self.assertEqual(parts[1], POSOrderSubmissionService._base10_to_mra_base64(7))
        self.assertEqual(self.terminal.mra_taxpayer_id, 11223344)
        self.assertEqual(self.terminal.terminal_position, 7)

    def test_rejected_pos_order_regenerates_stale_identity_fiscal_number(self):
        """Retries should not keep a rejected number built from old TIN/branch identity."""
        self.terminal.is_online = True
        self.terminal.online_invoice_counter = 1
        self.terminal.save(update_fields=['is_online', 'online_invoice_counter', 'updated_at'])
        TerminalAuditLog.objects.create(
            terminal=self.terminal,
            action='activated',
            details={
                'response': {
                    'data': {
                        'activatedTerminal': {
                            'taxpayerId': 11223344,
                            'terminalPosition': 7,
                        }
                    }
                }
            },
        )
        order = self._create_pos_order(order_number=5005, amount=Decimal('100.00'))
        stale_number = '-'.join(
            [
                POSOrderSubmissionService._base10_to_mra_base64(70267581),
                POSOrderSubmissionService._base10_to_mra_base64(1),
                POSOrderSubmissionService._base10_to_mra_base64(
                    POSOrderSubmissionService._to_julian_date(order.created_at)
                ),
                POSOrderSubmissionService._base10_to_mra_base64(1),
            ]
        )
        order.fiscal_invoice_number = stale_number
        order.eis_status = 'REJECTED'
        order.save(update_fields=['fiscal_invoice_number', 'eis_status', 'updated_at'])

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/sales/submit-sales-transaction',
                data={'data': {'validationURL': 'https://validate/sale/5005'}},
            )
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        parts = order.fiscal_invoice_number.split('-')

        self.assertNotEqual(order.fiscal_invoice_number, stale_number)
        self.assertEqual(parts[0], POSOrderSubmissionService._base10_to_mra_base64(11223344))
        self.assertEqual(parts[1], POSOrderSubmissionService._base10_to_mra_base64(7))
        self.assertEqual(parts[3], POSOrderSubmissionService._base10_to_mra_base64(1))
        self.assertEqual(order.eis_status, 'SUBMITTED')

    def test_offline_cumulative_limit_blocks_new_pos_submission(self):
        """POS offline submission should fail when queue exceeds configured cap."""
        self._create_fresh_sales_configurations(
            offline_limit={
                'maxTransactionAgeInHours': 72,
                'maxCummulativeAmount': '1000.00',
            }
        )

        # Existing queued offline invoice of 900.
        queued_invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='POS Compliance Business',
            items=[
                {
                    'mra_product_code': 'SKU-900',
                    'name': 'Queued Item',
                    'quantity': Decimal('1'),
                    'unit_price': Decimal('900.00'),
                    'tax_rate': Decimal('0'),
                    'tax_category': 'zero',
                }
            ],
            is_online=False,
        )
        InvoiceService.queue_offline_invoice(queued_invoice)

        # New order pushes projected total to 1100 > 1000 cap.
        order = self._create_pos_order(order_number=5001, amount=Decimal('200.00'))
        with self.assertRaises(MRAIntegrationError):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=False)

    def test_offline_limit_failure_does_not_consume_fiscal_sequence(self):
        """Failed preflight must not skip an offline fiscal sequence number."""
        self._create_fresh_sales_configurations(
            offline_limit={
                'maxTransactionAgeInHours': 72,
                'maxCummulativeAmount': '100.00',
            }
        )

        order = self._create_pos_order(order_number=5003, amount=Decimal('200.00'))
        with self.assertRaises(MRAIntegrationError):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=False)

        self.terminal.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.terminal.offline_invoice_counter, 0)
        self.assertFalse(order.fiscal_invoice_number)

    def test_offline_pos_submission_generates_signature_and_queues_invoice(self):
        """Offline POS prepare should attach offline signature and queue invoice for replay."""
        order = self._create_pos_order(order_number=5002, amount=Decimal('300.00'))

        result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=False)
        order.refresh_from_db()

        self.assertTrue(result.get('dry_run'))
        self.assertIsNotNone(result.get('offline_signature'))
        self.assertEqual(order.eis_status, 'PENDING')
        self.assertEqual(order.digital_signature, result.get('offline_signature'))

        mra_invoice = MRAInvoice.objects.filter(
            terminal=self.terminal,
            is_online=False,
            mra_response__order_id=str(order.id),
        ).first()
        self.assertIsNotNone(mra_invoice)
        self.assertEqual(mra_invoice.status, 'offline_queued')
        self.assertEqual(mra_invoice.invoice_signature, result.get('offline_signature'))
        self.assertTrue(OfflineInvoiceQueue.objects.filter(mra_invoice=mra_invoice).exists())
        self.assertTrue(Receipt.objects.filter(mra_invoice=mra_invoice).exists())

    @override_settings(
        MRA_EIS_ALWAYS_OFFLINE_B2C=True,
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=False,
    )
    def test_always_offline_b2c_generates_offline_invoice_without_live_sale_call(self):
        """When enabled, B2C sales issue an offline receipt immediately and queue replay."""
        self.terminal.is_online = True
        self.terminal.mra_token = 'test-terminal-jwt'
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(
            update_fields=[
                'is_online',
                'mra_token',
                'mra_taxpayer_id',
                'terminal_position',
                'updated_at',
            ]
        )
        order = self._create_pos_order(order_number=5004, amount=Decimal('300.00'))

        def mra_call(endpoint_key, payload=None, **kwargs):
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={'statusCode': 1, 'data': {'isBlocked': False}, 'errors': []},
                )
            if endpoint_key == 'get_last_offline_transaction':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/last-submitted-offline-transaction',
                    data={'statusCode': 0, 'remark': 'No offline transaction found.', 'data': None, 'errors': []},
                )
            raise AssertionError(f'Unexpected MRA endpoint {endpoint_key}')

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=mra_call) as mock_call:
            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.terminal.refresh_from_db()
        self.assertEqual(
            [call.args[0] for call in mock_call.call_args_list],
            ['get_terminal_blocking_message'],
        )
        self.assertEqual(result['endpoint'], 'report_sale_offline')
        self.assertEqual(result['response']['reason'], 'b2c_offline_first_enabled')
        self.assertEqual(result['submission_state'], 'offline_queued')
        self.assertTrue(result['queued_offline'])
        self.assertTrue(result['offline_signature'])
        self.assertEqual(order.digital_signature, result['offline_signature'])
        self.assertEqual(order.qr_code_payload, result['offline_validation_url'])
        self.assertEqual(order.eis_status, 'PENDING')
        self.assertEqual(self.terminal.offline_invoice_counter, 1)
        self.assertEqual(self.terminal.online_invoice_counter, 1)

        mra_invoice = MRAInvoice.objects.get(mra_response__order_id=str(order.id))
        self.assertFalse(mra_invoice.is_online)
        self.assertEqual(mra_invoice.status, 'offline_queued')
        self.assertTrue(OfflineInvoiceQueue.objects.filter(mra_invoice=mra_invoice).exists())

    @override_settings(
        MRA_EIS_ALWAYS_OFFLINE_B2C=True,
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=False,
    )
    def test_always_offline_b2c_relief_sale_requires_live_vat5_validation(self):
        """Offline-first B2C must not bypass live VAT5 validation for relief sales."""
        self.terminal.is_online = True
        self.terminal.mra_token = 'test-terminal-jwt'
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(
            update_fields=[
                'is_online',
                'mra_token',
                'mra_taxpayer_id',
                'terminal_position',
                'updated_at',
            ]
        )
        order = self._create_pos_order(order_number=5006, amount=Decimal('300.00'))
        order.is_relief_supply = True
        order.vat5_project_number = 'PRJ-001'
        order.vat5_certificate_number = 'VAT5-001'
        order.vat5_quantity = Decimal('1.000')
        order.save(
            update_fields=[
                'is_relief_supply',
                'vat5_project_number',
                'vat5_certificate_number',
                'vat5_quantity',
                'updated_at',
            ]
        )

        def mra_call(endpoint_key, payload=None, **kwargs):
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={'statusCode': 1, 'data': {'isBlocked': False}, 'errors': []},
                )
            if endpoint_key == 'validate_vat5':
                raise MRAIntegrationError(
                    'MRA request failed (validate_vat5): network connection failed to establish',
                    endpoint_key='validate_vat5',
                )
            raise AssertionError(f'Unexpected MRA endpoint {endpoint_key}')

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=mra_call):
            with self.assertRaisesMessage(MRAIntegrationError, 'Relief sale needs MRA online.'):
                POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.terminal.refresh_from_db()
        self.assertFalse(order.fiscal_invoice_number)
        self.assertEqual(self.terminal.offline_invoice_counter, 0)
        self.assertFalse(MRAInvoice.objects.filter(mra_response__order_id=str(order.id)).exists())

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=False,
    )
    def test_pos_submission_uses_mra_server_time_for_invoice_datetime(self):
        """Fiscal invoice time should come from MRA time sync, not raw order.created_at."""
        self.terminal.is_online = True
        self.terminal.mra_token = 'test-terminal-jwt'
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(
            update_fields=[
                'is_online',
                'mra_token',
                'mra_taxpayer_id',
                'terminal_position',
                'updated_at',
            ]
        )
        synced_server_time = datetime(2026, 6, 21, 9, 30, 0, tzinfo=datetime_timezone.utc)
        TerminalService.record_server_time_sync(
            self.terminal,
            server_time=synced_server_time,
            checked_at=timezone.now(),
            source='test_mra_ping',
        )
        order = self._create_pos_order(order_number=5031, amount=Decimal('250.00'))
        submitted_payloads = []

        def mra_call(endpoint_key, payload=None, **kwargs):
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={'statusCode': 1, 'data': {'isBlocked': False}, 'errors': []},
                )
            if endpoint_key == 'report_sale':
                submitted_payloads.append(payload)
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/submit-sales-transaction',
                    data={'statusCode': 1, 'data': {'validationURL': 'https://validate/5031'}, 'errors': []},
                )
            raise AssertionError(f'Unexpected MRA endpoint {endpoint_key}')

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=mra_call):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        self.assertEqual(len(submitted_payloads), 1)
        invoice_time = datetime.fromisoformat(
            submitted_payloads[0]['invoiceHeader']['invoiceDateTime'].replace('Z', '+00:00')
        )
        self.assertGreaterEqual(invoice_time, synced_server_time)
        self.assertLess(invoice_time - synced_server_time, timedelta(seconds=10))
        self.assertNotEqual(
            submitted_payloads[0]['invoiceHeader']['invoiceDateTime'],
            order.created_at.isoformat(),
        )
        mra_invoice = MRAInvoice.objects.get(mra_response__order_id=str(order.id))
        self.assertEqual(mra_invoice.invoice_date, invoice_time)
        self.assertEqual(
            mra_invoice.mra_response['time_sync']['source'],
            'cached_mra_server_time',
        )

    def test_offline_pos_submission_requires_cached_mra_server_time(self):
        """Offline sales need prior MRA time sync so timestamps are still server-based."""
        TerminalAuditLog.objects.filter(
            terminal=self.terminal,
            details__source='mra_server_time_sync',
        ).delete()
        order = self._create_pos_order(order_number=5032, amount=Decimal('250.00'))

        with self.assertRaisesMessage(MRAIntegrationError, 'MRA server time not synced'):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=False)

        order.refresh_from_db()
        self.assertFalse(order.fiscal_invoice_number)
        self.assertFalse(MRAInvoice.objects.filter(mra_response__order_id=str(order.id)).exists())

    def test_b2b_pos_submission_requires_online_terminal_before_fiscal_number(self):
        """B2B receipts must not be issued through the offline fiscal path."""
        order = self._create_pos_order(order_number=5021, amount=Decimal('300.00'))
        order.buyer_tin = '20162939'
        order.buyer_name = 'Buyer Ltd'
        order.save(update_fields=['buyer_tin', 'buyer_name', 'updated_at'])

        with self.assertRaisesMessage(MRAIntegrationError, 'B2B EIS sales require MRA online confirmation'):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=False)

        order.refresh_from_db()
        self.terminal.refresh_from_db()
        self.assertFalse(order.fiscal_invoice_number)
        self.assertEqual(self.terminal.offline_invoice_counter, 0)
        self.assertFalse(MRAInvoice.objects.filter(mra_response__order_id=str(order.id)).exists())

    @override_settings(
        MRA_EIS_ALWAYS_OFFLINE_B2C=True,
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_VALIDATE_BUYER_TIN_BEFORE_SALE=False,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=False,
    )
    def test_always_offline_b2c_does_not_apply_to_b2b_sales(self):
        """B2B remains online-only even when B2C offline-first mode is enabled."""
        self.terminal.is_online = True
        self.terminal.mra_token = 'test-terminal-jwt'
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(
            update_fields=[
                'is_online',
                'mra_token',
                'mra_taxpayer_id',
                'terminal_position',
                'updated_at',
            ]
        )
        order = self._create_pos_order(order_number=5023, amount=Decimal('300.00'))
        order.buyer_tin = '20162939'
        order.buyer_name = 'Buyer Ltd'
        order.save(update_fields=['buyer_tin', 'buyer_name', 'updated_at'])
        call_log = []

        def mra_call(endpoint_key, payload=None, **kwargs):
            call_log.append(endpoint_key)
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={'statusCode': 1, 'data': {'isBlocked': False}, 'errors': []},
                )
            if endpoint_key == 'report_sale':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/submit-sales-transaction',
                    data={'statusCode': 1, 'data': {'validationURL': 'https://validate/b2b/5023'}, 'errors': []},
                )
            raise AssertionError(f'Unexpected MRA endpoint {endpoint_key}')

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=mra_call):
            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.assertEqual(call_log, ['get_terminal_blocking_message', 'report_sale'])
        self.assertEqual(result['endpoint'], 'report_sale')
        self.assertEqual(result['submission_state'], 'accepted')
        self.assertFalse(result['offline_signature'])
        self.assertEqual(order.eis_status, 'SUBMITTED')
        self.assertFalse(OfflineInvoiceQueue.objects.filter(mra_invoice__mra_response__order_id=str(order.id)).exists())

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_VALIDATE_BUYER_TIN_BEFORE_SALE=False,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=False,
    )
    def test_b2b_network_failure_does_not_issue_offline_receipt(self):
        """A B2B network failure must fail closed instead of fallback-signing offline."""
        self.terminal.is_online = True
        self.terminal.mra_token = 'test-terminal-jwt'
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(
            update_fields=[
                'is_online',
                'mra_token',
                'mra_taxpayer_id',
                'terminal_position',
                'updated_at',
            ]
        )
        order = self._create_pos_order(order_number=5022, amount=Decimal('300.00'))
        order.buyer_tin = '20162939'
        order.buyer_name = 'Buyer Ltd'
        order.save(update_fields=['buyer_tin', 'buyer_name', 'updated_at'])
        call_log = []

        def mra_call(endpoint_key, payload=None, **kwargs):
            call_log.append(endpoint_key)
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={
                        'statusCode': 1,
                        'remark': 'Not blocked',
                        'data': {'isBlocked': False},
                        'errors': [],
                    },
                )
            raise MRAIntegrationError(
                'MRA request failed (report_sale): network connection failed to establish',
                endpoint_key=endpoint_key,
            )

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=mra_call):
            with self.assertRaisesMessage(MRAIntegrationError, 'B2B EIS sales require MRA online confirmation'):
                POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.terminal.refresh_from_db()
        self.assertEqual(call_log, ['get_terminal_blocking_message', 'report_sale'])
        self.assertTrue(self.terminal.is_online)
        self.assertFalse(order.fiscal_invoice_number)
        self.assertEqual(self.terminal.online_invoice_counter, 0)
        self.assertFalse(MRAInvoice.objects.filter(mra_response__order_id=str(order.id)).exists())

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    def test_network_failure_falls_back_to_offline_certification_flow(self):
        """Network-only online failure should issue, print, queue, and replay an offline sale."""
        self.business.tin = '70267581'
        self.business.vat_registered = False
        self.business.mra_taxpayer_type = 'NON_VAT'
        self.business.save(update_fields=['tin', 'vat_registered', 'mra_taxpayer_type', 'updated_at'])
        self.terminal.is_online = True
        self.terminal.mra_token = 'test-terminal-jwt'
        self.terminal.mra_taxpayer_id = 70267581
        self.terminal.terminal_position = 3
        self.terminal.save(
            update_fields=[
                'is_online',
                'mra_token',
                'mra_taxpayer_id',
                'terminal_position',
                'updated_at',
            ]
        )
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='system_settings',
            config_version='offline-cert-config',
            config_data={
                'globalConfiguration': {
                    'versionNo': 1,
                    'taxrates': [
                        {'id': 'NRT', 'name': 'Non Rated', 'rate': 0},
                    ],
                },
                'terminalConfiguration': {
                    'versionNo': 1,
                    'terminalSite': {'siteId': 'SITE-OFFLINE-CERT'},
                    'offlineLimit': {
                        'maxTransactionAgeInHours': 72,
                        'maxCummulativeAmount': '1000000.00',
                    },
                },
                'taxpayerConfiguration': {
                    'versionNo': 1,
                    'tin': '70267581',
                    'isVATRegistered': False,
                    'activatedTaxRateIds': ['NRT'],
                },
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )
        order = self._create_pos_order(order_number=5020, amount=Decimal('250.00'))
        call_log = []
        offline_submission_payloads = []

        def mra_call(endpoint_key, payload=None, **kwargs):
            call_log.append(endpoint_key)
            if endpoint_key == 'get_terminal_blocking_message':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/utilities/get-terminal-blocking-message',
                    data={
                        'statusCode': 1,
                        'remark': 'Not blocked',
                        'data': {'isBlocked': False},
                        'errors': [],
                    },
                )
            if endpoint_key == 'report_sale':
                raise MRAIntegrationError(
                    'MRA request failed (report_sale): network connection failed to establish',
                    endpoint_key='report_sale',
                )
            if endpoint_key == 'get_last_offline_transaction':
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/last-submitted-offline-transaction',
                    data={'statusCode': 1, 'data': {}, 'errors': []},
                )
            if endpoint_key == 'report_sale_offline':
                offline_submission_payloads.append(payload)
                return MRACallResult(
                    ok=True,
                    dry_run=False,
                    status_code=200,
                    endpoint='/api/v1/sales/submit-sales-transaction',
                    data={
                        'statusCode': 1,
                        'remark': 'Success',
                        'data': {
                            'transactionId': 'REMOTE-OFFLINE-5020',
                            'validationURL': 'https://validate/offline/5020',
                        },
                        'errors': [],
                    },
                )
            raise AssertionError(f'Unexpected MRA endpoint {endpoint_key}')

        with patch('mra_eis.services.core.MRAEISClient.call', side_effect=mra_call):
            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

            self.assertEqual(call_log, ['get_terminal_blocking_message', 'report_sale'])
            self.assertTrue(result['dry_run'])
            self.assertEqual(result['endpoint'], 'report_sale_offline')
            self.assertEqual(result['response']['reason'], 'network_offline_fallback')
            self.assertTrue(result['offline_signature'])
            self.assertIn('ReceiptValidation', result['offline_validation_url'])

            order.refresh_from_db()
            self.terminal.refresh_from_db()
            self.assertFalse(self.terminal.is_online)
            self.assertEqual(order.eis_status, 'PENDING')
            self.assertEqual(order.digital_signature, result['offline_signature'])
            self.assertEqual(order.qr_code_payload, result['offline_validation_url'])

            mra_invoice = MRAInvoice.objects.get(
                terminal=self.terminal,
                is_online=False,
                mra_response__order_id=str(order.id),
            )
            queue_entry = OfflineInvoiceQueue.objects.get(mra_invoice=mra_invoice)
            receipt = Receipt.objects.get(mra_invoice=mra_invoice)
            self.assertEqual(queue_entry.status, 'queued')
            self.assertEqual(mra_invoice.status, 'offline_queued')
            self.assertEqual(receipt.qr_code_data, result['offline_validation_url'])
            self.assertIn('offlineSignature', mra_invoice.mra_response['payload']['invoiceSummary'])
            self.assertEqual(
                mra_invoice.mra_response['response']['original_endpoint'],
                'report_sale',
            )

            self.terminal.is_online = True
            self.terminal.save(update_fields=['is_online', 'updated_at'])
            sync_result = InvoiceService.sync_offline_invoices(self.terminal)

        self.assertEqual(sync_result, {'synced': 1, 'failed': 0})
        self.assertEqual(call_log, [
            'get_terminal_blocking_message',
            'report_sale',
            'get_last_offline_transaction',
            'report_sale_offline',
        ])
        self.assertEqual(len(offline_submission_payloads), 1)
        submitted_payload = offline_submission_payloads[0]
        self.assertEqual(submitted_payload['invoiceHeader']['invoiceNumber'], order.fiscal_invoice_number)
        self.assertIn('offlineSignature', submitted_payload['invoiceSummary'])

        order.refresh_from_db()
        mra_invoice.refresh_from_db()
        queue_entry.refresh_from_db()
        self.assertEqual(queue_entry.status, 'synced')
        self.assertEqual(mra_invoice.status, 'offline_synced')
        self.assertEqual(mra_invoice.mra_response['order_id'], str(order.id))
        self.assertEqual(
            mra_invoice.mra_response['local_metadata']['offlineValidationURL'],
            result['offline_validation_url'],
        )
        self.assertEqual(order.eis_status, 'SUBMITTED')
        self.assertTrue(order.is_fiscal_locked)
        self.assertFalse(order.is_dirty)
        self.assertEqual(order.qr_code_payload, 'https://validate/offline/5020')
        self.assertEqual(order.eis_uuid, 'REMOTE-OFFLINE-5020')

    def test_offline_signature_uses_mra_request_amount_text(self):
        """Offline signature params must mirror the numeric amount text sent in JSON."""
        order = self._create_pos_order(order_number=5016, amount=Decimal('250.00'))
        order.fiscal_invoice_number = 'CuQ-D-JY37-U'
        order.save(update_fields=['fiscal_invoice_number', 'updated_at'])

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)
        POSOrderSubmissionService._apply_offline_signature(payload, self.terminal, is_online=False)

        params = payload['handyPosMetadata']['offlineValidationParams']
        self.assertIn('&I=250.0&', params)
        self.assertIn('&V=0.0&', params)

    def test_pos_payload_requires_mra_approved_product_mapping(self):
        """Backend must block fiscal sale payloads when product approval is missing."""
        order = self._create_pos_order(order_number=5004, amount=Decimal('50.00'))
        MRAProductMapping.objects.filter(inventory_item_id=order.items.first().inventory_item_id).update(
            is_approved=False
        )

        with self.assertRaises(MRAIntegrationError):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=False)

        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.offline_invoice_counter, 0)

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    def test_real_mra_sale_requires_terminal_jwt_token(self):
        """Real sale submissions need Authorization from terminal activation."""
        order = self._create_pos_order(order_number=5017, amount=Decimal('50.00'))

        with self.assertRaisesRegex(MRAIntegrationError, 'terminal JWT token'):
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        self.terminal.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.terminal.online_invoice_counter, 0)
        self.assertFalse(order.fiscal_invoice_number)

    def test_non_vat_taxpayer_allows_mra_standard_vat_mapping_for_pos_payload(self):
        """Non-VAT taxpayers still submit using MRA-approved product tax metadata."""
        self.business.tin = '70267581'
        self.business.vat_registered = False
        self.business.mra_taxpayer_type = 'NON_VAT'
        self.business.save(update_fields=['tin', 'vat_registered', 'mra_taxpayer_type', 'updated_at'])
        order = self._create_pos_order(order_number=5009, amount=Decimal('250.00'))
        order.fiscal_invoice_number = 'FISCAL-NON-VAT-001'
        order.save(update_fields=['fiscal_invoice_number', 'updated_at'])
        mapping = MRAProductMapping.objects.get(inventory_item_id=order.items.first().inventory_item_id)
        mapping.mra_tax_type = 'standard'
        mapping.mra_tax_rate = Decimal('17.50')
        mapping.tax_calculation_method = 'inclusive'
        mapping.save(update_fields=['mra_tax_type', 'mra_tax_rate', 'tax_calculation_method', 'updated_at'])

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)
        line = payload['invoiceLineItems'][0]

        self.assertEqual(line['taxRateId'], 'A')
        self.assertGreater(line['totalVAT'], 0)
        mapping.refresh_from_db()
        self.assertEqual(mapping.mra_tax_type, 'standard')
        self.assertEqual(mapping.mra_tax_rate, Decimal('17.50'))

    def test_non_vat_catalog_product_preserves_mra_standard_vat_mapping(self):
        """MRA product refresh must mirror EIS tax instead of local-converting it to zero."""
        product = ProductMappingService._normalize_mra_catalog_product(
            {
                'productCode': '2934309406073',
                'productName': 'Amazon Big Candy',
                'taxRate': 17.5,
                'taxType': 'standard',
                'isActive': True,
            },
            business=self.business,
        )

        self.assertIsNotNone(product)
        self.assertEqual(product['tax_type'], 'standard')
        self.assertEqual(product['tax_rate'], Decimal('17.50'))
        self.assertFalse(product['tax_adjusted_for_non_vat'])

    def test_sale_payload_rejects_local_tax_that_differs_from_mra_site_catalog(self):
        """A local zero override must not submit when EIS still configures the product as standard VAT."""
        self.business.tin = '70267581'
        self.business.vat_registered = False
        self.business.mra_taxpayer_type = 'NON_VAT'
        self.business.save(update_fields=['tin', 'vat_registered', 'mra_taxpayer_type', 'updated_at'])
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='terminal_site_products',
            config_version='standard-catalog',
            config_data={
                'products': [
                    {
                        'productCode': 'MRA-ITEM-5011',
                        'productName': 'Test Item 5011',
                        'taxType': 'standard',
                        'taxRate': 17.5,
                        'siteId': 'SITE-POS-001',
                        'isActive': True,
                    }
                ]
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )
        order = self._create_pos_order(order_number=5011, amount=Decimal('250.00'))
        order.fiscal_invoice_number = 'FISCAL-MISMATCH-001'
        order.save(update_fields=['fiscal_invoice_number', 'updated_at'])
        mapping = MRAProductMapping.objects.get(inventory_item_id=order.items.first().inventory_item_id)
        mapping.mra_tax_type = 'zero'
        mapping.mra_tax_rate = Decimal('0.00')
        mapping.tax_calculation_method = 'inclusive'
        mapping.save(update_fields=['mra_tax_type', 'mra_tax_rate', 'tax_calculation_method', 'updated_at'])

        with self.assertRaisesRegex(MRAIntegrationError, 'configured in EIS as standard VAT'):
            POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)

    def test_pos_payload_omits_blank_optional_buyer_tin_fields(self):
        """Walk-in sales should not send empty optional buyer TIN fields to MRA."""
        self.business.tin = 'LOCAL-TIN'
        self.business.save(update_fields=['tin', 'updated_at'])
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='taxpayer_configuration',
            config_version='1',
            config_data={
                'versionNo': 1,
                'tin': '70267581',
                'isVATRegistered': False,
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )
        order = self._create_pos_order(order_number=5010, amount=Decimal('250.00'))
        order.fiscal_invoice_number = 'FISCAL-ZERO-001'
        order.save(update_fields=['fiscal_invoice_number', 'updated_at'])

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)
        header = payload['invoiceHeader']

        self.assertEqual(header['sellerTIN'], '70267581')
        self.assertNotIn('buyerTIN', header)
        self.assertNotIn('buyerName', header)
        self.assertNotIn('buyerAuthorizationCode', header)

    def test_non_vat_zero_rated_payload_uses_activated_nrt_tax_rate(self):
        """Non-VAT taxpayers should use their activated non-rated tax id, not global VAT B."""
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='system_settings',
            config_version='non-vat-config',
            config_data={
                'globalConfiguration': {
                    'versionNo': 1,
                    'taxrates': [
                        {'id': 'A', 'name': 'Standard Rated', 'rate': 16.5},
                        {'id': 'B', 'name': 'Zero Rated', 'rate': 0},
                        {'id': 'E', 'name': 'Exempt', 'rate': 0},
                    ],
                },
                'terminalConfiguration': {
                    'versionNo': 1,
                    'terminalSite': {'siteId': 'MA1b3373d0-cb85-4ce5-ad1f-99b0cc89ad1b'},
                },
                'taxpayerConfiguration': {
                    'versionNo': 1,
                    'tin': '70267581',
                    'isVATRegistered': False,
                    'activatedTaxRateIds': ['CGT', 'NRT'],
                },
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )
        order = self._create_pos_order(order_number=5012, amount=Decimal('250.00'))
        order.fiscal_invoice_number = 'FISCAL-NRT-001'
        order.save(update_fields=['fiscal_invoice_number', 'updated_at'])

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)

        self.assertEqual(payload['invoiceLineItems'][0]['taxRateId'], 'NRT')
        self.assertEqual(payload['invoiceSummary']['taxBreakDown'][0]['rateId'], 'NRT')

    def test_pos_payload_groups_tax_rates_a_b_and_e(self):
        """VAT taxpayer sales must keep standard, zero-rated, and exempt groups separate."""
        from pos_sessions.models import Order, OrderItem

        self.business.vat_registered = True
        self.business.mra_taxpayer_type = 'VAT'
        self.business.save(update_fields=['vat_registered', 'mra_taxpayer_type', 'updated_at'])
        self._create_fresh_sales_configurations(
            global_configuration={
                'versionNo': 1,
                'taxrates': [
                    {'id': 'A', 'name': 'Standard Rated', 'rate': 16.5},
                    {'id': 'B', 'name': 'Zero Rated', 'rate': 0},
                    {'id': 'E', 'name': 'Exempt', 'rate': 0},
                ],
            },
            taxpayer_configuration={
                'versionNo': 1,
                'tin': '70267581',
                'isVATRegistered': True,
                'activatedTaxRateIds': ['A', 'B', 'E'],
            },
        )

        products = [
            ('Group A Item', 'MRA-GROUP-A', 'standard', Decimal('16.50'), Decimal('116.50')),
            ('Group B Item', 'MRA-GROUP-B', 'zero', Decimal('0.00'), Decimal('50.00')),
            ('Group E Item', 'MRA-GROUP-E', 'exempt', Decimal('0.00'), Decimal('30.00')),
        ]
        order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=5018,
            status='Completed',
            payment_method='Cash',
            subtotal=Decimal('196.50'),
            total=Decimal('196.50'),
            net_amount=Decimal('196.50'),
            vat_amount=Decimal('0.00'),
            gross_amount=Decimal('196.50'),
            fiscal_invoice_number='FISCAL-ABE-001',
        )

        for name, code, tax_type, tax_rate, price in products:
            inventory_item = InventoryItem.objects.create(
                business=self.business,
                branch=self.branch,
                name=name,
                category='General',
                item_type='sellable',
                price=price,
                stock_units=Decimal('5.000'),
                unit_type='unit',
                status='In Stock',
            )
            MRAProductMapping.objects.create(
                inventory_item=inventory_item,
                branch=self.branch,
                mra_product_code=code,
                mra_product_name=name,
                mra_tax_type=tax_type,
                mra_tax_rate=tax_rate,
                mra_unit_measure='unit',
                tax_calculation_method='inclusive',
                is_approved=True,
                approved_at=timezone.now(),
                mra_synced=True,
                last_synced_at=timezone.now(),
            )
            OrderItem.objects.create(
                order=order,
                inventory_item_id=str(inventory_item.id),
                name=name,
                quantity=Decimal('1.000'),
                price=price,
                subtotal=price,
                tax_amount=Decimal('0.00'),
                total=price,
            )

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)
        line_items = payload['invoiceLineItems']
        summary_by_rate = {
            row['rateId']: row
            for row in payload['invoiceSummary']['taxBreakDown']
        }

        self.assertEqual([line['taxRateId'] for line in line_items], ['A', 'B', 'E'])
        self.assertEqual(line_items[0]['total'], 100.0)
        self.assertEqual(line_items[0]['totalVAT'], 16.5)
        self.assertEqual(line_items[1]['total'], 50.0)
        self.assertEqual(line_items[1]['totalVAT'], 0.0)
        self.assertEqual(line_items[2]['total'], 30.0)
        self.assertEqual(line_items[2]['totalVAT'], 0.0)
        self.assertEqual(summary_by_rate['A'], {'rateId': 'A', 'taxableAmount': 100.0, 'taxAmount': 16.5})
        self.assertEqual(summary_by_rate['B'], {'rateId': 'B', 'taxableAmount': 50.0, 'taxAmount': 0.0})
        self.assertEqual(summary_by_rate['E'], {'rateId': 'E', 'taxableAmount': 30.0, 'taxAmount': 0.0})
        self.assertEqual(payload['invoiceSummary']['totalVAT'], 16.5)
        self.assertEqual(payload['invoiceSummary']['invoiceTotal'], 196.5)

    def test_pos_payload_normalizes_payment_method_for_mra(self):
        """MRA expects enum-like payment method tokens such as MobileMoney."""
        order = self._create_pos_order(order_number=5014, amount=Decimal('250.00'))
        order.payment_method = 'Mobile Money'
        order.fiscal_invoice_number = 'FISCAL-PAYMENT-001'
        order.save(update_fields=['payment_method', 'fiscal_invoice_number', 'updated_at'])

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)

        self.assertEqual(payload['invoiceHeader']['paymentMethod'], 'MobileMoney')

    def test_relief_supply_removes_standard_rated_vat_from_pos_payload(self):
        """VAT5 relief should keep the MRA standard tax id but remove charged VAT."""
        self.business.vat_registered = True
        self.business.mra_taxpayer_type = 'VAT'
        self.business.save(update_fields=['vat_registered', 'mra_taxpayer_type', 'updated_at'])
        self._create_fresh_sales_configurations(
            global_configuration={
                'versionNo': 1,
                'taxrates': [
                    {'id': 'A', 'name': 'Standard Rated', 'rate': 17.5},
                    {'id': 'B', 'name': 'Zero Rated', 'rate': 0},
                    {'id': 'E', 'name': 'Exempt', 'rate': 0},
                ],
            },
            taxpayer_configuration={
                'versionNo': 1,
                'tin': '70267581',
                'isVATRegistered': True,
                'activatedTaxRateIds': ['A', 'B', 'E'],
            },
        )
        order = self._create_pos_order(order_number=5023, amount=Decimal('117.50'))
        order.fiscal_invoice_number = 'FISCAL-RELIEF-STANDARD-001'
        order.is_relief_supply = True
        order.vat5_project_number = 'PRJ-001'
        order.vat5_certificate_number = 'VAT5-001'
        order.vat5_quantity = Decimal('1.000')
        order.save(
            update_fields=[
                'fiscal_invoice_number',
                'is_relief_supply',
                'vat5_project_number',
                'vat5_certificate_number',
                'vat5_quantity',
                'updated_at',
            ]
        )
        mapping = MRAProductMapping.objects.get(inventory_item_id=order.items.first().inventory_item_id)
        mapping.mra_tax_type = 'standard'
        mapping.mra_tax_rate = Decimal('17.50')
        mapping.tax_calculation_method = 'inclusive'
        mapping.save(update_fields=['mra_tax_type', 'mra_tax_rate', 'tax_calculation_method', 'updated_at'])

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=True)
        header = payload['invoiceHeader']
        line = payload['invoiceLineItems'][0]
        summary = payload['invoiceSummary']
        metadata_line = payload['handyPosMetadata']['lineSnapshots'][0]

        self.assertTrue(header['isReliefSupply'])
        self.assertEqual(header['vat5CertificateDetails']['certificateNumber'], 'VAT5-001')
        self.assertEqual(line['taxRateId'], 'A')
        self.assertEqual(line['unitPrice'], 100.0)
        self.assertEqual(line['total'], 100.0)
        self.assertEqual(line['totalVAT'], 0.0)
        self.assertEqual(summary['taxBreakDown'], [{'rateId': 'A', 'taxableAmount': 100.0, 'taxAmount': 0.0}])
        self.assertEqual(summary['totalVAT'], 0.0)
        self.assertEqual(summary['invoiceTotal'], 100.0)
        self.assertEqual(summary['amountTendered'], 100.0)
        self.assertTrue(metadata_line['reliefSupplyApplied'])
        self.assertEqual(metadata_line['reliefVATRemoved'], '17.50')

    def test_relief_supply_leaves_zero_rated_pos_lines_unchanged(self):
        """Relief supply should not alter zero/exempt MRA product tax treatment."""
        order = self._create_pos_order(order_number=5024, amount=Decimal('250.00'))
        order.fiscal_invoice_number = 'FISCAL-RELIEF-ZERO-001'
        order.is_relief_supply = True
        order.vat5_project_number = 'PRJ-001'
        order.vat5_certificate_number = 'VAT5-001'
        order.vat5_quantity = Decimal('1.000')
        order.save(
            update_fields=[
                'fiscal_invoice_number',
                'is_relief_supply',
                'vat5_project_number',
                'vat5_certificate_number',
                'vat5_quantity',
                'updated_at',
            ]
        )

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=True)
        line = payload['invoiceLineItems'][0]
        summary = payload['invoiceSummary']
        metadata_line = payload['handyPosMetadata']['lineSnapshots'][0]

        self.assertEqual(line['taxRateId'], 'NRT')
        self.assertEqual(line['unitPrice'], 250.0)
        self.assertEqual(line['total'], 250.0)
        self.assertEqual(line['totalVAT'], 0.0)
        self.assertEqual(summary['invoiceTotal'], 250.0)
        self.assertFalse(metadata_line['reliefSupplyApplied'])
        self.assertEqual(metadata_line['reliefVATRemoved'], '0.00')

    def test_offline_replay_preserves_and_normalizes_pos_header_fields(self):
        """Queued POS invoices should replay with the original compliant header values."""
        invoice = MRAInvoice.objects.create(
            business=self.business,
            branch=self.branch,
            terminal=self.terminal,
            invoice_number=19,
            seller_tin='70267581',
            seller_name=self.business.name,
            buyer_tin='',
            buyer_name='',
            items=[
                {
                    'id': 1,
                    'productCode': '2934309406073',
                    'description': 'Uncategorized | each',
                    'unitPrice': 250.0,
                    'quantity': 1.0,
                    'discount': 0.0,
                    'total': 200.0,
                    'totalVAT': 50.0,
                    'taxRateId': 'A',
                    'isProduct': True,
                }
            ],
            net_amount=Decimal('200.00'),
            tax_amount=Decimal('50.00'),
            gross_amount=Decimal('250.00'),
            tax_breakdown={'byRate': [{'rateId': 'A', 'taxableAmount': 200.0, 'taxAmount': 50.0}]},
            invoice_signature='offline-signature',
            is_online=False,
            invoice_date=timezone.now(),
            status='offline_queued',
            mra_response={
                'payload': {
                    'invoiceHeader': {
                        'invoiceNumber': 'CuQ-D-JY37-T',
                        'invoiceDateTime': '2026-05-18T20:11:09.416025+00:00',
                        'sellerTIN': '70267581',
                        'siteId': 'MA1b3373d0-cb85-4ce5-ad1f-99b0cc89ad1b',
                        'globalConfigVersion': 1,
                        'taxpayerConfigVersion': 20973,
                        'terminalConfigVersion': 1,
                        'paymentMethod': 'Mobile Money',
                        'isExport': True,
                        'isReliefSupply': True,
                        'buyerTIN': '20162939',
                        'buyerName': 'Buyer Ltd',
                        'buyerAuthorizationCode': 'AUTH-001',
                        'vat5CertificateDetails': {
                            'projectNumber': 'PRJ-001',
                            'certificateNumber': 'VAT5-001',
                            'quantity': 1.0,
                        },
                    }
                }
            },
        )

        payload = InvoiceService._build_mra_invoice_payload(invoice)
        header = payload['invoiceHeader']

        self.assertEqual(header['invoiceNumber'], 'CuQ-D-JY37-T')
        self.assertEqual(header['paymentMethod'], 'MobileMoney')
        self.assertEqual(header['taxpayerConfigVersion'], 20973)
        self.assertTrue(header['isExport'])
        self.assertTrue(header['isReliefSupply'])
        self.assertEqual(header['buyerTIN'], '20162939')
        self.assertEqual(header['buyerAuthorizationCode'], 'AUTH-001')
        self.assertEqual(header['vat5CertificateDetails']['certificateNumber'], 'VAT5-001')
        self.assertEqual(payload['invoiceLineItems'][0]['total'], 200.0)
        self.assertEqual(payload['invoiceLineItems'][0]['totalVAT'], 50.0)
        self.assertNotEqual(payload['invoiceSummary']['offlineSignature'], 'offline-signature')
        self.assertEqual(
            payload['invoiceSummary']['offlineSignature'],
            InvoiceService.build_offline_validation_artifacts_from_payload(payload, self.terminal)['offline_signature'],
        )

    @override_settings(MRA_EIS_SYNC_ALL_ACTIVE_TERMINALS=True)
    def test_replay_task_includes_active_terminal_even_when_cached_offline(self):
        """Scheduled replay must not skip queued invoices when terminal.is_online is stale false."""
        invoice = MRAInvoice.objects.create(
            business=self.business,
            branch=self.branch,
            terminal=self.terminal,
            invoice_number=77,
            seller_tin='70267581',
            seller_name=self.business.name,
            items=[],
            net_amount=Decimal('0.00'),
            tax_amount=Decimal('0.00'),
            gross_amount=Decimal('0.00'),
            tax_breakdown={},
            invoice_signature='offline-signature',
            is_online=False,
            invoice_date=timezone.now(),
            status='offline_queued',
        )
        OfflineInvoiceQueue.objects.create(
            terminal=self.terminal,
            mra_invoice=invoice,
            queue_position=1,
            status='queued',
        )
        self.assertFalse(self.terminal.is_online)

        with patch('mra_eis.tasks.InvoiceService.sync_offline_invoices') as mocked_sync:
            mocked_sync.return_value = {'synced': 1, 'failed': 0}
            from mra_eis.tasks import sync_offline_invoices_for_online_terminals

            result = sync_offline_invoices_for_online_terminals()

        mocked_sync.assert_called_once_with(self.terminal)
        self.assertEqual(result, {'terminals': 1, 'synced': 1, 'failed': 0})

    def test_pos_payload_uses_terminal_site_catalog_description(self):
        """Sale line description must match the exact MRA site product catalog."""
        site_id = 'MA1b3373d0-cb85-4ce5-ad1f-99b0cc89ad1b'
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='system_settings',
            config_version='site-config',
            config_data={
                'terminalConfiguration': {
                    'versionNo': 1,
                    'terminalSite': {'siteId': site_id},
                },
                'taxpayerConfiguration': {
                    'versionNo': 1,
                    'tin': '70267581',
                    'isVATRegistered': False,
                    'activatedTaxRateIds': ['NRT'],
                },
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )
        MRAConfiguration.objects.create(
            business=self.business,
            config_type='terminal_site_products',
            config_version='site-products',
            config_data={
                'items': [
                    {
                        'productCode': '2934309406073',
                        'productName': 'Amazon Big Candy',
                        'description': 'Uncategorized',
                        'unitOfMeasure': 'each',
                        'siteId': site_id,
                        'taxRateId': 'NRT',
                        'isProduct': True,
                    }
                ]
            },
            effective_from=timezone.now(),
            fetched_from_mra_at=timezone.now(),
            is_active=True,
        )
        order = self._create_pos_order(order_number=5013, amount=Decimal('250.00'))
        mapping = MRAProductMapping.objects.get(inventory_item_id=order.items.first().inventory_item_id)
        mapping.mra_product_code = '2934309406073'
        mapping.mra_product_name = 'Amazon Big Candy'
        mapping.mra_unit_measure = 'unit'
        mapping.save(update_fields=['mra_product_code', 'mra_product_name', 'mra_unit_measure', 'updated_at'])
        order.fiscal_invoice_number = 'FISCAL-SITE-DESC-001'
        order.save(update_fields=['fiscal_invoice_number', 'updated_at'])

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)

        self.assertEqual(payload['invoiceLineItems'][0]['productCode'], '2934309406073')
        self.assertEqual(payload['invoiceLineItems'][0]['description'], 'Uncategorized | each')
        mapping.refresh_from_db()
        self.assertEqual(mapping.mra_product_name, 'Uncategorized | each')

    def test_pos_payload_includes_buyer_tin_when_present(self):
        """B2B sales should still send buyer TIN details."""
        order = self._create_pos_order(order_number=5011, amount=Decimal('250.00'))
        order.fiscal_invoice_number = 'FISCAL-B2B-001'
        order.save(update_fields=['fiscal_invoice_number', 'updated_at'])

        payload = POSOrderSubmissionService.build_pos_order_payload(
            order,
            self.terminal,
            is_online=False,
            buyer_tin='20162939',
            buyer_name='Buyer Ltd',
        )
        header = payload['invoiceHeader']

        self.assertEqual(header['buyerTIN'], '20162939')
        self.assertEqual(header['buyerName'], 'Buyer Ltd')
        self.assertTrue(payload['handyPosMetadata']['isB2B'])
        self.assertTrue(payload['handyPosMetadata']['onlineOnly'])

    def test_pos_payload_recalculates_tax_from_mra_mapping(self):
        """EIS payload totals should use MRA tax setup, not stale POS snapshots."""
        self.business.vat_registered = True
        self.business.mra_taxpayer_type = 'VAT'
        self.business.save(update_fields=['vat_registered', 'mra_taxpayer_type', 'updated_at'])
        self._create_fresh_sales_configurations(
            global_configuration={
                'versionNo': 1,
                'taxrates': [
                    {'id': 'A', 'name': 'Standard Rated', 'rate': 16.5},
                ],
            },
            taxpayer_configuration={
                'versionNo': 1,
                'tin': '70267581',
                'isVATRegistered': True,
                'activatedTaxRateIds': ['A'],
            },
        )
        inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Taxed Item',
            category='General',
            item_type='sellable',
            price=Decimal('116.50'),
            stock_units=Decimal('5.000'),
            unit_type='unit',
            status='In Stock',
        )
        MRAProductMapping.objects.create(
            inventory_item=inventory_item,
            branch=self.branch,
            mra_product_code='MRA-TAXED-001',
            mra_product_name='Taxed Item',
            mra_tax_type='standard',
            mra_tax_rate=Decimal('16.50'),
            mra_unit_measure='unit',
            tax_calculation_method='inclusive',
            is_approved=True,
            approved_at=timezone.now(),
            mra_synced=True,
            last_synced_at=timezone.now(),
        )
        order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=5005,
            status='Completed',
            payment_method='Cash',
            subtotal=Decimal('116.50'),
            total=Decimal('116.50'),
            net_amount=Decimal('116.50'),
            vat_amount=Decimal('0.00'),
            gross_amount=Decimal('116.50'),
            fiscal_invoice_number='FISCAL-TAX-001',
        )
        OrderItem.objects.create(
            order=order,
            inventory_item_id=str(inventory_item.id),
            name='Taxed Item',
            quantity=Decimal('1.000'),
            price=Decimal('116.50'),
            subtotal=Decimal('116.50'),
            tax_amount=Decimal('0.00'),
            total=Decimal('116.50'),
        )

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)
        line_item = payload['invoiceLineItems'][0]
        summary = payload['invoiceSummary']

        self.assertEqual(line_item['productCode'], 'MRA-TAXED-001')
        self.assertEqual(line_item['total'], 100.0)
        self.assertEqual(line_item['totalVAT'], 16.5)
        self.assertEqual(summary['totalVAT'], 16.5)
        self.assertEqual(summary['invoiceTotal'], 116.5)

    def test_pos_payload_includes_levy_breakdown_from_mra_mapping(self):
        """Levy-enabled products should populate invoiceSummary.levyBreakDown."""
        self.business.vat_registered = True
        self.business.mra_taxpayer_type = 'VAT'
        self.business.save(update_fields=['vat_registered', 'mra_taxpayer_type', 'updated_at'])
        self._create_fresh_sales_configurations(
            global_configuration={
                'versionNo': 1,
                'taxrates': [
                    {'id': 'A', 'name': 'Standard Rated', 'rate': 16.5},
                ],
            },
            taxpayer_configuration={
                'versionNo': 1,
                'tin': '70267581',
                'isVATRegistered': True,
                'activatedTaxRateIds': ['A'],
                'activatedLevies': [
                    {'levyTypeId': 'ENV', 'levyRate': 2.5},
                ],
            },
        )
        inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Levy Item',
            category='General',
            item_type='sellable',
            price=Decimal('116.50'),
            stock_units=Decimal('5.000'),
            unit_type='unit',
            status='In Stock',
        )
        MRAProductMapping.objects.create(
            inventory_item=inventory_item,
            branch=self.branch,
            mra_product_code='MRA-LEVY-001',
            mra_product_name='Levy Item',
            mra_tax_type='standard',
            mra_tax_rate=Decimal('16.50'),
            mra_unit_measure='unit',
            tax_calculation_method='inclusive',
            mra_levies=[{'levyTypeId': 'ENV'}],
            is_approved=True,
            approved_at=timezone.now(),
            mra_synced=True,
            last_synced_at=timezone.now(),
        )
        order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=5016,
            status='Completed',
            payment_method='Cash',
            subtotal=Decimal('116.50'),
            total=Decimal('116.50'),
            net_amount=Decimal('116.50'),
            vat_amount=Decimal('0.00'),
            gross_amount=Decimal('116.50'),
            fiscal_invoice_number='FISCAL-LEVY-001',
        )
        OrderItem.objects.create(
            order=order,
            inventory_item_id=str(inventory_item.id),
            name='Levy Item',
            quantity=Decimal('1.000'),
            price=Decimal('116.50'),
            subtotal=Decimal('116.50'),
            tax_amount=Decimal('0.00'),
            total=Decimal('116.50'),
        )

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)
        summary = payload['invoiceSummary']

        self.assertEqual(summary['taxBreakDown'][0]['taxableAmount'], 100.0)
        self.assertEqual(summary['totalVAT'], 16.5)
        self.assertEqual(
            summary['levyBreakDown'],
            [{'levyTypeId': 'ENV', 'levyRate': 2.5, 'levyAmount': 2.5}],
        )
        self.assertEqual(summary['invoiceTotal'], 119.0)
        self.assertEqual(summary['amountTendered'], 119.0)

    def test_pos_payload_marks_service_lines_as_not_product(self):
        """MRA service mappings must be submitted with isProduct=false."""
        inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Room Service',
            category='MRA Approved Services',
            item_type='sellable',
            price=Decimal('18000.00'),
            stock_units=Decimal('0.000'),
            unit_type='unit',
            status='In Stock',
        )
        MRAProductMapping.objects.create(
            inventory_item=inventory_item,
            branch=self.branch,
            mra_product_code='MRA-SERVICE-001',
            mra_product_name='Room Service',
            mra_tax_type='standard',
            mra_tax_rate=Decimal('16.50'),
            mra_unit_measure='unit',
            tax_calculation_method='inclusive',
            is_product=False,
            is_approved=True,
            approved_at=timezone.now(),
            mra_synced=True,
            last_synced_at=timezone.now(),
        )
        order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=5017,
            status='Completed',
            payment_method='Cash',
            subtotal=Decimal('18000.00'),
            total=Decimal('18000.00'),
            net_amount=Decimal('18000.00'),
            vat_amount=Decimal('0.00'),
            gross_amount=Decimal('18000.00'),
            fiscal_invoice_number='FISCAL-SERVICE-001',
        )
        OrderItem.objects.create(
            order=order,
            inventory_item_id=str(inventory_item.id),
            name='Room Service',
            quantity=Decimal('1.000'),
            price=Decimal('18000.00'),
            subtotal=Decimal('18000.00'),
            tax_amount=Decimal('0.00'),
            total=Decimal('18000.00'),
        )

        payload = POSOrderSubmissionService.build_pos_order_payload(order, self.terminal, is_online=False)

        self.assertFalse(payload['invoiceLineItems'][0]['isProduct'])
        self.assertFalse(payload['handyPosMetadata']['lineSnapshots'][0]['isProduct'])

    def test_online_pos_submission_requires_validation_url_and_persists_receipt(self):
        """Accepted online sales must carry the MRA validation URL used for QR receipts."""
        self.terminal.is_online = True
        self.terminal.save(update_fields=['is_online', 'updated_at'])
        order = self._create_pos_order(order_number=5006, amount=Decimal('75.00'))

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/sales/submit-sales-transaction',
                data={'data': {'validationURL': 'https://validate/sale/5006'}},
            )
            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.assertFalse(result.get('dry_run'))
        self.assertEqual(order.eis_status, 'SUBMITTED')
        self.assertEqual(order.qr_code_payload, 'https://validate/sale/5006')

        mra_invoice = MRAInvoice.objects.get(
            terminal=self.terminal,
            is_online=True,
            mra_response__order_id=str(order.id),
        )
        self.assertEqual(mra_invoice.status, 'submitted')
        receipt = Receipt.objects.get(mra_invoice=mra_invoice)
        self.assertEqual(receipt.qr_code_data, 'https://validate/sale/5006')

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
        MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES=False,
        MRA_EIS_CHECK_TERMINAL_BLOCK_BEFORE_SALE=False,
    )
    def test_online_pos_submission_refreshes_config_from_nested_string_flag(self):
        """Accepted POS sales should refresh configs when MRA returns a loose truthy flag."""
        self.terminal.is_online = True
        self.terminal.mra_token = 'token'
        self.terminal.save(update_fields=['is_online', 'mra_token', 'updated_at'])
        order = self._create_pos_order(order_number=5019, amount=Decimal('75.00'))

        with (
            patch('mra_eis.services.core.MRAEISClient.call') as mocked_call,
            patch.object(ConfigurationService, 'fetch_and_store_configuration') as mocked_fetch_config,
        ):
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/sales/submit-sales-transaction',
                data={
                    'data': {
                        'validationURL': 'https://validate/sale/5019',
                        'meta': {'shouldDownloadLatestConfig': 'true'},
                    }
                },
            )

            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        self.assertEqual(result['eis_status'], 'SUBMITTED')
        mocked_fetch_config.assert_called_once_with(order.business, terminal=self.terminal)

    def test_online_pos_submission_omits_offline_signature(self):
        """Online sales should not send the offline-only signature field."""
        self.terminal.is_online = True
        self.terminal.mra_token = 'token'
        self.terminal.save(update_fields=['is_online', 'mra_token', 'updated_at'])
        order = self._create_pos_order(order_number=5018, amount=Decimal('75.00'))

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/sales/submit-sales-transaction',
                data={'data': {'validationURL': 'https://validate/sale/5018'}},
            )
            POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        submitted_payload = mocked_call.call_args.kwargs['payload']
        self.assertNotIn('offlineSignature', submitted_payload['invoiceSummary'])

    def test_online_pos_submission_without_validation_url_is_rejected(self):
        """Online EIS success without a validation URL is not receipt-compliant."""
        self.terminal.is_online = True
        self.terminal.save(update_fields=['is_online', 'updated_at'])
        order = self._create_pos_order(order_number=5007, amount=Decimal('80.00'))

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/sales/submit-sales-transaction',
                data={'data': {'shouldDownloadLatestConfig': False}},
            )
            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.assertEqual(order.eis_status, 'REJECTED')
        self.assertIn('validationURL', result['errors'][0])
        self.assertFalse(
            Receipt.objects.filter(
                mra_invoice__terminal=self.terminal,
                mra_invoice__is_online=True,
                mra_invoice__mra_response__order_id=str(order.id),
            ).exists()
        )

    def test_online_pos_submission_preserves_mra_http_rejection(self):
        """MRA HTTP validation failures should be rejected, not hidden as prepared offline work."""
        self.terminal.is_online = True
        self.terminal.save(update_fields=['is_online', 'updated_at'])
        order = self._create_pos_order(order_number=5008, amount=Decimal('90.00'))

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            def fake_call(endpoint_key, payload=None, **kwargs):
                if endpoint_key == 'get_terminal_blocking_message':
                    return MRACallResult(
                        ok=True,
                        dry_run=False,
                        status_code=200,
                        endpoint='/api/v1/utilities/get-terminal-blocking-message',
                        data={
                            'statusCode': 1,
                            'remark': 'Not blocked',
                            'data': {'isBlocked': False},
                            'errors': [],
                        },
                    )
                raise MRAIntegrationError(
                    'MRA request failed (report_sale): 400 Bad Request',
                    status_code=400,
                    endpoint='/api/v1/sales/submit-sales-transaction',
                    endpoint_key='report_sale',
                    response_data={'statusCode': -2, 'remark': 'TIN not found', 'data': None, 'errors': []},
                )

            mocked_call.side_effect = fake_call
            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=True)

        order.refresh_from_db()
        self.assertFalse(result.get('dry_run'))
        self.assertEqual(order.eis_status, 'REJECTED')
        self.assertIn('TIN not found', result['errors'])
        self.assertEqual(result['submission_state'], 'rejected')
        self.assertEqual(order.eis_validation_metadata['mra_submission']['state'], 'rejected')
        self.assertIn('TIN not found', order.eis_validation_metadata['mra_submission']['message'])

        mra_invoice = MRAInvoice.objects.get(
            terminal=self.terminal,
            is_online=True,
            mra_response__order_id=str(order.id),
        )
        self.assertEqual(mra_invoice.status, 'rejected')
        self.assertEqual(mra_invoice.mra_response['response']['remark'], 'TIN not found')

    def test_mra_server_error_keeps_offline_pos_order_pending_for_retry(self):
        """MRA 5xx responses are transient server failures, not accepted sales."""
        order = self._create_pos_order(order_number=5015, amount=Decimal('90.00'))
        html_error = (
            '<!DOCTYPE html><html><head><title>HTTP Error 500.30 - ASP.NET Core app failed to start'
            '</title></head><body>scary stack page</body></html>'
        )

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.side_effect = MRAIntegrationError(
                'MRA request failed (report_sale_offline): 500 Internal Server Error',
                status_code=500,
                endpoint='/api/v1/sales/submit-sales-transaction',
                endpoint_key='report_sale_offline',
                response_data={'raw': html_error},
            )
            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=False)

        order.refresh_from_db()

        self.assertTrue(result.get('dry_run'))
        self.assertEqual(order.eis_status, 'PENDING')
        self.assertIn(html_error, result['errors'])
        self.assertEqual(result['submission_state'], 'offline_queued')
        self.assertEqual(
            result['submission_message'],
            'Offline fiscal receipt issued and queued for MRA replay.',
        )
        self.assertNotIn('<html', result['submission_message'].lower())
        self.assertNotIn('mra request failed', result['submission_message'].lower())
        self.assertNotIn('internal server error', result['submission_message'].lower())
        self.assertNotIn('/api/v1/', result['submission_message'].lower())
        self.assertNotIn('server error', result['submission_message'].lower())
        self.assertNotIn('html server error', result['submission_message'].lower())
        self.assertTrue(result['queued_offline'])
        self.assertEqual(order.eis_validation_metadata['mra_submission']['state'], 'offline_queued')
        self.assertTrue(order.eis_validation_metadata['mra_submission']['retryable'])

        mra_invoice = MRAInvoice.objects.get(
            terminal=self.terminal,
            is_online=False,
            mra_response__order_id=str(order.id),
        )
        self.assertEqual(mra_invoice.status, 'offline_queued')
        self.assertTrue(OfflineInvoiceQueue.objects.filter(mra_invoice=mra_invoice).exists())
        self.assertEqual(mra_invoice.mra_response['response']['reason'], 'mra_server_error')

    def test_pos_submission_handles_plain_text_mra_success_response(self):
        """MRA text/plain success bodies should not crash POS submission handling."""
        order = self._create_pos_order(order_number=5014, amount=Decimal('90.00'))

        with patch('mra_eis.services.core.MRAEISClient.call') as mocked_call:
            mocked_call.return_value = MRACallResult(
                ok=True,
                dry_run=False,
                status_code=200,
                endpoint='/api/v1/sales/submit-sales-transaction',
                data='Success',
            )
            result = POSOrderSubmissionService.prepare_pos_order_submission(order, force_online=False)

        order.refresh_from_db()

        self.assertEqual(order.eis_status, 'SUBMITTED')
        self.assertEqual(result['response'], {'raw': 'Success'})


class TransactionReconciliationServiceTests(TransactionTestCase):
    """Reconcile local records from MRA's last submitted transaction endpoints."""

    def setUp(self):
        self.user = User.objects.create_user(email='reconcile@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Reconcile Business', tin='70267581')
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main',
            address='123 Main St',
            city='Blantyre',
            country='Malawi',
        )
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-RECON-001',
            device_serial='DEVICE-RECON-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-RECON-001',
            mra_taxpayer_id=70267581,
            terminal_position=3,
            mra_api_key='test-terminal-secret',
            mra_token='test-terminal-jwt',
            status='active',
            is_online=True,
        )

    def _create_order_and_invoice(self, *, fiscal_number: str, sequence: int, is_online: bool):
        order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=sequence,
            status='Completed',
            payment_method='Cash',
            subtotal=Decimal('250.00'),
            total=Decimal('250.00'),
            net_amount=Decimal('250.00'),
            vat_amount=Decimal('0.00'),
            gross_amount=Decimal('250.00'),
            fiscal_invoice_number=fiscal_number,
            eis_status='PENDING',
            is_dirty=True,
        )
        invoice = MRAInvoice.objects.create(
            business=self.business,
            branch=self.branch,
            terminal=self.terminal,
            invoice_number=sequence,
            seller_tin='70267581',
            seller_name='Reconcile Business',
            buyer_tin='',
            buyer_name='',
            items=[
                {
                    'description': 'Uncategorized | each',
                    'productCode': '2934309406073',
                    'quantity': 1,
                    'unitPrice': 250,
                    'total': 250,
                    'totalVAT': 0,
                }
            ],
            net_amount=Decimal('250.00'),
            tax_amount=Decimal('0.00'),
            gross_amount=Decimal('250.00'),
            tax_breakdown={'zero': '250.00'},
            invoice_signature='local-signature',
            status='draft' if is_online else 'offline_queued',
            is_online=is_online,
            invoice_date=timezone.now(),
            mra_response={
                'payload': {'invoiceHeader': {'invoiceNumber': fiscal_number}},
                'response': {'status': 'prepared', 'reason': 'mra_server_error'},
            },
        )
        return order, invoice

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_reconcile_last_online_marks_pending_order_submitted(self, mock_call):
        fiscal_number = 'CuQ-D-JY38-D'
        order, invoice = self._create_order_and_invoice(
            fiscal_number=fiscal_number,
            sequence=3,
            is_online=True,
        )
        retry = SyncRetryQueue.objects.create(
            terminal=self.terminal,
            operation_type='submit_pos_order',
            payload={'order_id': str(order.id)},
            next_attempt_at=timezone.now(),
            last_error='Failed to send a transaction',
        )
        submitted_at = timezone.now().replace(microsecond=0)
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/sales/last-submitted-online-transaction',
            data={
                'statusCode': 1,
                'remark': 'Last submitted online invoice retrieved successfully.',
                'data': {
                    'dateSubmitted': submitted_at.isoformat(),
                    'validationURL': 'https://dev-eis-portal.mra.mw/ReceiptValidation/Validate/?TI=CuQ-D-JY38-D',
                    'invoiceHeader': {'invoiceNumber': fiscal_number},
                },
                'errors': [],
            },
        )

        result = TransactionReconciliationService.reconcile_terminal(self.terminal, modes=['online'])

        order.refresh_from_db()
        invoice.refresh_from_db()
        retry.refresh_from_db()
        self.terminal.refresh_from_db()

        self.assertEqual(result['matched'], 1)
        self.assertTrue(result['results']['online']['matched'])
        self.assertEqual(order.eis_status, 'SUBMITTED')
        self.assertTrue(order.is_fiscal_locked)
        self.assertFalse(order.is_dirty)
        self.assertIn('ReceiptValidation', order.qr_code_payload)
        self.assertEqual(invoice.status, 'submitted')
        self.assertEqual(invoice.mra_response['response']['data']['invoiceHeader']['invoiceNumber'], fiscal_number)
        self.assertEqual(retry.status, 'completed')
        self.assertEqual(self.terminal.online_invoice_counter, 3)
        mock_call.assert_called_once_with(
            'get_last_online_transaction',
            payload=None,
            method='POST',
            mutating=False,
            send_json=False,
        )

    @override_settings(
        MRA_EIS_DRY_RUN=False,
        MRA_EIS_ENABLE_HTTP_CALLS=True,
        MRA_EIS_ALLOW_LIVE_SUBMISSION=True,
    )
    @patch.object(MRAEISClient, 'call')
    def test_reconcile_last_offline_marks_queue_synced(self, mock_call):
        fiscal_number = 'CuQ-D-JY38-E'
        _order, invoice = self._create_order_and_invoice(
            fiscal_number=fiscal_number,
            sequence=4,
            is_online=False,
        )
        queue = OfflineInvoiceQueue.objects.create(
            terminal=self.terminal,
            mra_invoice=invoice,
            queue_position=1,
            status='queued',
            last_sync_error='Failed to send a transaction',
        )
        mock_call.return_value = MRACallResult(
            ok=True,
            dry_run=False,
            status_code=200,
            endpoint='/api/v1/sales/last-submitted-offline-transaction',
            data={
                'statusCode': 1,
                'remark': 'Last submitted offline invoice retrieved successfully.',
                'data': {
                    'dateSubmitted': '0001-01-01T00:00:00',
                    'invoiceHeader': {'invoiceNumber': fiscal_number},
                    'invoiceSummary': {'offlineSignature': 'offline-signature'},
                },
                'errors': [],
            },
        )

        result = TransactionReconciliationService.reconcile_terminal(self.terminal, modes=['offline'])

        invoice.refresh_from_db()
        queue.refresh_from_db()
        self.terminal.refresh_from_db()

        self.assertEqual(result['matched'], 1)
        self.assertTrue(result['results']['offline']['matched'])
        self.assertEqual(invoice.status, 'offline_synced')
        self.assertEqual(queue.status, 'synced')
        self.assertEqual(queue.last_sync_error, '')
        self.assertEqual(self.terminal.offline_invoice_counter, 4)
        mock_call.assert_called_once_with(
            'get_last_offline_transaction',
            payload=None,
            method='POST',
            mutating=False,
            send_json=False,
        )


class ReceiptTests(TestCase):
    """Test receipt generation"""

    def setUp(self):
        self.user = User.objects.create_user(email='test@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Test Business')
        self.branch = Branch.objects.create(business=self.business, name='Main', address='123 Main St', city='Lilongwe', country='Malawi')
        
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-001',
            device_serial='DEVICE-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-001',
            mra_api_key='test-key',
            status='active',
            is_online=True
        )

        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        self.invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True
        )

    def _set_taxpayer_vat_registered(self, is_vat_registered: bool):
        MRAConfiguration.objects.update_or_create(
            business=self.business,
            config_type='taxpayer_configuration',
            config_version=f'taxpayer-vat-{is_vat_registered}',
            defaults={
                'config_data': {
                    'versionNo': 1,
                    'tin': '1234567890',
                    'isVATRegistered': is_vat_registered,
                    'activatedTaxRateIds': ['A'] if is_vat_registered else ['NRT'],
                },
                'effective_from': timezone.now(),
                'fetched_from_mra_at': timezone.now(),
                'is_active': True,
            },
        )

    def test_receipt_generation(self):
        """Test receipt is generated"""
        receipt = ReceiptService.generate_receipt(self.invoice)

        self.assertIsNotNone(receipt)
        self.assertIn('RECEIPT', receipt.receipt_text)
        self.assertIn(str(self.invoice.invoice_number), receipt.receipt_text)

    def test_qr_code_data(self):
        """Test QR code data is generated"""
        receipt = ReceiptService.generate_receipt(self.invoice)

        qr_data = json.loads(receipt.qr_code_data)
        self.assertEqual(qr_data['invoice_number'], receipt.receipt_number)
        self.assertEqual(qr_data['seller_tin'], self.invoice.seller_tin)
        self.assertEqual(qr_data['signature'], self.invoice.invoice_signature)

    def test_receipt_uses_mra_payload_invoice_number_when_available(self):
        """MRA receipt number should match invoiceHeader.invoiceNumber."""
        self.invoice.mra_response = {
            'payload': {
                'invoiceHeader': {
                    'invoiceNumber': 'CuQ-D-JY38-D',
                }
            }
        }
        self.invoice.save(update_fields=['mra_response', 'updated_at'])

        receipt = ReceiptService.generate_receipt(self.invoice)
        qr_data = json.loads(receipt.qr_code_data)

        self.assertEqual(receipt.receipt_number, 'CuQ-D-JY38-D')
        self.assertIn('Receipt Number: CuQ-D-JY38-D', receipt.receipt_text)
        self.assertEqual(qr_data['invoice_number'], 'CuQ-D-JY38-D')

    def test_receipt_vat_label_uses_taxpayer_config_for_zero_rated_vat_sale(self):
        """VAT taxpayer selling zero-rated goods should still print VAT registered."""
        self._set_taxpayer_vat_registered(True)
        zero_invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=[
                {
                    'mra_product_code': 'ZERO-001',
                    'name': 'Zero Rated Item',
                    'quantity': Decimal('1'),
                    'unit_price': Decimal('1000.00'),
                    'tax_rate': Decimal('0.00'),
                    'tax_category': 'zero',
                }
            ],
            is_online=True,
        )
        self.assertEqual(zero_invoice.tax_amount, Decimal('0.00'))

        receipt = ReceiptService.generate_receipt(zero_invoice)

        self.assertIn('*VAT REGISTERED*', receipt.receipt_text)
        self.assertNotIn('*NON VAT REGISTERED*', receipt.receipt_text)

    def test_receipt_vat_label_uses_taxpayer_config_for_non_vat_sale_with_vat_amount(self):
        """Non-VAT taxpayer should not print VAT registered just because VAT exists on the invoice."""
        self._set_taxpayer_vat_registered(False)
        self.assertGreater(self.invoice.tax_amount, Decimal('0.00'))

        receipt = ReceiptService.generate_receipt(self.invoice)

        self.assertIn('*NON VAT REGISTERED*', receipt.receipt_text)
        self.assertNotIn('*VAT REGISTERED*', receipt.receipt_text.replace('*NON VAT REGISTERED*', ''))


class AuditLogTests(TestCase):
    """Test audit logging"""

    def setUp(self):
        self.user = User.objects.create_user(email='test@example.com', password='test123')
        self.business = Business.objects.create(owner=self.user, name='Test Business')
        self.branch = Branch.objects.create(business=self.business, name='Main', address='123 Main St', city='Lilongwe', country='Malawi')
        
        self.terminal = Terminal.objects.create(
            business=self.business,
            branch=self.branch,
            terminal_id='TERM-001',
            device_serial='DEVICE-001',
            pos_name='Handy POS',
            pos_version='1.0.0',
            os_type='Web',
            mra_terminal_id='MRA-TERM-001',
            mra_api_key='test-key',
            status='active',
            is_online=True
        )

    def test_terminal_audit_log(self):
        """Test terminal audit log is created"""
        logs = TerminalAuditLog.objects.filter(terminal=self.terminal)
        self.assertGreater(logs.count(), 0)

    def test_invoice_audit_log(self):
        """Test invoice audit log is created"""
        items = [
            {
                'mra_product_code': 'BEVERAGE-001',
                'name': 'Coca Cola 500ml',
                'quantity': Decimal('1'),
                'unit_price': Decimal('2500.00'),
                'tax_rate': Decimal('16.50'),
                'tax_category': 'standard',
            }
        ]

        invoice = InvoiceService.create_invoice(
            terminal=self.terminal,
            seller_tin='1234567890',
            seller_name='Test Business',
            items=items,
            is_online=True
        )

        logs = InvoiceAuditLog.objects.filter(mra_invoice=invoice)
        self.assertGreater(logs.count(), 0)
        self.assertEqual(logs.first().action, 'created')
