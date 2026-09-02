import uuid
from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from business.models import Business, Branch, BusinessSettings
from inventory.models import InventoryItem, MRAProductMapping, PurchaseOrder, PurchaseOrderItem
from pos_sessions.correction_serializers import VoidTransactionSerializer
from pos_sessions.correction_views import VoidTransactionViewSet
from pos_sessions.models import Order, OrderItem, Session, VoidTransaction
from pos_sessions.sync_views import InsufficientStockError, decrement_inventory_for_order
from staff.models import Staff, StaffRole

User = get_user_model()


class OrderBatchTraceAndVoidTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='owner@example.com',
            password='test12345'
        )
        self.business = Business.objects.create(
            owner=self.user,
            name='Batch Trace Test Business',
        )
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main Branch',
            address='123 Main St',
            city='Lilongwe',
            country='Malawi',
        )

        self.inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Milk 1L',
            category='Dairy',
            item_type='sellable',
            stock_units=Decimal('10.000'),
            reorder_level=Decimal('1.000'),
            cost=Decimal('1.00'),
            price=Decimal('2.00'),
            value=Decimal('10.00'),
        )

        # Batch 1 (older / should be consumed first)
        po1 = PurchaseOrder.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=uuid.uuid4(),
            created_by='tester',
            received_date=timezone.now() - timedelta(days=3),
            total_items=1,
            total_cost=Decimal('5.00'),
        )
        self.batch_1 = PurchaseOrderItem.objects.create(
            purchase_order=po1,
            inventory_item=self.inventory_item,
            quantity_ordered=Decimal('5.000'),
            quantity_received=Decimal('5.000'),
            quantity_remaining=Decimal('5.000'),
            cost_per_unit=Decimal('1.00'),
            total_cost=Decimal('5.00'),
            batch_number='BATCH-OLD',
            expiry_date=(timezone.now() + timedelta(days=15)).date(),
        )

        # Batch 2 (newer)
        po2 = PurchaseOrder.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=uuid.uuid4(),
            created_by='tester',
            received_date=timezone.now() - timedelta(days=1),
            total_items=1,
            total_cost=Decimal('5.00'),
        )
        self.batch_2 = PurchaseOrderItem.objects.create(
            purchase_order=po2,
            inventory_item=self.inventory_item,
            quantity_ordered=Decimal('5.000'),
            quantity_received=Decimal('5.000'),
            quantity_remaining=Decimal('5.000'),
            cost_per_unit=Decimal('1.00'),
            total_cost=Decimal('5.00'),
            batch_number='BATCH-NEW',
            expiry_date=(timezone.now() + timedelta(days=60)).date(),
        )

    def _create_order_with_single_item(self, order_number, quantity):
        order = Order.objects.create(
            business=self.business,
            branch=self.branch,
            order_number=order_number,
            order_type='sale',
            payment_method='Cash',
            subtotal=Decimal(str(quantity)),
            total=Decimal(str(quantity)),
            net_amount=Decimal(str(quantity)),
            gross_amount=Decimal(str(quantity)),
            vat_amount=Decimal('0.00'),
        )
        order_item = OrderItem.objects.create(
            order=order,
            inventory_item_id=str(self.inventory_item.id),
            name=self.inventory_item.name,
            quantity=Decimal(str(quantity)),
            price=Decimal('1.00'),
            subtotal=Decimal(str(quantity)),
            tax_amount=Decimal('0.00'),
            total=Decimal(str(quantity)),
        )
        return order, order_item

    def test_decrement_records_batch_consumption_trace(self):
        order, order_item = self._create_order_with_single_item(order_number=1001, quantity='4.000')
        decrement_inventory_for_order(order, self.branch, self.business)
        order_item.refresh_from_db()

        self.batch_1.refresh_from_db()
        self.batch_2.refresh_from_db()

        self.assertEqual(self.batch_1.quantity_remaining, Decimal('1.000'))
        self.assertEqual(self.batch_2.quantity_remaining, Decimal('5.000'))

        self.assertEqual(len(order_item.batch_consumption), 1)
        trace = order_item.batch_consumption[0]
        self.assertEqual(trace['inventory_item_id'], str(self.inventory_item.id))
        self.assertEqual(trace['batch_id'], str(self.batch_1.id))
        self.assertEqual(Decimal(trace['quantity']), Decimal('4.000'))

    def test_decrement_blocks_when_quantity_exceeds_available_stock(self):
        order, _ = self._create_order_with_single_item(order_number=1099, quantity='11.000')

        with self.assertRaises(InsufficientStockError) as context:
            decrement_inventory_for_order(order, self.branch, self.business)

        self.assertIn('Insufficient stock for Milk 1L', str(context.exception))

        self.inventory_item.refresh_from_db()
        self.batch_1.refresh_from_db()
        self.batch_2.refresh_from_db()
        self.assertEqual(self.inventory_item.stock_units, Decimal('10.000'))
        self.assertEqual(self.batch_1.quantity_remaining, Decimal('5.000'))
        self.assertEqual(self.batch_2.quantity_remaining, Decimal('5.000'))

    def test_void_restores_original_batches_even_after_other_sales(self):
        # Order A uses 4 from old batch.
        order_a, order_item_a = self._create_order_with_single_item(order_number=1002, quantity='4.000')
        decrement_inventory_for_order(order_a, self.branch, self.business)

        # Order B later uses remaining old stock (1) and then new batch (1).
        order_b, _ = self._create_order_with_single_item(order_number=1003, quantity='2.000')
        decrement_inventory_for_order(order_b, self.branch, self.business)

        self.batch_1.refresh_from_db()
        self.batch_2.refresh_from_db()
        self.assertEqual(self.batch_1.quantity_remaining, Decimal('0.000'))
        self.assertEqual(self.batch_2.quantity_remaining, Decimal('4.000'))

        # Ensure order A trace points to its original batch usage only.
        order_item_a.refresh_from_db()
        self.assertEqual(len(order_item_a.batch_consumption), 1)
        self.assertEqual(order_item_a.batch_consumption[0]['batch_id'], str(self.batch_1.id))
        self.assertEqual(Decimal(order_item_a.batch_consumption[0]['quantity']), Decimal('4.000'))

        factory = APIRequestFactory()
        request = factory.post(
            '/sessions/void-transactions/create_void/',
            {
                'original_order_id': str(order_a.id),
                'void_reason': 'other',
                'reason_description': 'Regression test for original batch restore',
            },
            format='json',
        )
        force_authenticate(request, user=self.user)
        response = VoidTransactionViewSet.as_view({'post': 'create_void'})(request)

        self.assertEqual(response.status_code, 201)

        self.batch_1.refresh_from_db()
        self.batch_2.refresh_from_db()
        self.inventory_item.refresh_from_db()
        order_a.refresh_from_db()

        # Expected after voiding only Order A:
        # batch_1 was 0 after A+B, should return +4 to 4
        # batch_2 should stay 4 (Order B still consumed 1 from it)
        self.assertEqual(self.batch_1.quantity_remaining, Decimal('4.000'))
        self.assertEqual(self.batch_2.quantity_remaining, Decimal('4.000'))
        self.assertEqual(self.inventory_item.stock_units, Decimal('8.000'))
        self.assertEqual(order_a.status, 'Voided')

    def test_void_does_not_reduce_inventory_when_batch_totals_are_lower_than_stock(self):
        # Simulate drifted state from production log:
        # product stock is 2, but batch balance after restore is only 1.
        self.inventory_item.stock_units = Decimal('2.000')
        self.inventory_item.save(update_fields=['stock_units', 'updated_at'])

        self.batch_1.quantity_ordered = Decimal('1.000')
        self.batch_1.quantity_received = Decimal('1.000')
        self.batch_1.quantity_remaining = Decimal('0.000')
        self.batch_1.save(
            update_fields=['quantity_ordered', 'quantity_received', 'quantity_remaining', 'updated_at']
        )

        # Keep only one relevant batch for this scenario.
        self.batch_2.quantity_ordered = Decimal('0.000')
        self.batch_2.quantity_received = Decimal('0.000')
        self.batch_2.quantity_remaining = Decimal('0.000')
        self.batch_2.save(
            update_fields=['quantity_ordered', 'quantity_received', 'quantity_remaining', 'updated_at']
        )

        order, order_item = self._create_order_with_single_item(order_number=1004, quantity='1.000')
        order_item.batch_consumption = [
            {
                'inventory_item_id': str(self.inventory_item.id),
                'batch_id': None,
                'quantity': '1.000',
                'unassigned': True,
            }
        ]
        order_item.save(update_fields=['batch_consumption', 'updated_at'])

        factory = APIRequestFactory()
        request = factory.post(
            '/sessions/void-transactions/create_void/',
            {
                'original_order_id': str(order.id),
                'void_reason': 'customer_request',
                'reason_description': 'Regression: void must not reduce stock',
            },
            format='json',
        )
        force_authenticate(request, user=self.user)
        response = VoidTransactionViewSet.as_view({'post': 'create_void'})(request)

        self.assertEqual(response.status_code, 201)

        self.batch_1.refresh_from_db()
        self.inventory_item.refresh_from_db()

        # Batch was restored by 1 (0 -> 1), and inventory stock must increase (2 -> 3), never decrease.
        self.assertEqual(self.batch_1.quantity_remaining, Decimal('1.000'))
        self.assertEqual(self.inventory_item.stock_units, Decimal('3.000'))

    def test_eis_void_submits_correction_and_preserves_stock_reversal(self):
        BusinessSettings.objects.update_or_create(
            business=self.business,
            defaults={'enable_eis': True},
        )

        order, _ = self._create_order_with_single_item(order_number=1005, quantity='2.000')
        decrement_inventory_for_order(order, self.branch, self.business)
        order.fiscal_invoice_number = 'FISCAL-VOID-1005'
        order.eis_status = 'SUBMITTED'
        order.eis_uuid = 'original-sale-uuid'
        order.save(update_fields=['fiscal_invoice_number', 'eis_status', 'eis_uuid', 'updated_at'])

        factory = APIRequestFactory()
        request = factory.post(
            '/sessions/void-transactions/create_void/',
            {
                'original_order_id': str(order.id),
                'void_reason': 'customer_request',
                'reason_description': 'EIS void correction with stock reversal',
                'supporting_documents': ['RETURN-SLIP-1005', 'https://docs.example/void-1005'],
                'refund_method': 'cash',
                'refund_amount': '1.00',
            },
            format='json',
        )
        force_authenticate(request, user=self.user)

        with patch('pos_sessions.correction_views.CorrectionService.submit_void_transaction') as submit_void:
            submit_void.return_value = {
                'eis_status': 'SUBMITTED',
                'dry_run': False,
                'endpoint': 'cancel_receipt',
            }
            response = VoidTransactionViewSet.as_view({'post': 'create_void'})(request)

        self.assertEqual(response.status_code, 201)
        submit_void.assert_called_once()

        self.batch_1.refresh_from_db()
        self.batch_2.refresh_from_db()
        self.inventory_item.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(self.batch_1.quantity_remaining, Decimal('5.000'))
        self.assertEqual(self.batch_2.quantity_remaining, Decimal('5.000'))
        self.assertEqual(self.inventory_item.stock_units, Decimal('10.000'))
        self.assertEqual(order.status, 'Voided')
        self.assertEqual(response.data['eis_result']['endpoint'], 'cancel_receipt')

        void_transaction = VoidTransaction.objects.get(original_order=order)
        serialized = VoidTransactionSerializer(void_transaction).data
        self.assertIn('refund_amount', serialized)
        self.assertIn('refund_processed', serialized)
        self.assertEqual(Decimal(serialized['refund_amount']), Decimal('1.00'))
        self.assertTrue(serialized['refund_processed'])
        self.assertEqual(
            serialized['supporting_documents'],
            'RETURN-SLIP-1005\nhttps://docs.example/void-1005',
        )


class SyncPushOrderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='sync-owner@example.com',
            password='test12345'
        )
        self.business = Business.objects.create(
            owner=self.user,
            name='Sync Push Test Business',
        )
        BusinessSettings.objects.update_or_create(
            business=self.business,
            defaults={'block_sales_if_tax_mapping_missing': True},
        )
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main Branch',
            address='123 Main St',
            city='Lilongwe',
            country='Malawi',
        )
        self.session = Session.objects.create(
            business=self.business,
            branch=self.branch,
            user=self.user,
            status='active',
            opening_float=Decimal('0.00'),
            expected_cash=Decimal('0.00'),
            total_sales=Decimal('0.00'),
            started_at=timezone.now(),
        )
        self.inventory_item = InventoryItem.objects.create(
            business=self.business,
            branch=self.branch,
            name='Soda Can',
            category='Beverages',
            item_type='sellable',
            stock_units=Decimal('10.000'),
            reorder_level=Decimal('1.000'),
            cost=Decimal('3.00'),
            price=Decimal('5.00'),
            value=Decimal('30.00'),
        )
        MRAProductMapping.objects.create(
            inventory_item=self.inventory_item,
            branch=self.branch,
            mra_product_code='SODA-001',
            mra_product_name='Soda Can',
            mra_tax_type='standard',
            mra_tax_rate=Decimal('0.00'),
            mra_unit_measure='unit',
            tax_calculation_method='inclusive',
            is_approved=True,
            mra_synced=True,
            approved_at=timezone.now(),
            last_synced_at=timezone.now(),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _build_sync_payload(self, order_id: str) -> dict:
        now = timezone.now().isoformat()
        return {
            'last_synced_at': now,
            'branch_id': str(self.branch.id),
            'changes': [
                {
                    'id': order_id,
                    'entity_type': 'Order',
                    'op': 'create',
                    'timestamp': now,
                    'data': {
                        'id': order_id,
                        'orderNumber': 7001,
                        'orderType': 'sale',
                        'status': 'Completed',
                        'paymentMethod': 'Cash',
                        'subtotal': 5.0,
                        'total': 5.0,
                        'cogs': 3.0,
                        'createdAt': now,
                        'updatedAt': now,
                        'sessionId': str(self.session.id),
                        'items': [
                            {
                                'id': str(uuid.uuid4()),
                                'inventoryItemId': str(self.inventory_item.id),
                                'name': self.inventory_item.name,
                                'quantity': 1,
                                'price': 5.0,
                                'notes': '',
                            }
                        ],
                    },
                }
            ],
        }

    def _build_variable_price_sync_payload(
        self,
        order_id: str,
        *,
        quantity: float,
        price: float,
        item_total: float | None = None,
        item_subtotal: float | None = None,
        item_tax: float | None = None,
    ) -> dict:
        now = timezone.now().isoformat()
        item_payload = {
            'id': str(uuid.uuid4()),
            'inventoryItemId': str(self.inventory_item.id),
            'name': self.inventory_item.name,
            'quantity': quantity,
            'price': price,
            'notes': '',
        }
        if item_total is not None:
            item_payload['total'] = item_total
        if item_subtotal is not None:
            item_payload['subtotal'] = item_subtotal
        if item_tax is not None:
            item_payload['taxAmount'] = item_tax

        return {
            'last_synced_at': now,
            'branch_id': str(self.branch.id),
            'changes': [
                {
                    'id': order_id,
                    'entity_type': 'Order',
                    'op': 'create',
                    'timestamp': now,
                    'data': {
                        'id': order_id,
                        'orderNumber': 7101,
                        'orderType': 'sale',
                        'status': 'Completed',
                        'paymentMethod': 'Cash',
                        'subtotal': 0,
                        'total': 0,
                        'cogs': 0,
                        'createdAt': now,
                        'updatedAt': now,
                        'sessionId': str(self.session.id),
                        'items': [item_payload],
                    },
                }
            ],
        }

    def test_sync_push_creates_order_and_is_idempotent(self):
        order_id = str(uuid.uuid4())
        payload = self._build_sync_payload(order_id)

        first_response = self.client.post('/sessions/sync/push/', payload, format='json')
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.data['results']['errors'], [])
        self.assertEqual(len(first_response.data['results']['acknowledged']), 1)
        self.assertEqual(first_response.data['results']['acknowledged'][0]['id'], order_id)

        self.assertEqual(Order.objects.filter(id=order_id).count(), 1)
        created_order = Order.objects.get(id=order_id)
        self.assertEqual(created_order.order_number, 7001)
        self.assertEqual(created_order.session_id, self.session.id)
        self.assertEqual(created_order.items.count(), 1)

        self.inventory_item.refresh_from_db()
        self.assertEqual(self.inventory_item.stock_units, Decimal('9.000'))

        second_response = self.client.post('/sessions/sync/push/', payload, format='json')
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.data['results']['errors'], [])
        self.assertEqual(len(second_response.data['results']['acknowledged']), 1)
        self.assertEqual(second_response.data['results']['acknowledged'][0]['id'], order_id)

        self.assertEqual(Order.objects.filter(id=order_id).count(), 1)
        self.assertEqual(OrderItem.objects.filter(order_id=order_id).count(), 1)

        self.inventory_item.refresh_from_db()
        self.assertEqual(self.inventory_item.stock_units, Decimal('9.000'))

    def test_sync_push_blocks_order_when_quantity_exceeds_available_stock(self):
        order_id = str(uuid.uuid4())
        payload = self._build_sync_payload(order_id)
        item_payload = payload['changes'][0]['data']['items'][0]
        item_payload['quantity'] = 11
        payload['changes'][0]['data']['subtotal'] = 55.0
        payload['changes'][0]['data']['total'] = 55.0

        response = self.client.post('/sessions/sync/push/', payload, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.filter(id=order_id).count(), 0)
        self.assertEqual(len(response.data['results']['acknowledged']), 0)
        self.assertTrue(response.data['results']['errors'])
        self.assertEqual(response.data['results']['errors'][0].get('reason'), 'insufficient_stock')
        self.assertIn('Insufficient stock for Soda Can', response.data['results']['errors'][0]['error'])

        self.inventory_item.refresh_from_db()
        self.assertEqual(self.inventory_item.stock_units, Decimal('10.000'))

    def test_direct_order_create_blocks_when_quantity_exceeds_available_stock(self):
        payload = {
            'branch': self.branch.id,
            'session': str(self.session.id),
            'orderNumber': 7201,
            'orderType': 'sale',
            'status': 'Completed',
            'paymentMethod': 'Cash',
            'subtotal': 55.0,
            'total': 55.0,
            'cogs': 0,
            'items': [
                {
                    'id': str(uuid.uuid4()),
                    'inventoryItemId': str(self.inventory_item.id),
                    'name': self.inventory_item.name,
                    'quantity': 11,
                    'price': 5.0,
                    'notes': '',
                }
            ],
        }

        response = self.client.post('/sessions/orders/', payload, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Insufficient stock for Soda Can', str(response.data))
        self.assertFalse(Order.objects.filter(order_number=7201, branch=self.branch).exists())

        self.inventory_item.refresh_from_db()
        self.assertEqual(self.inventory_item.stock_units, Decimal('10.000'))

    def test_direct_order_create_blocks_discount_equal_to_item_total(self):
        payload = {
            'branch': self.branch.id,
            'session': str(self.session.id),
            'orderNumber': 7202,
            'orderType': 'sale',
            'status': 'Completed',
            'paymentMethod': 'Cash',
            'subtotal': 0.0,
            'total': 0.0,
            'cogs': 0,
            'items': [
                {
                    'id': str(uuid.uuid4()),
                    'inventoryItemId': str(self.inventory_item.id),
                    'name': self.inventory_item.name,
                    'quantity': 1,
                    'price': 5.0,
                    'notes': '',
                    'discountRuleId': 'full-discount',
                    'discountName': 'Invalid Full Discount',
                    'discountType': 'fixed',
                    'discountValue': 5.0,
                    'discountAmount': 5.0,
                }
            ],
        }

        response = self.client.post('/sessions/orders/', payload, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('Discount must be less than the item total', str(response.data))
        self.assertFalse(Order.objects.filter(order_number=7202, branch=self.branch).exists())

    def test_sync_push_variable_price_keeps_unit_price_and_fractional_quantity(self):
        self.inventory_item.is_variable_price = True
        self.inventory_item.price = Decimal('40.00')
        self.inventory_item.save(update_fields=['is_variable_price', 'price', 'updated_at'])

        mapping = MRAProductMapping.objects.get(inventory_item=self.inventory_item, branch=self.branch)
        mapping.mra_tax_rate = Decimal('16.50')
        mapping.tax_calculation_method = 'inclusive'
        mapping.save(update_fields=['mra_tax_rate', 'tax_calculation_method', 'updated_at'])

        order_id = str(uuid.uuid4())
        payload = self._build_variable_price_sync_payload(
            order_id,
            quantity=2.5,
            price=40.0,
        )

        response = self.client.post('/sessions/sync/push/', payload, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results']['errors'], [])
        self.assertEqual(Order.objects.filter(id=order_id).count(), 1)

        created_order = Order.objects.get(id=order_id)
        created_item = created_order.items.get()

        self.assertAlmostEqual(float(created_order.gross_amount), 100.0, places=2)
        self.assertAlmostEqual(float(created_order.vat_amount), 14.16, places=2)
        self.assertAlmostEqual(float(created_item.quantity), 2.5, places=3)
        self.assertAlmostEqual(float(created_item.price), 40.0, places=2)

        self.inventory_item.refresh_from_db()
        self.assertAlmostEqual(float(self.inventory_item.stock_units), 7.5, places=3)

    def test_sync_push_variable_price_normalizes_legacy_line_total_price_payload(self):
        self.inventory_item.is_variable_price = True
        self.inventory_item.price = Decimal('40.00')
        self.inventory_item.save(update_fields=['is_variable_price', 'price', 'updated_at'])

        mapping = MRAProductMapping.objects.get(inventory_item=self.inventory_item, branch=self.branch)
        mapping.mra_tax_rate = Decimal('0.00')
        mapping.tax_calculation_method = 'inclusive'
        mapping.save(update_fields=['mra_tax_rate', 'tax_calculation_method', 'updated_at'])

        order_id = str(uuid.uuid4())
        payload = self._build_variable_price_sync_payload(
            order_id,
            quantity=2.5,
            price=100.0,       # Legacy client sends line-total into unit-price field.
            item_total=100.0,  # Explicit line total allows backend normalization.
            item_subtotal=100.0,
            item_tax=0.0,
        )

        response = self.client.post('/sessions/sync/push/', payload, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results']['errors'], [])
        self.assertEqual(Order.objects.filter(id=order_id).count(), 1)

        created_order = Order.objects.get(id=order_id)
        created_item = created_order.items.get()

        self.assertAlmostEqual(float(created_order.gross_amount), 100.0, places=2)
        self.assertAlmostEqual(float(created_order.vat_amount), 0.0, places=2)
        # Backend should normalize legacy line-total pricing to a per-unit stored value.
        self.assertAlmostEqual(float(created_item.price), 40.0, places=2)
        self.assertAlmostEqual(float(created_item.total), 100.0, places=2)

    def test_sync_push_variable_price_exclusive_tax_recalculation_is_accurate(self):
        self.inventory_item.is_variable_price = True
        self.inventory_item.price = Decimal('40.00')
        self.inventory_item.save(update_fields=['is_variable_price', 'price', 'updated_at'])

        mapping = MRAProductMapping.objects.get(inventory_item=self.inventory_item, branch=self.branch)
        mapping.mra_tax_rate = Decimal('16.50')
        mapping.tax_calculation_method = 'exclusive'
        mapping.save(update_fields=['mra_tax_rate', 'tax_calculation_method', 'updated_at'])

        order_id = str(uuid.uuid4())
        payload = self._build_variable_price_sync_payload(
            order_id,
            quantity=2.5,
            price=40.0,
        )

        response = self.client.post('/sessions/sync/push/', payload, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results']['errors'], [])
        self.assertEqual(Order.objects.filter(id=order_id).count(), 1)

        created_order = Order.objects.get(id=order_id)
        created_item = created_order.items.get()

        self.assertAlmostEqual(float(created_order.net_amount), 100.0, places=2)
        self.assertAlmostEqual(float(created_order.vat_amount), 16.5, places=2)
        self.assertAlmostEqual(float(created_order.gross_amount), 116.5, places=2)
        self.assertAlmostEqual(float(created_item.subtotal), 100.0, places=2)
        self.assertAlmostEqual(float(created_item.tax_amount), 16.5, places=2)
        self.assertAlmostEqual(float(created_item.total), 116.5, places=2)

    def test_sync_push_discount_recalculates_eis_vat_from_discounted_amount(self):
        mapping = MRAProductMapping.objects.get(inventory_item=self.inventory_item, branch=self.branch)
        mapping.mra_tax_rate = Decimal('16.50')
        mapping.tax_calculation_method = 'inclusive'
        mapping.save(update_fields=['mra_tax_rate', 'tax_calculation_method', 'updated_at'])

        order_id = str(uuid.uuid4())
        payload = self._build_sync_payload(order_id)
        payload['changes'][0]['data']['subtotal'] = 77.25
        payload['changes'][0]['data']['total'] = 90.00
        item_payload = payload['changes'][0]['data']['items'][0]
        item_payload.update({
            'price': 100.0,
            'discountRuleId': 'promo-10',
            'discountName': 'Promo 10%',
            'discountType': 'percentage',
            'discountValue': 10,
            'discountAmount': 10.0,
        })

        response = self.client.post('/sessions/sync/push/', payload, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results']['errors'], [])
        created_order = Order.objects.get(id=order_id)
        created_item = created_order.items.get()

        self.assertAlmostEqual(float(created_order.discount_amount), 10.0, places=2)
        self.assertAlmostEqual(float(created_order.gross_amount), 90.0, places=2)
        self.assertAlmostEqual(float(created_order.vat_amount), 12.75, places=2)
        self.assertAlmostEqual(float(created_order.net_amount), 77.25, places=2)
        self.assertEqual(created_item.discount_rule_id, 'promo-10')
        self.assertEqual(created_item.discount_name, 'Promo 10%')
        self.assertAlmostEqual(float(created_item.discount_amount), 10.0, places=2)
        self.assertAlmostEqual(float(created_item.total), 90.0, places=2)
        self.assertAlmostEqual(float(created_item.tax_amount), 12.75, places=2)

    def test_sync_push_blocks_fixed_discount_equal_to_item_total(self):
        order_id = str(uuid.uuid4())
        payload = self._build_sync_payload(order_id)
        item_payload = payload['changes'][0]['data']['items'][0]
        item_payload.update({
            'price': 5.0,
            'discountRuleId': 'full-discount',
            'discountName': 'Invalid Full Discount',
            'discountType': 'fixed',
            'discountValue': 5.0,
            'discountAmount': 5.0,
        })

        response = self.client.post('/sessions/sync/push/', payload, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.filter(id=order_id).count(), 0)
        self.assertEqual(len(response.data['results']['acknowledged']), 0)
        self.assertTrue(response.data['results']['errors'])
        self.assertIn('Discount for Soda Can must be less than the item total', response.data['results']['errors'][0]['error'])

    def test_sync_push_blocks_percentage_discount_of_one_hundred_percent(self):
        order_id = str(uuid.uuid4())
        payload = self._build_sync_payload(order_id)
        item_payload = payload['changes'][0]['data']['items'][0]
        item_payload.update({
            'price': 5.0,
            'discountRuleId': 'full-percent-discount',
            'discountName': 'Invalid 100%',
            'discountType': 'percentage',
            'discountValue': 100,
            'discountAmount': 5.0,
        })

        response = self.client.post('/sessions/sync/push/', payload, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.filter(id=order_id).count(), 0)
        self.assertEqual(len(response.data['results']['acknowledged']), 0)
        self.assertTrue(response.data['results']['errors'])
        self.assertIn('Discount for Soda Can must be less than the item total', response.data['results']['errors'][0]['error'])

    def test_sync_push_blocks_products_with_unsynced_mra_mapping(self):
        mapping = MRAProductMapping.objects.get(inventory_item=self.inventory_item, branch=self.branch)
        mapping.is_approved = True
        mapping.mra_synced = False
        mapping.save(update_fields=['is_approved', 'mra_synced', 'updated_at'])

        order_id = str(uuid.uuid4())
        payload = self._build_sync_payload(order_id)
        response = self.client.post('/sessions/sync/push/', payload, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Order.objects.filter(id=order_id).count(), 0)
        self.assertTrue(response.data['results']['errors'])
        self.assertIn('unsynced MRA mappings', response.data['results']['errors'][0]['error'])


class SessionVisibilityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email='session-owner@example.com',
            password='test12345',
        )
        self.superuser = User.objects.create_superuser(
            email='super@example.com',
            password='test12345',
        )
        self.business = Business.objects.create(
            owner=self.owner,
            name='Session Visibility Business',
        )
        self.branch = Branch.objects.create(
            business=self.business,
            name='Main Branch',
            address='123 Main St',
            city='Lilongwe',
            country='Malawi',
        )
        self.closed_session = Session.objects.create(
            business=self.business,
            branch=self.branch,
            user=self.owner,
            status='closed',
            opening_float=Decimal('100.00'),
            expected_cash=Decimal('180.00'),
            actual_cash=Decimal('180.00'),
            closing_float=Decimal('180.00'),
            difference=Decimal('0.00'),
            total_sales=Decimal('80.00'),
            total_cash_sales=Decimal('80.00'),
            started_at=timezone.now() - timedelta(hours=4),
            closed_at=timezone.now() - timedelta(hours=1),
        )
        self.client = APIClient()

    def test_superuser_can_list_closed_sessions_without_business_assignment(self):
        self.client.force_authenticate(user=self.superuser)

        response = self.client.get(f'/api/sessions/sessions/?branch_id={self.branch.id}')

        self.assertEqual(response.status_code, 200)
        payload = response.json() if hasattr(response, 'json') else response.data
        session_rows = payload.get('results', payload) if isinstance(payload, dict) else payload
        returned_ids = {str(row['id']) for row in session_rows}
        self.assertIn(str(self.closed_session.id), returned_ids)

    def test_non_admin_staff_sees_only_own_closed_sessions_in_history(self):
        cashier_user = User.objects.create_user(
            email='cashier@example.com',
            password='test12345',
        )
        Staff.objects.create(
            business=self.business,
            branch=self.branch,
            user=cashier_user,
            name='Cashier User',
            email='cashier@example.com',
            role=StaffRole.CASHIER,
            is_active=True,
        )
        cashier_closed_session = Session.objects.create(
            business=self.business,
            branch=self.branch,
            user=cashier_user,
            status='closed',
            opening_float=Decimal('50.00'),
            expected_cash=Decimal('90.00'),
            actual_cash=Decimal('90.00'),
            closing_float=Decimal('90.00'),
            difference=Decimal('0.00'),
            total_sales=Decimal('40.00'),
            total_cash_sales=Decimal('40.00'),
            started_at=timezone.now() - timedelta(hours=3),
            closed_at=timezone.now() - timedelta(minutes=30),
        )

        self.client.force_authenticate(user=cashier_user)

        response = self.client.get(f'/api/sessions/sessions/closed_list/?branch_id={self.branch.id}')

        self.assertEqual(response.status_code, 200)
        payload = response.json() if hasattr(response, 'json') else response.data
        session_rows = payload.get('results', payload) if isinstance(payload, dict) else payload
        returned_ids = {str(row['id']) for row in session_rows}
        self.assertIn(str(cashier_closed_session.id), returned_ids)
        self.assertNotIn(str(self.closed_session.id), returned_ids)

    def test_manager_can_see_business_closed_sessions_in_history(self):
        manager_user = User.objects.create_user(
            email='manager@example.com',
            password='test12345',
        )
        Staff.objects.create(
            business=self.business,
            branch=self.branch,
            user=manager_user,
            name='Manager User',
            email='manager@example.com',
            role=StaffRole.MANAGER,
            is_active=True,
        )

        self.client.force_authenticate(user=manager_user)

        response = self.client.get(f'/api/sessions/sessions/closed_list/?branch_id={self.branch.id}')

        self.assertEqual(response.status_code, 200)
        payload = response.json() if hasattr(response, 'json') else response.data
        session_rows = payload.get('results', payload) if isinstance(payload, dict) else payload
        returned_ids = {str(row['id']) for row in session_rows}
        self.assertIn(str(self.closed_session.id), returned_ids)
