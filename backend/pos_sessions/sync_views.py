"""
Sync endpoints for POS Sessions and Orders
Implements the recommended sync flow:
1. Frontend PUSHES local changes
2. Backend validates and resolves conflicts
3. Frontend PULLS server changes
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.conf import settings
from django.utils import timezone
from datetime import datetime
from django.db import transaction
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .models import Session, Order, OrderItem
from .serializers import SessionSerializer, OrderSerializer
from .tax_utils import lock_tax_rate_on_use
from business.models import Business, Branch, TaxRate

NON_BLOCKING_OFFLINE_DRY_RUN_REASONS = {
    'submission_call_failed',
    'connection_error',
    'timeout',
    'network_error',
    'eis_unreachable',
    'mra_server_error',
}


class InsufficientStockError(ValueError):
    """Raised when a sale would consume more stock than is available."""

    def __init__(self, product_name, requested, available):
        self.product_name = str(product_name or 'Product')
        self.requested = requested
        self.available = available
        super().__init__(
            f'Insufficient stock for {self.product_name}: '
            f'requested {_format_stock_quantity(requested)}, '
            f'available {_format_stock_quantity(available)}.'
        )


class InvalidDiscountError(ValueError):
    """Raised when a discount would make a sale line zero or negative."""

    def __init__(self, product_name, discount_amount, line_amount):
        self.product_name = str(product_name or 'Product')
        self.discount_amount = discount_amount
        self.line_amount = line_amount
        super().__init__(
            f'Discount for {self.product_name} must be less than the item total. '
            f'Discount {_format_money(discount_amount)} cannot be applied to '
            f'item total {_format_money(line_amount)}.'
        )


def _format_stock_quantity(value):
    quantity = _to_decimal(value, Decimal('0')).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
    text = format(quantity, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _format_money(value):
    return format(_quantize_money(value), ',.2f')

def _to_optional_decimal(value):
    """Parse optional numeric payload values safely."""
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_decimal(value, default=Decimal('0')):
    """Parse numeric payload values safely."""
    parsed = _to_optional_decimal(value)
    return parsed if parsed is not None else default


def _parse_bool(value, default=False):
    if value in (None, '', 'null', 'undefined'):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _resolve_line_base_amount(item_data, tax_calculation_method='inclusive'):
    """
    Resolve the pre-tax line amount basis from mixed client payloads.

    Supports both:
    - unit pricing payloads (price x quantity)
    - legacy variable-price payloads (price sent as line total)
    """
    quantity = _to_decimal(item_data.get('quantity'), Decimal('0'))
    price = _to_decimal(item_data.get('price'), Decimal('0'))

    explicit_original_line = (
        _to_optional_decimal(item_data.get('lineAmountBeforeDiscount'))
        or _to_optional_decimal(item_data.get('line_amount_before_discount'))
        or _to_optional_decimal(item_data.get('originalLineAmount'))
        or _to_optional_decimal(item_data.get('original_line_amount'))
        or _to_optional_decimal(item_data.get('grossBeforeDiscount'))
        or _to_optional_decimal(item_data.get('gross_before_discount'))
    )
    explicit_subtotal = _to_optional_decimal(item_data.get('subtotal'))
    explicit_tax = _to_optional_decimal(item_data.get('taxAmount'))
    if explicit_tax is None:
        explicit_tax = _to_optional_decimal(item_data.get('tax_amount'))
    explicit_total = _to_optional_decimal(item_data.get('total'))

    method = str(tax_calculation_method or 'inclusive').strip().lower()
    line_base = None

    if method == 'exclusive':
        # For exclusive tax, base is net/subtotal before tax.
        if explicit_original_line is not None and explicit_original_line >= 0:
            line_base = explicit_original_line
        elif explicit_subtotal is not None and explicit_subtotal >= 0:
            line_base = explicit_subtotal
        elif explicit_total is not None and explicit_tax is not None:
            line_base = explicit_total - explicit_tax
        elif explicit_total is not None and explicit_total >= 0:
            line_base = explicit_total
    else:
        # For inclusive tax, base is gross/total (price already includes tax).
        if explicit_original_line is not None and explicit_original_line >= 0:
            line_base = explicit_original_line
        elif explicit_total is not None and explicit_total >= 0:
            line_base = explicit_total
        elif explicit_subtotal is not None and explicit_tax is not None:
            line_base = explicit_subtotal + explicit_tax
        elif explicit_subtotal is not None and explicit_subtotal >= 0:
            line_base = explicit_subtotal

    if line_base is None:
        line_base = quantity * price

    return max(Decimal('0'), line_base)


def _resolve_line_discount_amount(item_data, line_base_amount):
    line_base_amount = _quantize_money(line_base_amount)
    item_name = item_data.get('name') or item_data.get('description') or 'Product'
    discount_amount = _to_decimal(
        item_data.get('discountAmount')
        if item_data.get('discountAmount') is not None
        else item_data.get('discount_amount'),
        Decimal('0'),
    )
    discount_type = str(
        item_data.get('discountType')
        or item_data.get('discount_type')
        or ''
    ).strip().lower()
    discount_value = _to_decimal(
        item_data.get('discountValue')
        if item_data.get('discountValue') is not None
        else item_data.get('discount_value'),
        Decimal('0'),
    )

    if discount_amount <= 0 and discount_type == 'percentage' and discount_value > 0:
        discount_amount = Decimal(str(line_base_amount or 0)) * discount_value / Decimal('100')

    if discount_amount <= 0:
        return Decimal('0.00')

    discount_amount = _quantize_money(discount_amount)
    if discount_type == 'percentage' and discount_value >= Decimal('100'):
        raise InvalidDiscountError(item_name, discount_amount, line_base_amount)
    if discount_type == 'fixed' and discount_value >= line_base_amount:
        raise InvalidDiscountError(item_name, _quantize_money(discount_value), line_base_amount)
    if discount_amount >= line_base_amount:
        raise InvalidDiscountError(item_name, discount_amount, line_base_amount)

    return discount_amount


def _normalize_tax_type(value):
    normalized = str(value or '').strip().lower()
    if normalized in {'zero', 'zero_rated', 'zero-rated', 'vat_zero'}:
        return 'zero'
    if normalized in {'exempt', 'vat_exempt'}:
        return 'exempt'
    return 'standard'


def _quantize_money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)



def _mapping_uses_standard_vat(tax_type, tax_rate) -> bool:
    normalized_tax_type = _normalize_tax_type(tax_type)
    rate = _to_decimal(tax_rate, Decimal('0'))
    return normalized_tax_type == 'standard' and rate > 0


def _is_eis_taxpayer_non_vat(business) -> bool:
    try:
        from mra_eis.services import ConfigurationService

        return ConfigurationService.is_taxpayer_explicitly_non_vat(business)
    except Exception:
        taxpayer_type = str(getattr(business, 'mra_taxpayer_type', '') or '').strip().upper().replace('-', '_')
        if taxpayer_type in {'NON_VAT', 'NONVAT', 'NON_VAT_REGISTERED'}:
            return True
        if taxpayer_type == 'VAT':
            return False
        return getattr(business, 'vat_registered', None) is False


def _calculate_item_tax_values(line_base_amount, tax_type='standard', tax_rate=Decimal('0'), tax_calculation_method='inclusive'):
    """
    Return normalized per-line tax values.
    line_base_amount semantics:
    - inclusive: gross value (tax included)
    - exclusive: subtotal/net value (tax excluded)
    """
    base = max(Decimal('0'), Decimal(str(line_base_amount or 0)))
    normalized_tax_type = _normalize_tax_type(tax_type)
    method = str(tax_calculation_method or 'inclusive').strip().lower()
    rate = max(Decimal('0'), Decimal(str(tax_rate or 0)))

    if normalized_tax_type in {'zero', 'exempt'} or rate <= 0:
        return {
            'subtotal': _quantize_money(base),
            'tax_amount': Decimal('0.00'),
            'total': _quantize_money(base),
            'tax_type': normalized_tax_type,
            'tax_method': 'inclusive' if method != 'exclusive' else 'exclusive',
            'rate_percent': _quantize_money(rate),
        }

    effective_rate = rate / Decimal('100')
    if method == 'exclusive':
        subtotal = base
        tax_amount = subtotal * effective_rate
        total = subtotal + tax_amount
    else:
        total = base
        tax_amount = total * effective_rate / (Decimal('1') + effective_rate)
        subtotal = total - tax_amount

    return {
        'subtotal': _quantize_money(subtotal),
        'tax_amount': _quantize_money(tax_amount),
        'total': _quantize_money(total),
        'tax_type': normalized_tax_type,
        'tax_method': 'exclusive' if method == 'exclusive' else 'inclusive',
        'rate_percent': _quantize_money(rate),
    }


def _resolve_user_business(user):
    """
    Resolve business for both owners and staff users.
    """
    business = Business.objects.filter(owner=user).first()
    if business:
        return business

    try:
        from staff.models import Staff
        staff = Staff.objects.select_related('business').filter(user=user, is_active=True).first()
        return staff.business if staff and staff.business else None
    except Exception:
        return None


def _build_order_sync_payload(order):
    """Return normalized order fields used by frontend sync acknowledgements."""
    if not order:
        return {}

    return {
        'server_id': str(order.id),
        'order_number': order.order_number,
        'customer_name': order.customer_name,
        'customer_phone': order.customer_phone,
        'customer_tin': order.customer_tin,
        'customer_email': order.customer_email,
        'customer_address': order.customer_address,
        'customer_notes': order.customer_notes,
        'buyer_name': order.buyer_name,
        'buyer_tin': order.buyer_tin,
        'buyer_authorization_code': order.buyer_authorization_code,
        'is_export': order.is_export,
        'is_relief_supply': order.is_relief_supply,
        'vat5_project_number': order.vat5_project_number,
        'vat5_certificate_number': order.vat5_certificate_number,
        'vat5_quantity': float(order.vat5_quantity) if order.vat5_quantity is not None else None,
        'eis_validation_metadata': order.eis_validation_metadata,
        'mra_submission': (
            order.eis_validation_metadata.get('mra_submission')
            if isinstance(order.eis_validation_metadata, dict)
            else None
        ),
        'fiscal_invoice_number': order.fiscal_invoice_number,
        'eis_status': order.eis_status,
        'eis_uuid': order.eis_uuid,
        'eis_submitted_at': order.eis_submitted_at.isoformat() if order.eis_submitted_at else None,
        'qr_code_payload': order.qr_code_payload,
        'digital_signature': order.digital_signature,
        'net_amount': float(order.net_amount) if order.net_amount is not None else None,
        'vat_amount': float(order.vat_amount) if order.vat_amount is not None else None,
        'gross_amount': float(order.gross_amount) if order.gross_amount is not None else None,
        'updated_at': order.updated_at.isoformat() if order.updated_at else None,
    }


def _should_block_sales_if_eis_down(business_settings):
    """Whether live-mode compliance requires blocking sales when EIS is down."""
    if not getattr(settings, 'MRA_EIS_IS_LIVE', False):
        return False
    if not business_settings:
        return False
    return bool(getattr(business_settings, 'enable_eis', False)) and bool(
        getattr(business_settings, 'block_sales_if_eis_down', True)
    )


def _get_mra_submission_block_reason(mra_result):
    """Return compliance block reason when MRA submission was not confirmed."""
    if not isinstance(mra_result, dict):
        return 'MRA EIS submission did not return a valid response.'

    submission_message = str(mra_result.get('submission_message') or '').strip()
    submission_state = str(mra_result.get('submission_state') or '').strip()
    if submission_state in {'accepted', 'offline_queued'}:
        return None

    endpoint_key = str(mra_result.get('endpoint') or '').strip().lower()

    response_payload = mra_result.get('response')
    reason = 'eis_unreachable'
    if isinstance(response_payload, dict):
        reason = str(response_payload.get('reason') or reason).strip().lower()

    # MRA allows offline issuance only for genuine connectivity failures.
    if (
        endpoint_key == 'report_sale_offline'
        and mra_result.get('dry_run')
        and reason in NON_BLOCKING_OFFLINE_DRY_RUN_REASONS
    ):
        return None

    if mra_result.get('dry_run'):
        if submission_message:
            return submission_message
        return f'MRA EIS unavailable ({reason}).'

    eis_status = str(mra_result.get('eis_status') or '').upper()
    if eis_status != 'SUBMITTED':
        if submission_message:
            return submission_message
        return f'MRA EIS did not confirm submission (status: {eis_status or "UNKNOWN"}).'

    return None


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def sync_push(request):
    """
    Receive local changes from frontend and apply them to backend
    
    Expected request body:
    {
        "last_synced_at": "2026-01-09T10:00:00Z",
        "branch_id": "branch-uuid",
        "changes": [
            {
                "id": "session-uuid",
                "entity_type": "Session",
                "op": "create|update|delete",
                "data": {...},
                "timestamp": "2026-01-09T10:05:00Z"
            }
        ]
    }
    """
    try:
        last_synced_at = request.data.get('last_synced_at')
        branch_id = request.data.get('branch_id')
        changes = request.data.get('changes', [])
        try:
            from mra_eis.services import TerminalService

            request_device_serial = TerminalService.extract_request_device_serial(request)
        except Exception:
            request_device_serial = ''
        
        print(f"[Sync Push Sessions] Received {len(changes)} changes from frontend")
        print(f"[Sync Push Sessions] Last synced: {last_synced_at}, Branch: {branch_id}")
        
        # Get user's business (owner or assigned staff business)
        business = _resolve_user_business(request.user)
        if not business:
            return Response(
                {'error': 'User does not have an associated business'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        results = {
            'acknowledged': [],
            'conflicts': [],
            'errors': []
        }
        
        with transaction.atomic():
            for change in changes:
                try:
                    entity_type = change.get('entity_type')
                    operation = change.get('op')
                    data = change.get('data')
                    entity_id = change.get('id')
                    
                    print(f"[Sync Push Sessions] Processing {operation} for {entity_type} {entity_id}")
                    
                    if entity_type == 'Session':
                        if operation == 'create':
                            result = handle_create_session(entity_id, data, business, branch_id, request.user)
                        elif operation == 'update':
                            result = handle_update_session(entity_id, data, business, branch_id)
                        elif operation == 'delete':
                            result = handle_delete_session(entity_id, business, branch_id)
                        else:
                            results['errors'].append({
                                'id': entity_id,
                                'error': f'Unknown operation: {operation}'
                            })
                            continue
                    elif entity_type == 'Order':
                        if operation == 'create':
                            result = handle_create_order(
                                entity_id,
                                data,
                                business,
                                branch_id,
                                request.user,
                                request_device_serial=request_device_serial,
                            )
                        elif operation == 'update':
                            result = handle_update_order(
                                entity_id,
                                data,
                                business,
                                branch_id,
                                request.user,
                                request_device_serial=request_device_serial,
                            )
                        elif operation == 'delete':
                            result = handle_delete_order(entity_id, business, branch_id)
                        else:
                            results['errors'].append({
                                'id': entity_id,
                                'error': f'Unknown operation: {operation}'
                            })
                            continue
                    else:
                        results['errors'].append({
                            'id': entity_id,
                            'error': f'Unknown entity type: {entity_type}'
                        })
                        continue
                    
                    if result['success']:
                        ack_payload = {
                            'id': entity_id,
                            'status': 'success',
                            'server_id': result.get('server_id', entity_id)
                        }

                        for key in [
                            'order_number',
                            'customer_name',
                            'customer_phone',
                            'customer_tin',
                            'customer_email',
                            'customer_address',
                            'customer_notes',
                            'buyer_name',
                            'buyer_tin',
                            'buyer_authorization_code',
                            'is_export',
                            'is_relief_supply',
                            'vat5_project_number',
                            'vat5_certificate_number',
                            'vat5_quantity',
                            'eis_validation_metadata',
                            'mra_submission',
                            'fiscal_invoice_number',
                            'eis_status',
                            'eis_uuid',
                            'eis_submitted_at',
                            'qr_code_payload',
                            'digital_signature',
                            'net_amount',
                            'vat_amount',
                            'gross_amount',
                            'updated_at',
                        ]:
                            if key in result:
                                ack_payload[key] = result.get(key)

                        results['acknowledged'].append(ack_payload)
                        print(f"[Sync Push Sessions] Successfully processed {operation} for {entity_id}")
                    else:
                        error_payload = {
                            'id': entity_id,
                            'error': result.get('error', 'Unknown error')
                        }
                        if result.get('reason'):
                            error_payload['reason'] = result.get('reason')
                        if result.get('mra_submission'):
                            error_payload['mra_submission'] = result.get('mra_submission')
                        results['errors'].append(error_payload)
                        print(f"[Sync Push Sessions] Error processing {operation} for {entity_id}: {result.get('error')}")
                        
                except Exception as e:
                    print(f"[Sync Push Sessions] Exception processing change: {str(e)}")
                    results['errors'].append({
                        'id': change.get('id'),
                        'error': str(e)
                    })
        
        return Response({
            'synced_at': timezone.now().isoformat(),
            'results': results
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        print(f"[Sync Push Sessions] Fatal error: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sync_pull(request):
    """
    Return server changes since last sync
    
    Query parameters:
    - since: ISO datetime string (e.g., "2026-01-09T10:00:00Z")
    - branch_id: Branch UUID
    
    Returns:
    {
        "pulled_at": "2026-01-09T10:10:00Z",
        "changes": {
            "sessions": [...],
            "orders": [...]
        }
    }
    """
    try:
        since_str = request.query_params.get('since')
        branch_id = request.query_params.get('branch_id')
        
        if not since_str:
            return Response(
                {'error': 'since parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not branch_id:
            return Response(
                {'error': 'branch_id parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Parse since datetime
        try:
            since_dt = datetime.fromisoformat(since_str.replace('Z', '+00:00'))
        except ValueError:
            return Response(
                {'error': 'Invalid datetime format for since parameter'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        print(f"[Sync Pull Sessions] Pulling changes since {since_dt} for branch {branch_id}")
        
        # Get user's business (owner or assigned staff business)
        business = _resolve_user_business(request.user)
        if not business:
            return Response(
                {'error': 'User does not have an associated business'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Resolve branch identifier: support 'BRN-<number>' and numeric ids, fallback to name
        from business.models import Branch
        resolved_branch = None
        if isinstance(branch_id, str):
            import re
            m = re.match(r"^BRN-(\d+)$", branch_id)
            if m:
                try:
                    resolved_branch = Branch.objects.get(pk=int(m.group(1)), business=business)
                except Branch.DoesNotExist:
                    pass
            elif branch_id.isdigit():
                try:
                    resolved_branch = Branch.objects.get(pk=int(branch_id), business=business)
                except Branch.DoesNotExist:
                    pass
        if resolved_branch is None:
            try:
                resolved_branch = Branch.objects.get(pk=branch_id, business=business)
            except Exception:
                try:
                    resolved_branch = Branch.objects.get(name=branch_id, business=business)
                except Branch.DoesNotExist:
                    return Response(
                        {'error': f"Branch '{branch_id}' not found for this business"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
        
        # Fetch updated sessions
        sessions = Session.objects.filter(
            business=business,
            branch=resolved_branch,
            updated_at__gte=since_dt
        ).order_by('updated_at')
        
        # Fetch updated orders
        orders = Order.objects.filter(
            business=business,
            branch=resolved_branch,
            updated_at__gte=since_dt
        ).order_by('updated_at')
        
        print(f"[Sync Pull Sessions] Found {sessions.count()} updated sessions and {orders.count()} updated orders")
        
        sessions_serializer = SessionSerializer(sessions, many=True)
        orders_serializer = OrderSerializer(orders, many=True)
        
        return Response({
            'pulled_at': timezone.now().isoformat(),
            'changes': {
                'sessions': sessions_serializer.data,
                'orders': orders_serializer.data
            }
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        print(f"[Sync Pull Sessions] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )


def handle_create_session(session_id, data, business, branch_id, user):
    """Handle creation of session from frontend"""
    try:
        from business.models import Branch
        
        # Validate branch exists
        try:
            branch = Branch.objects.get(id=branch_id, business=business)
        except Branch.DoesNotExist:
            return {
                'success': False,
                'error': f'Branch {branch_id} not found for this business'
            }
        
        # Check if session already exists
        existing = Session.objects.filter(id=session_id, business=business).first()
        if existing:
            print(f"[Sync Sessions] Session {session_id} already exists, updating instead")
            return handle_update_session(session_id, data, business, branch_id)

        # Enforce one active session per user (across branches).
        existing_active = Session.objects.filter(
            user=user,
            business=business,
            status='active'
        ).select_related('branch').order_by('-started_at').first()
        if existing_active:
            branch_name = getattr(existing_active.branch, 'name', '') or str(existing_active.branch_id)
            return {
                'success': False,
                'error': (
                    'You already have an active session. '
                    f'Close it before starting a new one (branch: {branch_name}).'
                )
            }

        # Create new session
        # Note: Frontend sends camelCase, but sync service converts to snake_case
        # So we need to check both formats for compatibility
        session_data = {
            'id': session_id,
            'business': business,
            'branch': branch,
            'user': user,  # Always include user
            'status': data.get('status', 'active'),
            'pump_name': data.get('pump_name') or data.get('pumpName'),
            'opening_float': float(data.get('opening_float') or data.get('openingFloat', 0)) or 0,
            'expected_cash': float(data.get('expected_cash') or data.get('expectedCash', 0)) or 0,
            'actual_cash': float(data.get('actual_cash') or data.get('actualCash')) if (data.get('actual_cash') or data.get('actualCash')) else None,
            'closing_float': float(data.get('closing_float') or data.get('closingFloat')) if (data.get('closing_float') or data.get('closingFloat')) else None,
            'difference': float(data.get('difference')) if data.get('difference') else None,
            'total_sales': float(data.get('total_sales') or data.get('totalSales', 0)) or 0,
            'total_cash_sales': float(data.get('total_cash_sales') or data.get('totalCashSales', 0)) or 0,
            'total_card_sales': float(data.get('total_card_sales') or data.get('totalCardSales', 0)) or 0,
            'total_mobile_money_sales': float(data.get('total_mobile_money_sales') or data.get('totalMobileMoneySales', 0)) or 0,
            'total_on_account_sales': float(data.get('total_on_account_sales') or data.get('totalOnAccountSales', 0)) or 0,
            'total_other_sales': float(data.get('total_other_sales') or data.get('totalOtherSales', 0)) or 0,
            'total_tips': float(data.get('total_tips') or data.get('totalTips', 0)) or 0,
            'opening_stock': data.get('opening_stock') or data.get('openingStock', []),
            'closing_stock': data.get('closing_stock') or data.get('closingStock', []),
            'started_at': data.get('started_at') or data.get('startedAt'),
            'closed_at': data.get('closed_at') or data.get('closedAt'),
        }
        
        # Validate that started_at is present for new sessions
        if not session_data.get('started_at'):
            return {
                'success': False,
                'error': 'started_at is required for new sessions'
            }
        
        # Remove None values
        session_data = {k: v for k, v in session_data.items() if v is not None}
        
        session = Session.objects.create(**session_data)
        print(f"[Sync Sessions] Created session {session_id}")
        
        return {
            'success': True,
            'server_id': str(session.id)
        }
        
    except Exception as e:
        print(f"[Sync Sessions] Error creating session: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


def handle_update_session(session_id, data, business, branch_id):
    """Handle update of session from frontend"""
    try:
        session = Session.objects.get(id=session_id, business=business, branch_id=branch_id)
        
        # Update fields
        if 'status' in data:
            session.status = data['status']
        if 'pumpName' in data or 'pump_name' in data:
            session.pump_name = data.get('pump_name') or data.get('pumpName')
        if 'openingFloat' in data:
            session.opening_float = data['openingFloat']
        if 'expectedCash' in data:
            session.expected_cash = data['expectedCash']
        if 'actualCash' in data:
            session.actual_cash = data['actualCash']
        if 'closingFloat' in data:
            session.closing_float = data['closingFloat']
        if 'difference' in data:
            session.difference = data['difference']
        if 'totalSales' in data:
            session.total_sales = data['totalSales']
        if 'totalCashSales' in data:
            session.total_cash_sales = data['totalCashSales']
        if 'totalCardSales' in data:
            session.total_card_sales = data['totalCardSales']
        if 'totalMobileMoneySales' in data:
            session.total_mobile_money_sales = data['totalMobileMoneySales']
        if 'totalOnAccountSales' in data:
            session.total_on_account_sales = data['totalOnAccountSales']
        if 'totalOtherSales' in data:
            session.total_other_sales = data['totalOtherSales']
        if 'totalTips' in data:
            session.total_tips = data['totalTips']
        if 'openingStock' in data:
            session.opening_stock = data['openingStock']
        if 'closingStock' in data:
            session.closing_stock = data['closingStock']
        if 'closedAt' in data:
            session.closed_at = data['closedAt']
        
        session.save()
        print(f"[Sync Sessions] Updated session {session_id}")
        
        return {
            'success': True,
            'server_id': str(session.id)
        }
        
    except Session.DoesNotExist:
        print(f"[Sync Sessions] Session {session_id} not found, creating instead")
        return handle_create_session(session_id, data, business, branch_id, None)
    except Exception as e:
        print(f"[Sync Sessions] Error updating session: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def handle_delete_session(session_id, business, branch_id):
    """Handle deletion of session from frontend"""
    try:
        session = Session.objects.get(id=session_id, business=business, branch_id=branch_id)
        session.delete()
        print(f"[Sync Sessions] Deleted session {session_id}")
        
        return {
            'success': True,
            'server_id': session_id
        }
        
    except Session.DoesNotExist:
        print(f"[Sync Sessions] Session {session_id} not found for deletion")
        return {
            'success': True,
            'server_id': session_id
        }
    except Exception as e:
        print(f"[Sync Sessions] Error deleting session: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def handle_create_order(order_id, data, business, branch_id, user, *, request_device_serial=''):
    """Handle creation of order from frontend"""
    from business.models import Branch
    from django.db import IntegrityError
    from inventory.models import InventoryItem, PurchaseOrderItem, MRAProductMapping
    from decimal import Decimal
    
    try:
        # Validate branch exists
        try:
            branch = Branch.objects.get(id=branch_id, business=business)
        except Branch.DoesNotExist:
            return {
                'success': False,
                'error': f'Branch {branch_id} not found for this business'
            }
        
        # Check if order already exists by ID
        existing = Order.objects.filter(id=order_id, business=business).first()
        if existing:
            print(f"[Sync Sessions] Order {order_id} already exists - returning success without creating duplicate")
            return {
                'success': True,
                **_build_order_sync_payload(existing)
            }
        
        business_settings = getattr(business, 'settings', None)
        eis_enabled = bool(getattr(business_settings, 'enable_eis', False))
        block_sales_if_tax_mapping_missing = bool(
            getattr(business_settings, 'block_sales_if_tax_mapping_missing', False)
        )

        # CRITICAL: Validate that ALL products have approved+synced MRA mappings
        # Only enforce when block_sales_if_tax_mapping_missing is enabled.
        print(f"[Sync Sessions] Validating MRA mappings for order {order_id}")
        if block_sales_if_tax_mapping_missing and data.get('items'):
            unmapped_products = []
            unapproved_products = []
            unsynced_products = []
            for item_data in data['items']:
                # The item_data contains 'inventoryItemId' which is the actual inventory item ID
                # NOT the order item ID (which is in 'id')
                item_id = item_data.get('inventoryItemId') or item_data.get('inventory_item_id') or item_data.get('id', '')
                item_name = item_data.get('name', 'Unknown')
                
                print(f"[Sync Sessions] Checking MRA mapping for item: id={item_id}, name={item_name}, full_data={item_data}")
                
                try:
                    # Check if MRA mapping exists and is approved
                    all_mappings = list(MRAProductMapping.objects.filter(inventory_item_id=item_id))
                    approved_and_synced = next(
                        (m for m in all_mappings if m.is_approved and m.mra_synced),
                        None
                    )
                    approved_but_unsynced = next(
                        (m for m in all_mappings if m.is_approved and not m.mra_synced),
                        None
                    )
                    any_mapping = all_mappings[0] if all_mappings else None

                    if approved_and_synced:
                        print(f"[Sync Sessions] ✓ Product {item_name} ({item_id}) has approved+synced MRA mapping")
                        if _mapping_uses_standard_vat(
                            approved_and_synced.mra_tax_type,
                            approved_and_synced.mra_tax_rate,
                        ):
                            print(
                                f"[Sync Sessions] ℹ Product {item_name} ({item_id}) uses MRA-approved "
                                f"standard VAT ({approved_and_synced.mra_tax_rate}%). Keeping MRA tax metadata."
                            )
                    elif approved_but_unsynced:
                        print(f"[Sync Sessions] ⚠ Product {item_name} ({item_id}) mapping is approved but NOT synced")
                        unsynced_products.append(item_name)
                    elif any_mapping:
                        print(f"[Sync Sessions] ⚠ Product {item_name} ({item_id}) has unapproved MRA mapping")
                        unapproved_products.append(item_name)
                    else:
                        print(f"[Sync Sessions] ✗ Product {item_name} ({item_id}) has NO MRA mapping")
                        unmapped_products.append(item_name)
                        
                except Exception as e:
                    print(f"[Sync Sessions] Error checking MRA mapping for {item_name}: {str(e)}")
                    import traceback
                    traceback.print_exc()
                    unmapped_products.append(item_name)
            
            # Block order if any products are unmapped or unapproved
            if unmapped_products:
                error_msg = f"Cannot create order: Products without MRA mappings: {', '.join(unmapped_products)}"
                print(f"[Sync Sessions] ✗ BLOCKED order creation - {error_msg}")
                return {
                    'success': False,
                    'error': error_msg
                }
            
            if unapproved_products:
                error_msg = f"Cannot create order: Products with unapproved MRA mappings: {', '.join(unapproved_products)}"
                print(f"[Sync Sessions] ✗ BLOCKED order creation - {error_msg}")
                return {
                    'success': False,
                    'error': error_msg
                }

            if unsynced_products:
                error_msg = f"Cannot create order: Products with unsynced MRA mappings: {', '.join(unsynced_products)}"
                print(f"[Sync Sessions] ✗ BLOCKED order creation - {error_msg}")
                return {
                    'success': False,
                    'error': error_msg
                }


        if block_sales_if_tax_mapping_missing:
            print(f"[Sync Sessions] ✓ All products have approved+synced MRA mappings - proceeding with order creation")
        else:
            print(f"[Sync Sessions] MRA mapping enforcement disabled - proceeding with order creation")
        
        def _next_order_number_for_branch(target_branch):
            last_order = Order.objects.filter(branch=target_branch).order_by('-order_number').first()
            return (last_order.order_number if last_order else 100) + 1

        # Get or generate order number.
        # IMPORTANT: Never map a different local order to an existing remote order number.
        # If the requested number already exists for another order, allocate the next available number.
        requested_order_number = int(data.get('orderNumber', 0))
        if requested_order_number <= 0:
            order_number = _next_order_number_for_branch(branch)
            print(f"[Sync Sessions] Generated order number {order_number} for order {order_id}")
        else:
            existing_by_number = Order.objects.filter(
                branch=branch,
                order_number=requested_order_number
            ).first()
            if existing_by_number:
                if str(existing_by_number.id) == str(order_id):
                    print(
                        f"[Sync Sessions] Order number {requested_order_number} belongs to the same "
                        f"order {order_id}; treating as idempotent retry"
                    )
                    return {
                        'success': True,
                        **_build_order_sync_payload(existing_by_number)
                    }

                order_number = _next_order_number_for_branch(branch)
                print(
                    f"[Sync Sessions] Order number collision for branch {branch.id}: "
                    f"requested={requested_order_number}, existing_order={existing_by_number.id}. "
                    f"Reassigned to {order_number} for order {order_id}"
                )
            else:
                order_number = requested_order_number
        
        # Create new order
        payment_method = data.get('paymentMethod') or data.get('payment_method') or 'Cash'
        print(f"[Sync Sessions] Order payment method - paymentMethod: {data.get('paymentMethod')}, payment_method: {data.get('payment_method')}, final: {payment_method}")
        print(f"[Sync Sessions] Full order data: {data}")
        
        # Calculate tax based on product-specific MRA mappings (inclusive/exclusive)
        # Each product has its own tax calculation method in its MRA mapping
        # CRITICAL: ALWAYS recalculate from items - NEVER trust frontend totals for tax
        # This ensures accurate tax when items have different tax rates (mixed tax scenario)
        
        print(f"[Sync Sessions] Frontend sent - subtotal: {data.get('subtotal')}, total: {data.get('total')}, vat: {data.get('vat_amount')}")
        
        # Initialize totals - will be recalculated from items
        subtotal = 0
        total = 0
        vat_amount = 0
        
        # Recalculate tax based on product-specific MRA mappings
        # CRITICAL: Always recalculate from items to ensure accuracy
        # This ensures order totals match the sum of item totals
        items_have_complete_price_data = False
        if data.get('items'):
            items_have_complete_price_data = all(
                (item.get('price') is not None and item.get('price') != '') or
                (item.get('total') is not None and item.get('total') != '')
                for item in data['items']
            )
            print(f"[Sync Sessions] Items have complete price data: {items_have_complete_price_data}")
        
        default_tax_rate = None
        if not eis_enabled and not block_sales_if_tax_mapping_missing:
            default_tax_rate = TaxRate.objects.filter(
                business=business,
                is_active=True,
                is_default=True,
            ).order_by('-effective_from').first()
            if not default_tax_rate:
                default_tax_rate = TaxRate.objects.filter(
                    business=business,
                    is_active=True,
                ).order_by('-effective_from').first()

        if data.get('items') and items_have_complete_price_data:
            total_vat = Decimal('0')
            total_subtotal = Decimal('0')
            total_gross = Decimal('0')
            
            for item_data in data['items']:
                # CRITICAL: Use inventoryItemId (the actual product ID), NOT id (which is the order item ID)
                item_id = item_data.get('inventoryItemId') or item_data.get('inventory_item_id') or item_data.get('id', '')
                item_quantity = _to_decimal(item_data.get('quantity'), Decimal('0'))
                item_price = _to_decimal(item_data.get('price'), Decimal('0'))
                
                print(
                    f"[Sync Sessions] Processing item {item_id}: "
                    f"qty={item_quantity}, price={item_price}"
                )
                
                try:
                    # Get MRA mapping for this product
                    mra_mapping = MRAProductMapping.objects.filter(
                        inventory_item_id=item_id,
                        is_approved=True,
                        mra_synced=True
                    ).first()
                    
                    if mra_mapping:
                        tax_calculation_method = mra_mapping.tax_calculation_method
                        tax_rate = _to_decimal(mra_mapping.mra_tax_rate, Decimal('0'))
                        tax_type = _normalize_tax_type(mra_mapping.mra_tax_type)
                        line_base_amount = _resolve_line_base_amount(item_data, tax_calculation_method)
                        discount_amount = _resolve_line_discount_amount(item_data, line_base_amount)
                        discounted_line_base_amount = max(Decimal('0'), line_base_amount - discount_amount)
                        tax_values = _calculate_item_tax_values(
                            line_base_amount=discounted_line_base_amount,
                            tax_type=tax_type,
                            tax_rate=tax_rate,
                            tax_calculation_method=tax_calculation_method,
                        )

                        print(
                            f"[Sync Sessions] Found MRA mapping for {item_id}: "
                            f"method={tax_calculation_method}, rate={tax_rate}%, tax_type={tax_type}, "
                            f"base={line_base_amount}, discount={discount_amount}"
                        )

                        total_vat += tax_values['tax_amount']
                        total_subtotal += tax_values['subtotal']
                        total_gross += tax_values['total']
                    else:
                        # No mapping found.
                        fallback_method = str(
                            item_data.get('taxCalculationMethod')
                            or item_data.get('tax_calculation_method')
                            or 'inclusive'
                        ).strip().lower()
                        fallback_tax_rate = _to_decimal(
                            item_data.get('taxRate') or item_data.get('tax_rate'),
                            Decimal('0')
                        )
                        fallback_tax_type = _normalize_tax_type(
                            item_data.get('taxType') or item_data.get('tax_type') or 'standard'
                        )

                        if eis_enabled:
                            fallback_tax_rate = Decimal('0')
                            fallback_tax_type = 'standard'
                            fallback_method = 'inclusive'
                        elif not block_sales_if_tax_mapping_missing and fallback_tax_rate <= 0 and fallback_tax_type == 'standard' and default_tax_rate:
                            fallback_tax_rate = _to_decimal(default_tax_rate.rate, Decimal('0'))
                            fallback_tax_type = _normalize_tax_type(default_tax_rate.tax_type)

                        line_base_amount = _resolve_line_base_amount(item_data, fallback_method)
                        discount_amount = _resolve_line_discount_amount(item_data, line_base_amount)
                        discounted_line_base_amount = max(Decimal('0'), line_base_amount - discount_amount)
                        print(
                            f"[Sync Sessions] No MRA mapping found for {item_id}, "
                            f"using fallback tax: type={fallback_tax_type}, rate={fallback_tax_rate}%, method={fallback_method}, "
                            f"base={line_base_amount}, discount={discount_amount}"
                        )
                        fallback_values = _calculate_item_tax_values(
                            line_base_amount=discounted_line_base_amount,
                            tax_type=fallback_tax_type,
                            tax_rate=fallback_tax_rate,
                            tax_calculation_method=fallback_method,
                        )
                        total_vat += fallback_values['tax_amount']
                        total_subtotal += fallback_values['subtotal']
                        total_gross += fallback_values['total']
                        
                except Exception as e:
                    print(f"[Sync Sessions] Error processing MRA mapping for {item_id}: {str(e)}")
                    # Fallback to item totals/tax payloads if available.
                    fallback_method = str(
                        item_data.get('taxCalculationMethod')
                        or item_data.get('tax_calculation_method')
                        or 'inclusive'
                    ).strip().lower()
                    fallback_tax_rate = _to_decimal(
                        item_data.get('taxRate') or item_data.get('tax_rate'),
                        Decimal('0')
                    )
                    fallback_tax_type = _normalize_tax_type(
                        item_data.get('taxType') or item_data.get('tax_type') or 'standard'
                    )
                    if eis_enabled:
                        fallback_tax_rate = Decimal('0')
                        fallback_tax_type = 'standard'
                        fallback_method = 'inclusive'
                    elif not block_sales_if_tax_mapping_missing and fallback_tax_rate <= 0 and fallback_tax_type == 'standard' and default_tax_rate:
                        fallback_tax_rate = _to_decimal(default_tax_rate.rate, Decimal('0'))
                        fallback_tax_type = _normalize_tax_type(default_tax_rate.tax_type)

                    fallback_line_amount = _resolve_line_base_amount(item_data, fallback_method)
                    fallback_discount_amount = _resolve_line_discount_amount(item_data, fallback_line_amount)
                    discounted_fallback_line_amount = max(Decimal('0'), fallback_line_amount - fallback_discount_amount)
                    fallback_values = _calculate_item_tax_values(
                        line_base_amount=discounted_fallback_line_amount,
                        tax_type=fallback_tax_type,
                        tax_rate=fallback_tax_rate,
                        tax_calculation_method=fallback_method,
                    )
                    total_vat += fallback_values['tax_amount']
                    total_subtotal += fallback_values['subtotal']
                    total_gross += fallback_values['total']
            
            # Update order totals based on product-specific calculations
            subtotal = float(_quantize_money(total_subtotal))
            total = float(_quantize_money(total_gross))
            vat_amount = float(_quantize_money(total_vat))
            
            print(f"[Sync Sessions] Final calculated totals - subtotal: {subtotal}, vat: {vat_amount}, total: {total}")
        else:
            print(f"[Sync Sessions] No items in order, using provided totals")
        
        order_data = {
            'id': order_id,
            'business': business,
            'branch': branch,
            'order_number': order_number,
            'order_type': data.get('orderType', 'sale'),
            'status': data.get('status', 'New'),
            'payment_method': payment_method,
            'pump_name': data.get('pump_name') or data.get('pumpName'),
            'customer_name': data.get('customer_name') or data.get('customerName'),
            'customer_phone': data.get('customer_phone') or data.get('customerPhone'),
            'customer_tin': data.get('customer_tin') or data.get('customerTin'),
            'customer_email': data.get('customer_email') or data.get('customerEmail'),
            'customer_address': data.get('customer_address') or data.get('customerAddress'),
            'customer_notes': data.get('customer_notes') or data.get('customerNotes'),
            'buyer_name': data.get('buyer_name') or data.get('buyerName'),
            'buyer_tin': data.get('buyer_tin') or data.get('buyerTin'),
            'buyer_authorization_code': data.get('buyer_authorization_code') or data.get('buyerAuthorizationCode'),
            'is_export': _parse_bool(data.get('is_export', data.get('isExport')), False),
            'is_relief_supply': _parse_bool(data.get('is_relief_supply', data.get('isReliefSupply')), False),
            'vat5_project_number': data.get('vat5_project_number') or data.get('vat5ProjectNumber'),
            'vat5_certificate_number': data.get('vat5_certificate_number') or data.get('vat5CertificateNumber'),
            'vat5_quantity': _to_decimal(
                data.get('vat5_quantity', data.get('vat5Quantity')),
                Decimal('0')
            ) if data.get('vat5_quantity', data.get('vat5Quantity')) not in (None, '') else None,
            'subtotal': _quantize_money(subtotal),
            'discount_amount': _quantize_money(
                sum(
                    (
                        _resolve_line_discount_amount(
                            item,
                            _resolve_line_base_amount(
                                item,
                                item.get('taxCalculationMethod') or item.get('tax_calculation_method') or 'inclusive',
                            ),
                        )
                        for item in (data.get('items') or [])
                    ),
                    Decimal('0'),
                )
            ),
            'discount_metadata': {
                'source': 'pos_admin_discount_rules',
                'items': [
                    {
                        'inventoryItemId': item.get('inventoryItemId') or item.get('inventory_item_id') or item.get('id'),
                        'discountRuleId': item.get('discountRuleId') or item.get('discount_rule_id'),
                        'discountName': item.get('discountName') or item.get('discount_name'),
                        'discountType': item.get('discountType') or item.get('discount_type'),
                        'discountValue': item.get('discountValue') or item.get('discount_value'),
                        'discountAmount': str(
                            _resolve_line_discount_amount(
                                item,
                                _resolve_line_base_amount(
                                    item,
                                    item.get('taxCalculationMethod') or item.get('tax_calculation_method') or 'inclusive',
                                ),
                            )
                        ),
                    }
                    for item in (data.get('items') or [])
                    if _resolve_line_discount_amount(
                        item,
                        _resolve_line_base_amount(
                            item,
                            item.get('taxCalculationMethod') or item.get('tax_calculation_method') or 'inclusive',
                        ),
                    ) > 0
                ],
            },
            'total': _quantize_money(total),
            'cogs': _quantize_money(_to_decimal(data.get('cogs'), Decimal('0'))),
            'created_at': data.get('createdAt'),
            'updated_at': data.get('updatedAt'),
        }
        
        # Handle tax fields if provided
        if data.get('tax_rate_name'):
            order_data['tax_rate_name'] = data.get('tax_rate_name')
        if data.get('tax_rate_value'):
            order_data['tax_rate_value'] = float(data.get('tax_rate_value'))
        if data.get('tax_type'):
            order_data['tax_type'] = data.get('tax_type')
        
        # Set calculated VAT amount - CRITICAL: Use sum of item taxes, not calculated from subtotal
        # This ensures accurate tax when items have different tax rates (mixed tax scenario)
        order_data['vat_amount'] = _quantize_money(vat_amount)
        order_data['net_amount'] = _quantize_money(subtotal)
        order_data['gross_amount'] = _quantize_money(total)
        
        print(f"[Sync Sessions] Order totals - subtotal: {subtotal}, vat_amount: {vat_amount}, total: {total}")
        
        # Enforce current-user active session (only one active session per user).
        session_id = data.get('sessionId') or data.get('session_id') or data.get('session')
        print(f"[Sync Sessions] Order data keys: {data.keys()}")
        print(f"[Sync Sessions] Looking for session_id - sessionId: {data.get('sessionId')}, session_id: {data.get('session_id')}, session: {data.get('session')}")
        print(f"[Sync Sessions] Final session_id: {session_id}")

        active_session = Session.objects.filter(
            user=user,
            business=business,
            branch=branch,
            status='active'
        ).order_by('-started_at').first()

        if not active_session:
            return {
                'success': False,
                'error': 'No active session found for current user in this branch. Please start a session.'
            }

        if session_id and str(session_id) != str(active_session.id):
            print(
                f"[Sync Sessions] Overriding session {session_id} with current user active session {active_session.id}"
            )

        order_data['session'] = active_session

        if eis_enabled and order_data.get('is_relief_supply'):
            project_number = str(order_data.get('vat5_project_number') or '').strip()
            certificate_number = str(order_data.get('vat5_certificate_number') or '').strip()
            quantity_value = order_data.get('vat5_quantity') or Decimal('0')
            if not project_number or not certificate_number or quantity_value <= 0:
                return {
                    'success': False,
                    'error': 'VAT5 details required.',
                    'mra_submission': {
                        'state': 'failed_before_order_create',
                        'message': 'VAT5 details required.',
                        'retryable': False,
                    },
                }
            if getattr(settings, 'MRA_EIS_VALIDATE_VAT5_BEFORE_SALE', True):
                try:
                    from mra_eis.services import EISSaleComplianceService, MRAIntegrationError
                    from mra_eis.services.core import _is_mra_network_failure

                    vat5_validation = EISSaleComplianceService.validate_vat5_certificate(
                        business=business,
                        project_number=project_number,
                        certificate_number=certificate_number,
                        quantity=quantity_value,
                    )
                    if vat5_validation.get('checked') and not vat5_validation.get('is_valid'):
                        return {
                            'success': False,
                            'error': 'Invalid VAT5 certificate.',
                            'mra_submission': {
                                'state': 'failed_before_order_create',
                                'message': 'Invalid VAT5 certificate.',
                                'retryable': False,
                            },
                        }
                except Exception as vat5_exc:
                    try:
                        is_network_failure = _is_mra_network_failure(vat5_exc)
                    except Exception:
                        is_network_failure = False
                    if is_network_failure:
                        message = 'Relief sale needs MRA online.'
                    elif isinstance(vat5_exc, MRAIntegrationError):
                        message = str(vat5_exc)
                    else:
                        message = 'VAT5 validation failed.'
                    return {
                        'success': False,
                        'error': message,
                        'mra_submission': {
                            'state': 'failed_before_order_create',
                            'message': message,
                            'retryable': False,
                        },
                    }
        
        # Remove None values
        order_data = {k: v for k, v in order_data.items() if v is not None}
        print(f"[Sync Sessions] Final order_data: {order_data}")
        
        create_attempts = 0
        order = None
        while create_attempts < 3:
            try:
                order = Order.objects.create(**order_data)
                break
            except IntegrityError as ie:
                if 'order_number' not in str(ie):
                    raise

                existing_by_number = Order.objects.filter(
                    branch=branch,
                    order_number=order_number
                ).first()
                if existing_by_number and str(existing_by_number.id) == str(order_id):
                    print(
                        f"[Sync Sessions] Integrity retry resolved for existing order {order_id} "
                        f"with order_number {order_number}"
                    )
                    return {
                        'success': True,
                        **_build_order_sync_payload(existing_by_number)
                    }

                previous_order_number = order_number
                order_number = _next_order_number_for_branch(branch)
                order_data['order_number'] = order_number
                create_attempts += 1
                print(
                    f"[Sync Sessions] UNIQUE order_number conflict (attempt {create_attempts}) "
                    f"for order {order_id}: {previous_order_number} -> {order_number}"
                )

        if order is None:
            return {
                'success': False,
                'error': 'Failed to allocate a unique order number for this sale.'
            }
        
        # Create order items if provided
        if data.get('items'):
            for item_data in data['items']:
                # CRITICAL: Use inventoryItemId (the actual product ID), NOT id (which is the order item ID)
                inventory_ref = item_data.get('inventoryItemId') or item_data.get('inventory_item_id') or item_data.get('id', '')
                inventory_item_id = str(inventory_ref or '').strip()
                item_name = str(item_data.get('name', '') or '').strip()

                # Normalize legacy payloads where `id` may be an order-item UUID.
                if inventory_item_id:
                    inventory_match = InventoryItem.objects.filter(
                        id=inventory_item_id,
                        business=business,
                        branch=branch,
                    ).first()
                else:
                    inventory_match = None

                if not inventory_match and item_name:
                    name_matches = list(
                        InventoryItem.objects.filter(
                            name__iexact=item_name,
                            business=business,
                            branch=branch,
                        )[:2]
                    )
                    if len(name_matches) == 1:
                        inventory_match = name_matches[0]
                        inventory_item_id = str(inventory_match.id)
                        print(
                            f"[Sync Sessions] Normalized order item reference by name: "
                            f"{item_name} -> {inventory_item_id}"
                        )

                print(f"[Sync Sessions] Creating OrderItem - inventoryItemId: {inventory_item_id}, name: {item_name}")
                
                # Extract per-item tax information (MRA compliance - Immutable snapshot)
                tax_rate = _to_decimal(item_data.get('taxRate') or item_data.get('tax_rate', 0), Decimal('0'))
                tax_type = _normalize_tax_type(item_data.get('taxType') or item_data.get('tax_type', 'standard'))
                tax_calculation_method = str(
                    item_data.get('taxCalculationMethod') or item_data.get('tax_calculation_method', 'inclusive')
                ).strip().lower()
                
                # Extract calculated tax amounts
                item_price = _to_decimal(item_data.get('price'), Decimal('0'))
                item_quantity = _to_decimal(item_data.get('quantity'), Decimal('0'))

                line_base_amount = _resolve_line_base_amount(item_data, tax_calculation_method)
                item_discount_amount = _resolve_line_discount_amount(item_data, line_base_amount)
                discounted_line_base_amount = max(Decimal('0'), line_base_amount - item_discount_amount)

                mapping_for_item = MRAProductMapping.objects.filter(
                    inventory_item_id=inventory_item_id,
                    is_approved=True,
                    mra_synced=True,
                ).first()
                if mapping_for_item:
                    tax_rate = _to_decimal(mapping_for_item.mra_tax_rate, Decimal('0'))
                    tax_type = _normalize_tax_type(mapping_for_item.mra_tax_type)
                    tax_calculation_method = str(mapping_for_item.tax_calculation_method or 'inclusive').strip().lower()
                elif eis_enabled:
                    tax_rate = Decimal('0')
                    tax_type = 'standard'
                    tax_calculation_method = 'inclusive'
                elif not block_sales_if_tax_mapping_missing and default_tax_rate:
                    if tax_type == 'standard' and tax_rate <= 0:
                        tax_rate = _to_decimal(default_tax_rate.rate, Decimal('0'))
                        tax_type = _normalize_tax_type(default_tax_rate.tax_type)

                tax_values = _calculate_item_tax_values(
                    line_base_amount=discounted_line_base_amount,
                    tax_type=tax_type,
                    tax_rate=tax_rate,
                    tax_calculation_method=tax_calculation_method,
                )

                normalized_unit_price = item_price
                if item_quantity > 0:
                    recalculated_line = item_price * item_quantity
                    if abs(recalculated_line - line_base_amount) > Decimal('0.01'):
                        # Legacy payloads can send line total in `price`.
                        normalized_unit_price = (line_base_amount / item_quantity).quantize(Decimal('0.01'))
                elif line_base_amount > 0:
                    # Preserve stored totals even if quantity was omitted/invalid.
                    item_quantity = Decimal('1')
                    normalized_unit_price = line_base_amount.quantize(Decimal('0.01'))

                item_subtotal = tax_values['subtotal']
                item_tax_amount = tax_values['tax_amount']
                item_total = tax_values['total']
                tax_calculation_method = tax_values['tax_method']
                tax_type = tax_values['tax_type']
                tax_rate = tax_values['rate_percent']
                
                print(
                    f"[Sync Sessions] OrderItem tax info - rate: {tax_rate}%, type: {tax_type}, "
                    f"method: {tax_calculation_method}, subtotal: {item_subtotal}, "
                    f"tax: {item_tax_amount}, total: {item_total}, "
                    f"unit_price: {normalized_unit_price}"
                )
                
                OrderItem.objects.create(
                    order=order,
                    inventory_item_id=inventory_item_id,
                    name=item_name,
                    quantity=item_quantity.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP),
                    price=normalized_unit_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    notes=item_data.get('notes', ''),
                    discount_rule_id=item_data.get('discountRuleId') or item_data.get('discount_rule_id') or None,
                    discount_name=str(item_data.get('discountName') or item_data.get('discount_name') or '')[:120],
                    discount_type=str(item_data.get('discountType') or item_data.get('discount_type') or '')[:20],
                    discount_value=_to_decimal(
                        item_data.get('discountValue')
                        if item_data.get('discountValue') is not None
                        else item_data.get('discount_value'),
                        Decimal('0'),
                    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    discount_amount=item_discount_amount,
                    # Per-item tax information (MRA compliance - Immutable snapshot)
                    tax_rate=tax_rate.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    tax_type=tax_type,
                    tax_calculation_method=tax_calculation_method,
                    # Calculated tax amounts (Immutable snapshot for audit trail)
                    subtotal=item_subtotal,
                    tax_amount=item_tax_amount,
                    total=item_total
                )

        # Lock the matched tax rate after first use (MRA immutability requirement).
        # We match by snapshot values saved on the order.
        try:
            tax_rate_name = str(order.tax_rate_name or '').strip()
            tax_rate_value = order.tax_rate_value
            if tax_rate_name or tax_rate_value:
                normalized_tax_rate_value = None
                if tax_rate_value:
                    try:
                        normalized_tax_rate_value = Decimal(str(tax_rate_value))
                    except (InvalidOperation, TypeError, ValueError):
                        normalized_tax_rate_value = None

                tax_rate_query = TaxRate.objects.filter(
                    business=business,
                    is_active=True,
                )
                if tax_rate_name:
                    tax_rate_query = tax_rate_query.filter(name=tax_rate_name)
                if normalized_tax_rate_value is not None:
                    tax_rate_query = tax_rate_query.filter(rate=normalized_tax_rate_value)
                matched_tax_rate = tax_rate_query.order_by('-is_default', '-effective_from').first()
                lock_tax_rate_on_use(matched_tax_rate)
        except Exception as lock_exc:
            print(f"[Sync Sessions] Warning: tax lock update failed for order {order_id}: {lock_exc}")

        try:
            decrement_inventory_for_order(order, branch, business, validate_only=True)
        except InsufficientStockError as stock_exc:
            print(f"[Sync Sessions] ✗ BLOCKED order {order_id}: {stock_exc}")
            order.delete()
            return {
                'success': False,
                'error': str(stock_exc),
                'reason': 'insufficient_stock',
            }
        except Exception as stock_validation_exc:
            print(f"[Sync Sessions] ✗ BLOCKED order {order_id}: stock validation failed: {stock_validation_exc}")
            order.delete()
            return {
                'success': False,
                'error': f'Stock validation failed. Sale was not completed. Details: {stock_validation_exc}',
            }

        # Prepare order for MRA EIS pipeline.
        # In live mode with block_sales_if_eis_down enabled, this becomes blocking.
        business_settings = None
        try:
            try:
                business_settings = order.business.settings
            except Exception:
                business_settings = None
            if eis_enabled:
                from mra_eis.services import POSOrderSubmissionService

                mra_result = POSOrderSubmissionService.prepare_pos_order_submission(
                    order,
                    request_device_serial=request_device_serial,
                    enforce_device_binding=True,
                )
                mra_response = mra_result.get('response') if isinstance(mra_result, dict) else {}
                mra_reason = (
                    mra_response.get('reason')
                    if isinstance(mra_response, dict)
                    else ''
                ) or '; '.join(mra_result.get('errors') or [])
                print(
                    f"[Sync Sessions] Prepared MRA payload for order {order.id}: "
                    f"fiscal={mra_result.get('fiscal_invoice_number')} "
                    f"endpoint={mra_result.get('endpoint')} "
                    f"dry_run={mra_result.get('dry_run')} reason={mra_reason or 'none'}"
                )
                if _should_block_sales_if_eis_down(business_settings):
                    block_reason = _get_mra_submission_block_reason(mra_result)
                    if block_reason:
                        print(
                            f"[Sync Sessions] ✗ BLOCKED order {order_id}: "
                            f"{block_reason} block_sales_if_eis_down is enabled."
                        )
                        order.delete()
                        return {
                            'success': False,
                            'error': (
                                f"{block_reason} "
                                "Sale blocked by compliance policy (block_sales_if_eis_down)."
                            ),
                            'mra_submission': {
                                'state': mra_result.get('submission_state') or 'unknown',
                                'message': mra_result.get('submission_message') or block_reason,
                                'retryable': bool(mra_result.get('retryable')),
                                'queued_offline': bool(mra_result.get('queued_offline')),
                                'endpoint': mra_result.get('endpoint'),
                                'dry_run': bool(mra_result.get('dry_run')),
                                'errors': mra_result.get('errors') or [],
                                'fiscal_invoice_number': mra_result.get('fiscal_invoice_number') or order.fiscal_invoice_number or '',
                                'eis_status': mra_result.get('eis_status') or order.eis_status or '',
                                'offline_validation_url': mra_result.get('offline_validation_url') or '',
                                'offline_signature': mra_result.get('offline_signature') or '',
                            },
                        }
        except Exception as mra_exc:
            print(f"[Sync Sessions] Warning: MRA preparation failed for order {order.id}: {str(mra_exc)}")
            try:
                from mra_eis.services import MRAIntegrationError
            except Exception:
                MRAIntegrationError = Exception

            if isinstance(mra_exc, MRAIntegrationError):
                order.delete()
                return {
                    'success': False,
                    'error': str(mra_exc),
                    'mra_submission': {
                        'state': 'failed_before_submission',
                        'message': str(mra_exc),
                        'retryable': False,
                    },
                }

            if eis_enabled:
                print(
                    f"[Sync Sessions] ✗ BLOCKED order {order_id}: "
                    "MRA sale preparation failed before a valid submission result was produced."
                )
                order.delete()
                return {
                    'success': False,
                    'error': f'MRA EIS sale preparation failed. Sale was not completed. Details: {str(mra_exc)}',
                    'mra_submission': {
                        'state': 'failed_before_submission',
                        'message': str(mra_exc),
                        'retryable': False,
                    },
                }

        # Decrement inventory stock for all items in the order using FIFO
        print(f"[Sync Sessions] Decrementing inventory stock for order {order_id}")
        try:
            decrement_inventory_for_order(order, branch, business)
        except InsufficientStockError as stock_exc:
            print(f"[Sync Sessions] ✗ BLOCKED order {order_id}: {stock_exc}")
            order.delete()
            return {
                'success': False,
                'error': str(stock_exc),
                'reason': 'insufficient_stock',
            }
        except Exception as e:
            print(f"[Sync Sessions] ✗ BLOCKED order {order_id}: failed to decrement inventory: {str(e)}")
            order.delete()
            return {
                'success': False,
                'error': f'Stock update failed. Sale was not completed. Details: {str(e)}',
            }
        
        print(f"[Sync Sessions] Created order {order_id}")
        
        return {
            'success': True,
            **_build_order_sync_payload(order)
        }
        
    except IntegrityError as ie:
        print(f"[Sync Sessions] IntegrityError creating order: {str(ie)}")
        return {
            'success': False,
            'error': f'Integrity error: {str(ie)}'
        }
    except Exception as e:
        print(f"[Sync Sessions] Error creating order: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


def handle_update_order(order_id, data, business, branch_id, user, *, request_device_serial=''):
    """Handle update of order from frontend"""
    try:
        order = Order.objects.get(id=order_id, business=business, branch_id=branch_id)
        
        # Update fields
        if 'status' in data:
            order.status = data['status']
        if 'paymentMethod' in data:
            order.payment_method = data['paymentMethod']
        if 'pumpName' in data or 'pump_name' in data:
            order.pump_name = data.get('pump_name') or data.get('pumpName')
        if 'subtotal' in data:
            order.subtotal = data['subtotal']
        if 'total' in data:
            order.total = data['total']
        if 'cogs' in data:
            order.cogs = data['cogs']
        if 'customerName' in data or 'customer_name' in data:
            order.customer_name = data.get('customer_name') or data.get('customerName')
        if 'customerPhone' in data or 'customer_phone' in data:
            order.customer_phone = data.get('customer_phone') or data.get('customerPhone')
        if 'customerTin' in data or 'customer_tin' in data:
            order.customer_tin = data.get('customer_tin') or data.get('customerTin')
        if 'customerEmail' in data or 'customer_email' in data:
            order.customer_email = data.get('customer_email') or data.get('customerEmail')
        if 'customerAddress' in data or 'customer_address' in data:
            order.customer_address = data.get('customer_address') or data.get('customerAddress')
        if 'customerNotes' in data or 'customer_notes' in data:
            order.customer_notes = data.get('customer_notes') or data.get('customerNotes')
        if 'buyerName' in data or 'buyer_name' in data:
            order.buyer_name = data.get('buyer_name') or data.get('buyerName')
        if 'buyerTin' in data or 'buyer_tin' in data:
            order.buyer_tin = data.get('buyer_tin') or data.get('buyerTin')
        if 'buyerAuthorizationCode' in data or 'buyer_authorization_code' in data:
            order.buyer_authorization_code = data.get('buyer_authorization_code') or data.get('buyerAuthorizationCode')
        if 'isExport' in data or 'is_export' in data:
            order.is_export = _parse_bool(data.get('is_export', data.get('isExport')), False)
        if 'isReliefSupply' in data or 'is_relief_supply' in data:
            order.is_relief_supply = _parse_bool(data.get('is_relief_supply', data.get('isReliefSupply')), False)
        if 'vat5ProjectNumber' in data or 'vat5_project_number' in data:
            order.vat5_project_number = data.get('vat5_project_number') or data.get('vat5ProjectNumber')
        if 'vat5CertificateNumber' in data or 'vat5_certificate_number' in data:
            order.vat5_certificate_number = data.get('vat5_certificate_number') or data.get('vat5CertificateNumber')
        if 'vat5Quantity' in data or 'vat5_quantity' in data:
            vat5_quantity = data.get('vat5_quantity', data.get('vat5Quantity'))
            order.vat5_quantity = _to_decimal(vat5_quantity, Decimal('0')) if vat5_quantity not in (None, '') else None
        
        # Handle tax fields if provided
        if 'tax_rate_name' in data:
            order.tax_rate_name = data['tax_rate_name']
        if 'tax_rate_value' in data:
            order.tax_rate_value = float(data['tax_rate_value'])
        if 'tax_type' in data:
            order.tax_type = data['tax_type']
        if 'vat_amount' in data:
            order.vat_amount = float(data['vat_amount'])
        if 'net_amount' in data:
            order.net_amount = float(data['net_amount'])
        if 'gross_amount' in data:
            order.gross_amount = float(data['gross_amount'])
        
        order.save()

        # Keep lock behavior consistent for orders updated via sync.
        try:
            tax_rate_name = str(order.tax_rate_name or '').strip()
            tax_rate_value = order.tax_rate_value
            if tax_rate_name or tax_rate_value:
                normalized_tax_rate_value = None
                if tax_rate_value:
                    try:
                        normalized_tax_rate_value = Decimal(str(tax_rate_value))
                    except (InvalidOperation, TypeError, ValueError):
                        normalized_tax_rate_value = None

                tax_rate_query = TaxRate.objects.filter(
                    business=business,
                    is_active=True,
                )
                if tax_rate_name:
                    tax_rate_query = tax_rate_query.filter(name=tax_rate_name)
                if normalized_tax_rate_value is not None:
                    tax_rate_query = tax_rate_query.filter(rate=normalized_tax_rate_value)
                matched_tax_rate = tax_rate_query.order_by('-is_default', '-effective_from').first()
                lock_tax_rate_on_use(matched_tax_rate)
        except Exception as lock_exc:
            print(f"[Sync Sessions] Warning: tax lock update failed for order {order_id}: {lock_exc}")

        print(f"[Sync Sessions] Updated order {order_id}")
        
        return {
            'success': True,
            **_build_order_sync_payload(order)
        }
        
    except Order.DoesNotExist:
        print(f"[Sync Sessions] Order {order_id} not found, creating instead")
        return handle_create_order(
            order_id,
            data,
            business,
            branch_id,
            user,
            request_device_serial=request_device_serial,
        )
    except Exception as e:
        print(f"[Sync Sessions] Error updating order: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def handle_delete_order(order_id, business, branch_id):
    """Handle deletion of order from frontend"""
    try:
        order = Order.objects.get(id=order_id, business=business, branch_id=branch_id)
        order.delete()
        print(f"[Sync Sessions] Deleted order {order_id}")
        
        return {
            'success': True,
            'server_id': order_id
        }
        
    except Order.DoesNotExist:
        print(f"[Sync Sessions] Order {order_id} not found for deletion")
        return {
            'success': True,
            'server_id': order_id
        }
    except Exception as e:
        print(f"[Sync Sessions] Error deleting order: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def decrement_inventory_for_order(order, branch, business, *, validate_only=False):
    """
    Decrement inventory stock for all items in an order using FIFO method.
    This ensures backend stock is synchronized with frontend stock decrements.
    
    FIFO Process:
    1. Get all purchase history batches for the item ordered by received_date
    2. Decrement quantity_remaining from oldest batches first
    3. Update main inventory item stock
    4. Track which batches were used for audit trail
    
    CRITICAL FIX: Always update inventory stock, even when it reaches exactly 0
    """
    from collections import defaultdict
    from decimal import Decimal, InvalidOperation
    from django.db.models import Sum
    from inventory.models import InventoryItem, PurchaseOrderItem

    def parse_positive_decimal(value, label):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            print(f"[Sync Sessions] Warning: Invalid decimal for {label}: {value}")
            return Decimal('0')
        if parsed.is_nan() or parsed.is_infinite():
            print(f"[Sync Sessions] Warning: Non-finite decimal for {label}: {value}")
            return Decimal('0')
        return parsed if parsed > 0 else Decimal('0')

    def parse_non_negative_decimal(value, label):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            print(f"[Sync Sessions] Warning: Invalid decimal for {label}: {value}")
            return Decimal('0')
        if parsed.is_nan() or parsed.is_infinite():
            print(f"[Sync Sessions] Warning: Non-finite decimal for {label}: {value}")
            return Decimal('0')
        return parsed if parsed >= 0 else Decimal('0')

    def decimal_to_quantity_string(value):
        normalized = parse_non_negative_decimal(value, "trace.quantity")
        return format(normalized.quantize(Decimal('0.001')), 'f')

    def resolve_inventory_item(reference, item_name=''):
        """Resolve inventory item robustly from id/sku/barcode/name."""
        ref = str(reference or '').strip()
        if ref:
            by_id = InventoryItem.objects.filter(
                id=ref,
                business=business,
                branch=branch,
            ).first()
            if by_id:
                return by_id

            by_sku = InventoryItem.objects.filter(
                sku__iexact=ref,
                business=business,
                branch=branch,
            ).first()
            if by_sku:
                return by_sku

            by_barcode = InventoryItem.objects.filter(
                barcode__iexact=ref,
                business=business,
                branch=branch,
            ).first()
            if by_barcode:
                return by_barcode

        normalized_name = str(item_name or '').strip()
        if normalized_name:
            name_matches = list(
                InventoryItem.objects.filter(
                    name__iexact=normalized_name,
                    business=business,
                    branch=branch,
                )[:2]
            )
            if len(name_matches) == 1:
                return name_matches[0]

        return None

    def normalize_trace_entries(trace_entries):
        """Collapse duplicate trace rows per (inventory item, batch, unassigned flag)."""
        aggregated = defaultdict(lambda: Decimal('0'))
        for entry in trace_entries:
            if not isinstance(entry, dict):
                continue
            inventory_item_id = str(entry.get('inventory_item_id') or '').strip()
            raw_batch_id = entry.get('batch_id')
            batch_id = str(raw_batch_id).strip() if raw_batch_id else ''
            is_unassigned = bool(entry.get('unassigned'))
            quantity = parse_positive_decimal(
                entry.get('quantity'),
                f"trace.entry.quantity:{inventory_item_id}:{batch_id or 'none'}"
            )
            if not inventory_item_id or quantity <= 0:
                continue
            key = (inventory_item_id, batch_id, is_unassigned)
            aggregated[key] += quantity

        normalized = []
        for (inventory_item_id, batch_id, is_unassigned), quantity in aggregated.items():
            row = {
                'inventory_item_id': inventory_item_id,
                'batch_id': batch_id or None,
                'quantity': decimal_to_quantity_string(quantity),
            }
            if is_unassigned:
                row['unassigned'] = True
            normalized.append(row)
        return normalized

    def register_target(target_reference, target_name, quantity, order_item_id, context='direct'):
        if quantity <= 0:
            return
        target_ref = str(target_reference or '').strip()
        if not target_ref:
            return
        resolved_target = resolve_inventory_item(target_ref, target_name)
        target_id = str(resolved_target.id) if resolved_target else target_ref

        decrement_targets[target_id] += quantity
        contributors_by_target[target_id].append({
            'order_item_id': order_item_id,
            'quantity': quantity,
            'context': context,
        })

    print(f"[Sync Sessions] Starting inventory decrement for order {order.id}")

    order_items = list(order.items.all().order_by('created_at', 'id'))
    decrement_targets = defaultdict(lambda: Decimal('0'))
    contributors_by_target = defaultdict(list)
    per_order_item_usage = defaultdict(list)

    for order_item in order_items:
        sold_item_id = str(order_item.inventory_item_id or '').strip()
        sold_quantity = parse_positive_decimal(order_item.quantity, f"order_item.quantity:{sold_item_id}")
        if sold_quantity <= 0:
            continue

        print(f"[Sync Sessions] Processing order line: {sold_item_id}, sold quantity: {sold_quantity}")

        sold_inventory_item = resolve_inventory_item(sold_item_id, order_item.name)
        if not sold_inventory_item:
            raise ValueError(f'Inventory item not found for sale line "{order_item.name or sold_item_id}".')

        resolved_sold_item_id = str(sold_inventory_item.id)
        if resolved_sold_item_id != sold_item_id:
            print(
                f"[Sync Sessions] Normalized sold item reference {sold_item_id} -> {resolved_sold_item_id} "
                f"({sold_inventory_item.name})"
            )
            sold_item_id = resolved_sold_item_id
            if str(order_item.inventory_item_id) != resolved_sold_item_id:
                order_item.inventory_item_id = resolved_sold_item_id
                order_item.save(update_fields=['inventory_item_id', 'updated_at'])

        if (
            sold_inventory_item.item_type == 'sellable'
            and sold_inventory_item.is_produced
            and sold_inventory_item.recipe
        ):
            print(f"[Sync Sessions] Item {sold_inventory_item.name} is produced; decrementing recipe ingredients")
            recipe_entries = sold_inventory_item.recipe if isinstance(sold_inventory_item.recipe, list) else []

            for recipe_item in recipe_entries:
                ingredient_id = (
                    recipe_item.get('ingredientId')
                    or recipe_item.get('ingredient_id')
                    or recipe_item.get('id')
                )
                ingredient_name = str(recipe_item.get('name') or '').strip()
                ingredient_per_unit = parse_positive_decimal(
                    recipe_item.get('quantity'),
                    f"recipe.quantity:{ingredient_id}"
                )
                if not ingredient_id or ingredient_per_unit <= 0:
                    continue

                decrement_amount = sold_quantity * ingredient_per_unit
                register_target(
                    target_reference=ingredient_id,
                    target_name=ingredient_name,
                    quantity=decrement_amount,
                    order_item_id=order_item.id,
                    context=f"recipe:{ingredient_id}",
                )
                print(
                    f"[Sync Sessions] Recipe target {ingredient_id}: +{decrement_amount} "
                    f"(sold {sold_quantity} x {ingredient_per_unit})"
                )
        else:
            register_target(
                target_reference=sold_item_id,
                target_name=order_item.name,
                quantity=sold_quantity,
                order_item_id=order_item.id,
                context='direct',
            )
            print(f"[Sync Sessions] Direct stock target {sold_item_id}: +{sold_quantity}")

    for target_item_id, quantity_to_decrement in decrement_targets.items():
        if quantity_to_decrement <= 0:
            continue

        inventory_item = resolve_inventory_item(target_item_id)
        if not inventory_item:
            raise ValueError(f'Inventory item not found for stock target {target_item_id}.')

        item_id = str(inventory_item.id)
        available_batch_total = (
            PurchaseOrderItem.objects.filter(
                inventory_item_id=item_id,
                purchase_order__branch=branch,
                quantity_remaining__gt=0,
            )
            .aggregate(total=Sum('quantity_remaining'))
            .get('total')
        )
        if available_batch_total is not None:
            available_quantity = parse_non_negative_decimal(
                available_batch_total,
                f"inventory.available_batch_total:{item_id}",
            )
        else:
            available_quantity = parse_non_negative_decimal(
                inventory_item.stock_units,
                f"inventory.stock_units:{item_id}",
            )

        if available_quantity < quantity_to_decrement:
            raise InsufficientStockError(
                inventory_item.name,
                quantity_to_decrement,
                available_quantity,
            )

    if validate_only:
        print(f"[Sync Sessions] Stock validation passed for order {order.id}")
        return

    for target_item_id, quantity_to_decrement in decrement_targets.items():
        if quantity_to_decrement <= 0:
            continue

        try:
            inventory_item = resolve_inventory_item(target_item_id)
            if not inventory_item:
                raise InventoryItem.DoesNotExist()

            item_id = str(inventory_item.id)
            if item_id != str(target_item_id):
                print(
                    f"[Sync Sessions] Normalized decrement target {target_item_id} -> {item_id} "
                    f"({inventory_item.name})"
                )

            print(
                f"[Sync Sessions] Applying decrement for {inventory_item.name} ({item_id}): "
                f"{quantity_to_decrement}, current stock: {inventory_item.stock_units}"
            )

            remaining_to_decrement = quantity_to_decrement
            batches_used = []

            batches_qs = PurchaseOrderItem.objects.filter(
                inventory_item_id=item_id,
                purchase_order__branch=branch,
                quantity_remaining__gt=0
            ).select_related('purchase_order')

            batches = list(batches_qs)
            batches.sort(
                key=lambda batch: (
                    batch.expiry_date is None,
                    batch.expiry_date or datetime.max.date(),
                    batch.purchase_order.received_date or batch.created_at,
                    batch.created_at,
                )
            )

            print(f"[Sync Sessions] Found {len(batches)} available batches for FIFO decrement")

            for batch in batches:
                if remaining_to_decrement <= 0:
                    break

                batch_remaining = parse_non_negative_decimal(
                    batch.quantity_remaining,
                    f"batch.quantity_remaining:{batch.id}"
                )
                decrement_amount = min(remaining_to_decrement, batch_remaining)
                if decrement_amount <= 0:
                    continue

                old_remaining = batch.quantity_remaining
                batch.quantity_remaining = batch_remaining - decrement_amount
                batch.save()

                batches_used.append({
                    'batch_id': str(batch.id),
                    'received_date': batch.purchase_order.received_date.isoformat() if batch.purchase_order.received_date else None,
                    'decremented': decrement_amount,
                    'remaining_before': parse_non_negative_decimal(old_remaining, f"batch.old_remaining:{batch.id}"),
                    'remaining_after': parse_non_negative_decimal(batch.quantity_remaining, f"batch.new_remaining:{batch.id}"),
                })

                print(
                    f"[Sync Sessions] FIFO Batch {batch.id}: decremented {decrement_amount}, "
                    f"remaining {old_remaining} -> {batch.quantity_remaining}"
                )

                remaining_to_decrement -= decrement_amount

            contributors = contributors_by_target.get(target_item_id, [])
            if not contributors and item_id != target_item_id:
                contributors = contributors_by_target.get(item_id, [])

            if contributors:
                allocation_batches = [
                    {
                        'batch_id': batch_info['batch_id'],
                        'remaining': parse_positive_decimal(
                            batch_info.get('decremented'),
                            f"allocation.remaining:{batch_info.get('batch_id')}"
                        ),
                    }
                    for batch_info in batches_used
                ]
                batch_cursor = 0

                for contributor in contributors:
                    contributor_remaining = parse_positive_decimal(
                        contributor.get('quantity'),
                        f"contributor.quantity:{contributor.get('order_item_id')}"
                    )
                    order_item_id = contributor.get('order_item_id')
                    if not order_item_id or contributor_remaining <= 0:
                        continue

                    while contributor_remaining > 0 and batch_cursor < len(allocation_batches):
                        allocation_batch = allocation_batches[batch_cursor]
                        allocatable = allocation_batch.get('remaining', Decimal('0'))
                        if allocatable <= 0:
                            batch_cursor += 1
                            continue

                        allocation_amount = min(contributor_remaining, allocatable)
                        if allocation_amount <= 0:
                            break

                        per_order_item_usage[order_item_id].append({
                            'inventory_item_id': item_id,
                            'batch_id': allocation_batch['batch_id'],
                            'quantity': decimal_to_quantity_string(allocation_amount),
                        })

                        allocation_batch['remaining'] = allocatable - allocation_amount
                        contributor_remaining -= allocation_amount

                        if allocation_batch['remaining'] <= 0:
                            batch_cursor += 1

                    if contributor_remaining > 0:
                        # Decrement happened without an attributable batch (or insufficient batch trail).
                        # Persist as unassigned so void can fallback safely for legacy/no-batch scenarios.
                        per_order_item_usage[order_item_id].append({
                            'inventory_item_id': item_id,
                            'batch_id': None,
                            'quantity': decimal_to_quantity_string(contributor_remaining),
                            'unassigned': True,
                        })

            if batches and remaining_to_decrement > 0:
                raise InsufficientStockError(
                    inventory_item.name,
                    quantity_to_decrement,
                    quantity_to_decrement - remaining_to_decrement,
                )

            old_stock = parse_non_negative_decimal(inventory_item.stock_units, f"inventory.stock_units:{item_id}")
            if batches:
                batch_total = (
                    PurchaseOrderItem.objects.filter(
                        inventory_item_id=item_id,
                        purchase_order__branch=branch,
                    )
                    .aggregate(total=Sum('quantity_remaining'))
                    .get('total')
                )
                final_stock = parse_non_negative_decimal(batch_total, f"inventory.batch_total:{item_id}")
            else:
                new_stock = old_stock - quantity_to_decrement
                final_stock = max(Decimal('0'), new_stock)

            inventory_item.stock_units = final_stock
            inventory_item.value = final_stock * (inventory_item.cost or Decimal('0'))
            inventory_item.update_status()

            print(
                f"[Sync Sessions] Updated inventory item {item_id}: stock {old_stock} -> {final_stock} "
                f"(quantity_to_decrement: {quantity_to_decrement})"
            )
            print(f"[Sync Sessions] FIFO batches used: {len(batches_used)}")
            for batch_info in batches_used:
                print(
                    f"  - Batch {batch_info['batch_id']} (received: {batch_info['received_date']}): "
                    f"{batch_info['remaining_before']} -> {batch_info['remaining_after']} "
                    f"(decremented: {batch_info['decremented']})"
                )

        except InventoryItem.DoesNotExist:
            raise ValueError(f'Inventory item {target_item_id} not found in branch {branch.id}.')
        except Exception as e:
            print(f"[Sync Sessions] Error decrementing inventory for item {target_item_id}: {str(e)}")
            import traceback
            traceback.print_exc()
            raise

    for order_item in order_items:
        normalized_trace = normalize_trace_entries(per_order_item_usage.get(order_item.id, []))
        if order_item.batch_consumption != normalized_trace:
            order_item.batch_consumption = normalized_trace
            order_item.save(update_fields=['batch_consumption', 'updated_at'])

    print(f"[Sync Sessions] Completed inventory decrement for order {order.id}")
