from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.utils import timezone
import uuid

from mra_eis.services import CorrectionService

from .models import CreditNote, DebitNote, VoidTransaction, Order
from .correction_serializers import (
    CreditNoteSerializer,
    DebitNoteSerializer,
    VoidTransactionSerializer,
    CreateCreditNoteSerializer,
    CreateDebitNoteSerializer,
    CreateVoidTransactionSerializer,
)


def _accessible_business_ids(user):
    if getattr(user, 'is_superuser', False):
        from business.models import Business

        return Business.objects.values_list('id', flat=True)

    business_ids = []
    try:
        from business.models import Business

        business_ids.extend(Business.objects.filter(owner=user).values_list('id', flat=True))
    except Exception:
        pass

    try:
        from staff.models import Staff

        business_ids.extend(
            Staff.objects.filter(user=user, is_active=True).values_list('business_id', flat=True)
        )
    except Exception:
        pass

    return list(dict.fromkeys(business_ids))


def _should_submit_eis_correction(order):
    business_settings = getattr(order.business, 'settings', None)
    return bool(getattr(business_settings, 'enable_eis', False)) or bool(order.fiscal_invoice_number)


class CreditNoteViewSet(viewsets.ModelViewSet):
    """ViewSet for managing Credit Notes"""
    queryset = CreditNote.objects.all()
    serializer_class = CreditNoteSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter by branch"""
        user = self.request.user
        branch_id = self.request.query_params.get('branch_id')
        
        queryset = CreditNote.objects.filter(business_id__in=_accessible_business_ids(user))
        
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        
        return queryset.order_by('-created_at')
    
    @action(detail=False, methods=['post'])
    def create_credit_note(self, request):
        """Create a new Credit Note"""
        serializer = CreateCreditNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            with transaction.atomic():
                order = Order.objects.get(
                    id=serializer.validated_data['original_order_id'],
                    business_id__in=_accessible_business_ids(request.user),
                )
                
                # Generate credit note number
                credit_note_number = f"CN-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
                
                # Create credit note
                credit_note = CreditNote.objects.create(
                    business=order.business,
                    branch=order.branch,
                    original_order=order,
                    credit_note_number=credit_note_number,
                    reason=serializer.validated_data['reason'],
                    description=serializer.validated_data['description'],
                    credit_amount=serializer.validated_data['credit_amount'],
                    vat_amount=serializer.validated_data['vat_amount'],
                    total_credit=serializer.validated_data['credit_amount'] + serializer.validated_data['vat_amount'],
                    created_by=request.user,
                )
                
                eis_result = None
                if _should_submit_eis_correction(order):
                    eis_result = CorrectionService.submit_credit_note(credit_note)
                else:
                    credit_note.mark_dirty()

                response_data = dict(CreditNoteSerializer(credit_note).data)
                response_data['eis_result'] = eis_result
                
                return Response(
                    response_data,
                    status=status.HTTP_201_CREATED
                )
        except Order.DoesNotExist:
            return Response(
                {'error': 'Order not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=False, methods=['get'])
    def by_order(self, request):
        """Get credit notes for a specific order"""
        order_id = request.query_params.get('order_id')
        
        if not order_id:
            return Response(
                {'error': 'order_id parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        credit_notes = CreditNote.objects.filter(
            original_order_id=order_id,
            business_id__in=_accessible_business_ids(request.user),
        ).order_by('-created_at')
        serializer = CreditNoteSerializer(credit_notes, many=True)
        
        return Response(serializer.data)


class DebitNoteViewSet(viewsets.ModelViewSet):
    """ViewSet for managing Debit Notes"""
    queryset = DebitNote.objects.all()
    serializer_class = DebitNoteSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter by branch"""
        branch_id = self.request.query_params.get('branch_id')
        
        queryset = DebitNote.objects.filter(business_id__in=_accessible_business_ids(self.request.user))
        
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        
        return queryset.order_by('-created_at')
    
    @action(detail=False, methods=['post'])
    def create_debit_note(self, request):
        """Create a new Debit Note"""
        serializer = CreateDebitNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            with transaction.atomic():
                order = Order.objects.get(
                    id=serializer.validated_data['original_order_id'],
                    business_id__in=_accessible_business_ids(request.user),
                )
                
                # Generate debit note number
                debit_note_number = f"DN-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
                
                # Create debit note
                debit_note = DebitNote.objects.create(
                    business=order.business,
                    branch=order.branch,
                    original_order=order,
                    debit_note_number=debit_note_number,
                    description=serializer.validated_data['description'],
                    additional_amount=serializer.validated_data['additional_amount'],
                    vat_amount=serializer.validated_data['vat_amount'],
                    total_debit=serializer.validated_data['additional_amount'] + serializer.validated_data['vat_amount'],
                    created_by=request.user,
                )
                
                eis_result = None
                if _should_submit_eis_correction(order):
                    eis_result = CorrectionService.submit_debit_note(debit_note)
                else:
                    debit_note.mark_dirty()

                response_data = dict(DebitNoteSerializer(debit_note).data)
                response_data['eis_result'] = eis_result
                
                return Response(
                    response_data,
                    status=status.HTTP_201_CREATED
                )
        except Order.DoesNotExist:
            return Response(
                {'error': 'Order not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=False, methods=['get'])
    def by_order(self, request):
        """Get debit notes for a specific order"""
        order_id = request.query_params.get('order_id')
        
        if not order_id:
            return Response(
                {'error': 'order_id parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        debit_notes = DebitNote.objects.filter(
            original_order_id=order_id,
            business_id__in=_accessible_business_ids(request.user),
        ).order_by('-created_at')
        serializer = DebitNoteSerializer(debit_notes, many=True)
        
        return Response(serializer.data)


class VoidTransactionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing Void Transactions"""
    queryset = VoidTransaction.objects.all()
    serializer_class = VoidTransactionSerializer
    permission_classes = [IsAuthenticated]

    def _can_void_order(self, user, order):
        """
        Only allow voiding for business owners, superusers, or Admin staff
        in the same business as the order.
        """
        if getattr(user, 'is_superuser', False):
            return True

        if getattr(order, 'business_id', None) and getattr(order.business, 'owner_id', None) == getattr(user, 'id', None):
            return True

        try:
            from staff.models import Staff, StaffRole

            staff_profile = Staff.objects.filter(
                user=user,
                business=order.business,
                is_active=True
            ).first()
            return bool(staff_profile and staff_profile.role == StaffRole.ADMIN)
        except Exception:
            return False
    
    def get_queryset(self):
        """Filter by branch"""
        branch_id = self.request.query_params.get('branch_id')
        
        queryset = VoidTransaction.objects.filter(business_id__in=_accessible_business_ids(self.request.user))
        
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)
        
        return queryset.order_by('-created_at')
    
    @action(detail=False, methods=['post'])
    def create_void(self, request):
        """Create a new Void Transaction and restore stock"""
        print(f"[VoidTransaction.create_void] Request data: {request.data}")
        print(f"[VoidTransaction.create_void] Request user: {request.user}")
        
        serializer = CreateVoidTransactionSerializer(data=request.data)
        print(f"[VoidTransaction.create_void] Serializer: {serializer}")
        
        is_valid = serializer.is_valid()
        print(f"[VoidTransaction.create_void] Is valid: {is_valid}")
        print(f"[VoidTransaction.create_void] Errors: {serializer.errors}")
        print(f"[VoidTransaction.create_void] Validated data: {serializer.validated_data if is_valid else 'N/A'}")
        
        if not is_valid:
            print(f"[VoidTransaction.create_void] Returning validation errors")
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            order = Order.objects.select_related('business').get(
                id=serializer.validated_data['original_order_id'],
                business_id__in=_accessible_business_ids(request.user),
            )
        except Order.DoesNotExist:
            return Response(
                {'error': 'Order not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        if not self._can_void_order(request.user, order):
            return Response(
                {'error': 'Only admin users can void sales'},
                status=status.HTTP_403_FORBIDDEN
            )

        if order.status == 'Voided' or order.void_transactions.exists():
            return Response(
                {'error': 'Order is already voided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            with transaction.atomic():
                from collections import defaultdict
                from decimal import Decimal, InvalidOperation
                from inventory.models import InventoryItem, PurchaseOrderItem

                def parse_positive_decimal(value, label):
                    try:
                        parsed = Decimal(str(value))
                    except (InvalidOperation, TypeError, ValueError):
                        print(f"[Void] Warning: Invalid decimal for {label}: {value}")
                        return Decimal('0')
                    if parsed.is_nan() or parsed.is_infinite():
                        print(f"[Void] Warning: Non-finite decimal for {label}: {value}")
                        return Decimal('0')
                    return parsed if parsed > 0 else Decimal('0')

                def parse_non_negative_decimal(value, label):
                    try:
                        parsed = Decimal(str(value))
                    except (InvalidOperation, TypeError, ValueError):
                        print(f"[Void] Warning: Invalid decimal for {label}: {value}")
                        return Decimal('0')
                    if parsed.is_nan() or parsed.is_infinite():
                        print(f"[Void] Warning: Non-finite decimal for {label}: {value}")
                        return Decimal('0')
                    return parsed if parsed >= 0 else Decimal('0')

                def resolve_product(inventory_item_id):
                    item_ref = str(inventory_item_id or '').strip()
                    if not item_ref:
                        return None
                    product_in_branch = InventoryItem.objects.filter(
                        id=item_ref,
                        branch_id=order.branch.id,
                    ).first()
                    if product_in_branch:
                        return product_in_branch
                    return InventoryItem.objects.filter(id=item_ref).first()

                def derive_stock_status(stock_units, reorder_level):
                    if stock_units > reorder_level:
                        return 'In Stock'
                    if stock_units > 0:
                        return 'Low Stock'
                    return 'Out of Stock'

                def restore_to_specific_batch(batch_id, inventory_item_id, requested_quantity):
                    if requested_quantity <= 0 or not batch_id:
                        return Decimal('0')

                    batch = PurchaseOrderItem.objects.filter(
                        id=batch_id,
                        inventory_item_id=inventory_item_id,
                        purchase_order__branch=order.branch,
                    ).first()
                    if not batch:
                        return Decimal('0')

                    quantity_ordered = parse_non_negative_decimal(
                        batch.quantity_ordered,
                        f"batch.quantity_ordered:{batch.id}"
                    )
                    quantity_remaining = parse_non_negative_decimal(
                        batch.quantity_remaining,
                        f"batch.quantity_remaining:{batch.id}"
                    )
                    quantity_used = max(Decimal('0'), quantity_ordered - quantity_remaining)
                    restore_amount = min(requested_quantity, quantity_used)
                    if restore_amount <= 0:
                        return Decimal('0')

                    batch.quantity_remaining = quantity_remaining + restore_amount
                    batch.is_dirty = True
                    batch.save(update_fields=['quantity_remaining', 'is_dirty', 'updated_at'])
                    return restore_amount

                def restore_to_batches_legacy(inventory_item_id, requested_quantity):
                    if requested_quantity <= 0:
                        return Decimal('0')

                    remaining = requested_quantity
                    restored_total = Decimal('0')
                    batches = PurchaseOrderItem.objects.filter(
                        purchase_order__branch=order.branch,
                        inventory_item_id=inventory_item_id
                    ).order_by('-created_at')

                    for batch in batches:
                        if remaining <= 0:
                            break

                        quantity_ordered = parse_non_negative_decimal(
                            batch.quantity_ordered,
                            f"batch.quantity_ordered:{batch.id}"
                        )
                        quantity_remaining = parse_non_negative_decimal(
                            batch.quantity_remaining,
                            f"batch.quantity_remaining:{batch.id}"
                        )
                        quantity_used = max(Decimal('0'), quantity_ordered - quantity_remaining)
                        if quantity_used <= 0:
                            continue

                        restore_amount = min(remaining, quantity_used)
                        if restore_amount <= 0:
                            continue

                        batch.quantity_remaining = quantity_remaining + restore_amount
                        batch.is_dirty = True
                        batch.save(update_fields=['quantity_remaining', 'is_dirty', 'updated_at'])
                        remaining -= restore_amount
                        restored_total += restore_amount

                    return restored_total

                print(f"[VoidTransaction.create_void] Order found: {order.id}")
                
                # Generate void number
                void_number = f"VOID-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
                
                # RESTORE STOCK:
                # 1) Primary path: replay exact per-order-item batch trace (batch_consumption).
                # 2) Legacy fallback: LIFO restore for orders that do not have trace data.
                order_items = list(order.items.all().order_by('created_at', 'id'))
                legacy_restore_requests = defaultdict(lambda: Decimal('0'))
                direct_inventory_restore = defaultdict(lambda: Decimal('0'))
                batch_restore_totals = defaultdict(lambda: Decimal('0'))

                for order_item in order_items:
                    line_quantity = parse_positive_decimal(
                        order_item.quantity,
                        f"order_item.quantity:{order_item.id}"
                    )
                    print(
                        f"[Void] Processing order item: {order_item.name}, "
                        f"quantity: {line_quantity}, inventory_item_id: {order_item.inventory_item_id}"
                    )

                    raw_trace_entries = (
                        order_item.batch_consumption
                        if isinstance(order_item.batch_consumption, list)
                        else []
                    )
                    parsed_trace_entries = []
                    for index, raw_entry in enumerate(raw_trace_entries):
                        if not isinstance(raw_entry, dict):
                            continue
                        inventory_item_id = str(
                            raw_entry.get('inventory_item_id')
                            or order_item.inventory_item_id
                            or ''
                        ).strip()
                        if not inventory_item_id:
                            continue
                        raw_batch_id = raw_entry.get('batch_id')
                        batch_id = str(raw_batch_id).strip() if raw_batch_id else None
                        entry_quantity = parse_positive_decimal(
                            raw_entry.get('quantity'),
                            f"batch_consumption.quantity:{order_item.id}:{index}"
                        )
                        if entry_quantity <= 0:
                            continue
                        parsed_trace_entries.append({
                            'inventory_item_id': inventory_item_id,
                            'batch_id': batch_id,
                            'quantity': entry_quantity,
                        })

                    if parsed_trace_entries:
                        print(
                            f"[Void] Using batch trace for order item {order_item.id}: "
                            f"{len(parsed_trace_entries)} entries"
                        )
                        for trace_entry in parsed_trace_entries:
                            inventory_item_id = trace_entry['inventory_item_id']
                            batch_id = trace_entry['batch_id']
                            requested_quantity = trace_entry['quantity']
                            restored_specific = restore_to_specific_batch(
                                batch_id=batch_id,
                                inventory_item_id=inventory_item_id,
                                requested_quantity=requested_quantity,
                            )

                            if restored_specific > 0:
                                batch_restore_totals[inventory_item_id] += restored_specific
                                print(
                                    f"[Void] ✓ Restored {restored_specific} to original batch {batch_id} "
                                    f"for inventory item {inventory_item_id}"
                                )

                            remaining_quantity = requested_quantity - restored_specific
                            if remaining_quantity > 0:
                                legacy_restore_requests[inventory_item_id] += remaining_quantity
                                print(
                                    f"[Void] Trace fallback queued: {remaining_quantity} for "
                                    f"inventory item {inventory_item_id} (batch {batch_id})"
                                )
                    else:
                        fallback_item_id = str(order_item.inventory_item_id or '').strip()
                        if fallback_item_id and line_quantity > 0:
                            legacy_restore_requests[fallback_item_id] += line_quantity
                            print(
                                f"[Void] Legacy fallback queued: {line_quantity} for item "
                                f"{fallback_item_id} (no batch trace on order item)"
                            )

                for inventory_item_id, requested_quantity in legacy_restore_requests.items():
                    restored_legacy = restore_to_batches_legacy(
                        inventory_item_id=inventory_item_id,
                        requested_quantity=requested_quantity,
                    )
                    if restored_legacy > 0:
                        batch_restore_totals[inventory_item_id] += restored_legacy
                        print(
                            f"[Void] Legacy batch restore: {inventory_item_id} "
                            f"restored {restored_legacy} from fallback batches"
                        )
                    remaining_after_legacy = requested_quantity - restored_legacy
                    if remaining_after_legacy > 0:
                        direct_inventory_restore[inventory_item_id] += remaining_after_legacy
                        print(
                            f"[Void] Direct inventory restore required: {inventory_item_id} "
                            f"+{remaining_after_legacy}"
                        )

                inventory_item_ids_to_update = set(batch_restore_totals.keys()) | set(direct_inventory_restore.keys())

                for inventory_item_id in inventory_item_ids_to_update:
                    product = resolve_product(inventory_item_id)
                    if not product:
                        print(f"[Void] ✗ WARNING: Could not find product {inventory_item_id} for stock refresh")
                        continue

                    current_stock = parse_non_negative_decimal(
                        product.stock_units,
                        f"product.stock_units:{product.id}"
                    )
                    restored_to_batches = parse_non_negative_decimal(
                        batch_restore_totals.get(inventory_item_id, Decimal('0')),
                        f"batch_restore_total:{inventory_item_id}"
                    )
                    direct_restore_quantity = parse_non_negative_decimal(
                        direct_inventory_restore.get(inventory_item_id, Decimal('0')),
                        f"direct_restore:{inventory_item_id}"
                    )
                    total_restore_quantity = restored_to_batches + direct_restore_quantity

                    if total_restore_quantity <= 0:
                        print(
                            f"[Void] No stock delta to apply for inventory item {inventory_item_id}; "
                            f"skipping inventory update."
                        )
                        continue

                    # Apply void as a positive stock delta.
                    # Do not force stock to batch totals here, because non-batch adjustments
                    # may exist and void must never reduce stock.
                    new_stock = current_stock + total_restore_quantity

                    reorder_level = parse_non_negative_decimal(
                        product.reorder_level,
                        f"product.reorder_level:{product.id}"
                    )
                    product.stock_units = new_stock
                    product.value = new_stock * (product.cost or Decimal('0'))
                    product.status = derive_stock_status(new_stock, reorder_level)
                    product.is_dirty = True
                    product.save(
                        update_fields=['stock_units', 'value', 'status', 'is_dirty', 'updated_at']
                    )

                    print(
                        f"[Void] ✓ Refreshed inventory {product.name} ({inventory_item_id}): "
                        f"{current_stock} -> {new_stock}"
                    )
                
                # Create void transaction
                void_transaction = VoidTransaction.objects.create(
                    business=order.business,
                    branch=order.branch,
                    original_order=order,
                    void_number=void_number,
                    void_reason=serializer.validated_data.get('void_reason', 'other'),
                    reason_description=serializer.validated_data['reason_description'],
                    supporting_documents=serializer.validated_data.get('supporting_documents', []),
                    voided_amount=order.net_amount,
                    voided_vat=order.vat_amount,
                    refund_method=serializer.validated_data.get('refund_method', 'none'),
                    refund_amount=serializer.validated_data.get('refund_amount', Decimal('0.00')),
                    refund_processed=(
                        serializer.validated_data.get('refund_method', 'none') != 'none'
                        and serializer.validated_data.get('refund_amount', Decimal('0.00')) > 0
                    ),
                    refund_processed_at=(
                        timezone.now()
                        if serializer.validated_data.get('refund_method', 'none') != 'none'
                        and serializer.validated_data.get('refund_amount', Decimal('0.00')) > 0
                        else None
                    ),
                    refund_processed_by=(
                        request.user
                        if serializer.validated_data.get('refund_method', 'none') != 'none'
                        and serializer.validated_data.get('refund_amount', Decimal('0.00')) > 0
                        else None
                    ),
                    created_by=request.user,
                )

                eis_result = None
                if _should_submit_eis_correction(order):
                    eis_result = CorrectionService.submit_void_transaction(void_transaction)
                else:
                    void_transaction.mark_dirty()
                
                # Keep fiscal identity immutable, but mark the local sale state
                # as voided after the correcting document has been recorded.
                if order.is_fiscal_locked:
                    Order.objects.filter(pk=order.pk).update(
                        status='Voided',
                        is_dirty=True,
                        updated_at=timezone.now(),
                    )
                    order.refresh_from_db()
                else:
                    order.status = 'Voided'
                    order.is_dirty = True
                    order.save(update_fields=['status', 'is_dirty', 'updated_at'])
                
                print(f"[Void] Void transaction created: {void_number}, Stock restored for order {order.id}")
                
                # Return both void transaction and updated order so frontend can update immediately
                from .serializers import OrderSerializer
                
                response_data = {
                    'void_transaction': VoidTransactionSerializer(void_transaction).data,
                    'order': OrderSerializer(order).data,
                    'eis_result': eis_result,
                }
                
                return Response(response_data, status=status.HTTP_201_CREATED)
        except Exception as e:
            print(f"[Void] Error creating void transaction: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=False, methods=['get'])
    def by_order(self, request):
        """Get void transactions for a specific order"""
        order_id = request.query_params.get('order_id')
        
        if not order_id:
            return Response(
                {'error': 'order_id parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        void_transactions = VoidTransaction.objects.filter(
            original_order_id=order_id,
            business_id__in=_accessible_business_ids(request.user),
        ).order_by('-created_at')
        serializer = VoidTransactionSerializer(void_transactions, many=True)
        
        return Response(serializer.data)
