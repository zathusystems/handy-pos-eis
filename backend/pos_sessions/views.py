from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from .models import Session, Order, OrderItem, DiscountRule
from .serializers import SessionSerializer, OrderSerializer, OrderItemSerializer, DiscountRuleSerializer
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
import re


def _get_accessible_business_ids(user):
    business_ids = set()
    user_businesses = getattr(user, 'businesses', None)
    if user_businesses is not None and hasattr(user_businesses, 'values_list'):
        business_ids.update(user_businesses.values_list('id', flat=True))
    try:
        from staff.models import Staff
        staff_business_ids = Staff.objects.filter(
            user=user,
            business_id__isnull=False,
        ).values_list('business_id', flat=True)
        business_ids.update(staff_business_ids)
    except Exception:
        pass
    return list(business_ids)

NON_BLOCKING_OFFLINE_DRY_RUN_REASONS = {
    'b2c_offline_first_enabled',
    'submission_call_failed',
    'connection_error',
    'timeout',
    'network_error',
    'eis_unreachable',
    'mra_server_error',
}


def _normalize_branch_reference(raw_reference):
    """
    Normalize branch references from client query params.
    Supports integer IDs, BRN-<id>, and string aliases.
    """
    if raw_reference is None:
        return None

    value = str(raw_reference).strip()
    if not value:
        return None

    lowered = value.lower()
    if lowered in {'nan', 'null', 'none', 'undefined'}:
        return None

    legacy_match = re.match(r'^BRN-(\d+)$', value, flags=re.IGNORECASE)
    if legacy_match:
        return int(legacy_match.group(1))

    if value.isdigit():
        return int(value)

    return value


def _apply_branch_filter(queryset, branch_reference, field_name='branch'):
    """
    Apply branch filter safely without raising ValueError on non-numeric values.
    Supports numeric IDs, BRN-<id>, "main", slug, and branch name.
    """
    lookup = _normalize_branch_reference(branch_reference)
    if lookup is None:
        return queryset

    if isinstance(lookup, int):
        return queryset.filter(**{f'{field_name}_id': lookup})

    normalized = lookup.lower()
    if normalized in {'main', 'main-branch', 'main_branch'}:
        return queryset.filter(**{f'{field_name}__name__iendswith': 'Main Branch'})

    return queryset.filter(
        Q(**{f'{field_name}__slug': lookup}) |
        Q(**{f'{field_name}__name__iexact': lookup})
    )


def _enforce_mra_submission_policy(order, business_settings, mra_result):
    """
    Enforce MRA live-mode blocking rule when EIS is unreachable.

    If block_sales_if_eis_down is enabled, a sale must not complete unless
    MRA submission is confirmed in live mode.
    """
    if not getattr(settings, 'MRA_EIS_IS_LIVE', False):
        return
    if not business_settings:
        return
    if not bool(getattr(business_settings, 'enable_eis', False)):
        return
    if not bool(getattr(business_settings, 'block_sales_if_eis_down', True)):
        return

    if not isinstance(mra_result, dict):
        raise ValidationError(
            {
                'error': 'MRA EIS submission did not return a valid response. Sale blocked by compliance policy.',
                'mra_submission': {
                    'state': 'invalid_response',
                    'message': 'MRA EIS submission did not return a valid response.',
                    'retryable': True,
                },
            }
        )

    submission_message = str(mra_result.get('submission_message') or '').strip()
    submission_state = str(mra_result.get('submission_state') or '').strip()
    submission_payload = {
        'state': submission_state or 'unknown',
        'message': submission_message or 'MRA EIS submission was not confirmed.',
        'retryable': bool(mra_result.get('retryable')),
        'queued_offline': bool(mra_result.get('queued_offline')),
        'endpoint': mra_result.get('endpoint'),
        'dry_run': bool(mra_result.get('dry_run')),
        'errors': mra_result.get('errors') or [],
    }
    endpoint_key = str(mra_result.get('endpoint') or '').strip().lower()

    response_payload = mra_result.get('response')
    reason = 'eis_unreachable'
    if isinstance(response_payload, dict):
        reason = str(response_payload.get('reason') or reason).strip().lower()

    # Allow offline dry-run only for genuine connectivity issues.
    if (
        endpoint_key == 'report_sale_offline'
        and mra_result.get('dry_run')
        and reason in NON_BLOCKING_OFFLINE_DRY_RUN_REASONS
    ):
        return

    if mra_result.get('dry_run'):
        message = submission_message or 'MRA EIS unavailable.'
        raise ValidationError(
            {
                'error': f'{message} Sale blocked by compliance policy (block_sales_if_eis_down).',
                'reason': reason,
                'order_id': str(order.id),
                'mra_submission': submission_payload,
            }
        )

    eis_status = str(mra_result.get('eis_status') or '').upper()
    if eis_status != 'SUBMITTED':
        message = submission_message or 'MRA EIS did not confirm submission.'
        raise ValidationError(
            {
                'error': f'{message} Sale blocked by compliance policy (block_sales_if_eis_down).',
                'eis_status': eis_status or 'UNKNOWN',
                'order_id': str(order.id),
                'mra_submission': submission_payload,
            }
        )


class OrderItemViewSet(viewsets.ModelViewSet):
    """ViewSet for managing order items"""
    queryset = OrderItem.objects.all()
    serializer_class = OrderItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter order items by order"""
        order_id = self.request.query_params.get('order_id')
        
        queryset = OrderItem.objects.all()
        
        if order_id:
            queryset = queryset.filter(order_id=order_id)
        
        return queryset


class DiscountRuleViewSet(viewsets.ModelViewSet):
    """Admin-managed discount rules. POS reads active rules from here."""
    serializer_class = DiscountRuleSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        business_ids = _get_accessible_business_ids(self.request.user)
        queryset = DiscountRule.objects.filter(business_id__in=business_ids)

        business_id = self.request.query_params.get('business_id')
        if business_id:
            queryset = queryset.filter(business_id=business_id)

        branch_id = self.request.query_params.get('branch_id')
        if branch_id:
            branch_lookup = _normalize_branch_reference(branch_id)
            if isinstance(branch_lookup, int):
                queryset = queryset.filter(Q(branch_id=branch_lookup) | Q(branch__isnull=True))

        active_only = str(self.request.query_params.get('active', '')).strip().lower()
        if active_only in {'1', 'true', 'yes'}:
            now = timezone.now()
            queryset = queryset.filter(is_active=True).filter(
                Q(starts_at__isnull=True) | Q(starts_at__lte=now),
                Q(ends_at__isnull=True) | Q(ends_at__gte=now),
            )

        product_id = str(self.request.query_params.get('product_id') or '').strip()
        category = str(self.request.query_params.get('category') or '').strip().lower()
        if product_id or category:
            scoped = []
            for rule in queryset:
                applies_to = str(rule.applies_to or 'all')
                if applies_to == 'all':
                    scoped.append(rule.id)
                    continue
                if applies_to == 'products' and product_id:
                    product_ids = [str(value) for value in (rule.product_ids or [])]
                    if product_id in product_ids:
                        scoped.append(rule.id)
                    continue
                if applies_to == 'categories' and category:
                    categories = [str(value).strip().lower() for value in (rule.categories or [])]
                    if category in categories:
                        scoped.append(rule.id)
            queryset = queryset.filter(id__in=scoped)

        return queryset.order_by('name')

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class OrderViewSet(viewsets.ModelViewSet):
    """ViewSet for managing POS orders"""
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Filter orders by session and branch"""
        session_id = self.request.query_params.get('session_id')
        branch_id = self.request.query_params.get('branch_id')
        
        queryset = Order.objects.all()
        
        if session_id:
            queryset = queryset.filter(session_id=session_id)
        
        if branch_id:
            queryset = _apply_branch_filter(queryset, branch_id)
        
        return queryset

    def create(self, request, *args, **kwargs):
        """
        Create order and apply backend stock movement using FIFO.
        This keeps backend inventory + purchase batches authoritative and aligned
        with expiry-aware stock consumption.
        """
        from business.models import Branch
        from .sync_views import InsufficientStockError, decrement_inventory_for_order
        
        # Prepare data for serializer
        data = request.data.copy()
        
        branch_obj = None
        # Resolve branch ID to branch object if needed
        if 'branch' in data and isinstance(data['branch'], str):
            try:
                branch_obj = Branch.objects.get(id=data['branch'])
                data['branch'] = branch_obj.id
            except Branch.DoesNotExist:
                return Response(
                    {'error': f'Branch {data["branch"]} not found'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        elif 'branch' in data:
            try:
                branch_obj = Branch.objects.get(id=data['branch'])
            except Branch.DoesNotExist:
                return Response(
                    {'error': f'Branch {data["branch"]} not found'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Enforce current-user active session (only one active session per user).
        active_session_qs = Session.objects.filter(user=request.user, status='active')
        if branch_obj:
            active_session_qs = active_session_qs.filter(branch=branch_obj)
            if getattr(branch_obj, 'business', None):
                active_session_qs = active_session_qs.filter(business=branch_obj.business)

        active_session = active_session_qs.order_by('-started_at').first()
        if not active_session:
            return Response(
                {'error': 'No active session found for current user in this branch. Please start a session.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if 'session' in data and data.get('session') and str(data.get('session')) != str(active_session.id):
            print(
                f"[Order] Overriding session {data.get('session')} with current user active session {active_session.id}"
            )

        data['session'] = active_session.id
        
        serializer = self.get_serializer(data=data)
        if not serializer.is_valid():
            print(f"[Order Create Error] Validation failed: {serializer.errors}")
            print(f"[Order Create Error] Request data: {request.data}")
        serializer.is_valid(raise_exception=True)
        
        order = None
        mra_result = None
        business_settings = None
        try:
            with transaction.atomic():
                # Create the order and apply stock movement as one local POS transaction.
                order = serializer.save()

                print(f"[Order] Order created: {order.id} with {order.items.count()} items")

                try:
                    decrement_inventory_for_order(order, order.branch, order.business)
                except InsufficientStockError as stock_err:
                    raise ValidationError(
                        {
                            'error': str(stock_err),
                            'reason': 'insufficient_stock',
                        }
                    )
                except Exception as stock_err:
                    print(f"[Order] Error applying FIFO stock decrement: {stock_err}")
                    raise ValidationError(
                        {
                            'error': 'Stock update failed. Sale was not completed.',
                            'details': str(stock_err),
                        }
                    )

                print(f"[Order] Local order {order.id} stock transaction completed")

        except ValidationError as e:
            print(f"[Order Create Error] Validation failed during order creation: {str(e)}")
            raise
        except Exception as e:
            print(f"[Order Create Error] Exception during order creation: {str(e)}")
            import traceback
            traceback.print_exc()
            raise

        # Run MRA EIS outside the local POS stock/order transaction. The MRA
        # pipeline has its own transaction handling and may queue offline
        # receipts; keeping it separate prevents MRA/receipt failures from
        # poisoning the local order transaction with TransactionManagementError.
        try:
            try:
                business_settings = order.business.settings
            except Exception:
                business_settings = None

            if bool(getattr(business_settings, 'enable_eis', False)):
                from mra_eis.services import POSOrderSubmissionService, TerminalService

                mra_result = POSOrderSubmissionService.prepare_pos_order_submission(
                    order,
                    request_device_serial=TerminalService.extract_request_device_serial(request),
                    enforce_device_binding=True,
                )
                mra_response = mra_result.get('response') if isinstance(mra_result, dict) else {}
                mra_reason = (
                    mra_response.get('reason')
                    if isinstance(mra_response, dict)
                    else ''
                ) or '; '.join(mra_result.get('errors') or [])
                print(
                    f"[Order] Prepared MRA payload for order {order.id}: "
                    f"fiscal={mra_result.get('fiscal_invoice_number')} "
                    f"endpoint={mra_result.get('endpoint')} "
                    f"dry_run={mra_result.get('dry_run')} reason={mra_reason or 'none'}"
                )
                _enforce_mra_submission_policy(order, business_settings, mra_result)
        except ValidationError:
            raise
        except Exception as mra_exc:
            print(f"[Order] Warning: MRA preparation failed for order {order.id}: {str(mra_exc)}")
            try:
                from mra_eis.services import MRAIntegrationError
            except Exception:
                MRAIntegrationError = Exception

            if isinstance(mra_exc, MRAIntegrationError):
                raise ValidationError(
                    {
                        'error': str(mra_exc),
                        'mra_submission': {
                            'state': 'failed_before_submission',
                            'message': str(mra_exc),
                            'retryable': False,
                        },
                        'order_id': str(order.id),
                    }
                )

            if bool(getattr(business_settings, 'enable_eis', False)):
                raise ValidationError(
                    {
                        'error': 'MRA EIS sale preparation failed. Sale was not completed.',
                        'details': str(mra_exc),
                        'mra_submission': {
                            'state': 'failed_before_submission',
                            'message': str(mra_exc),
                            'retryable': False,
                        },
                        'order_id': str(order.id),
                    }
                )

        print(f"[Order] Order {order.id} completed")
        
        order.refresh_from_db()
        response_data = self.get_serializer(order).data
        if isinstance(mra_result, dict):
            mra_submission_state = str(mra_result.get('submission_state') or '').strip().lower()
            response_data['mra_submission'] = {
                'state': mra_result.get('submission_state'),
                'message': mra_result.get('submission_message'),
                'retryable': mra_result.get('retryable'),
                'queued_offline': mra_result.get('queued_offline'),
                'endpoint': mra_result.get('endpoint'),
                'dry_run': mra_result.get('dry_run'),
                'errors': [] if mra_submission_state == 'offline_queued' else (mra_result.get('errors') or []),
                'fiscal_invoice_number': mra_result.get('fiscal_invoice_number') or order.fiscal_invoice_number or '',
                'eis_status': mra_result.get('eis_status') or order.eis_status or '',
                'offline_validation_url': mra_result.get('offline_validation_url') or '',
                'offline_signature': mra_result.get('offline_signature') or '',
            }
            if mra_submission_state == 'offline_queued' or bool(mra_result.get('queued_offline')):
                response_data['eis_sync_state'] = 'OFFLINE_QUEUED'
                response_data['fiscal_receipt_state'] = 'OFFLINE_QUEUED'
                response_data['fiscal_receipt_ready'] = True

        headers = self.get_success_headers(response_data)
        return Response(response_data, status=status.HTTP_201_CREATED, headers=headers)

    @action(detail=False, methods=['get'])
    def by_session(self, request):
        """Get all orders for a session"""
        session_id = request.query_params.get('session_id')
        
        if not session_id:
            return Response(
                {'error': 'session_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        orders = Order.objects.filter(session_id=session_id)
        serializer = self.get_serializer(orders, many=True)
        return Response(serializer.data)

    

class SessionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing POS sessions"""
    queryset = Session.objects.all()
    serializer_class = SessionSerializer
    permission_classes = [IsAuthenticated]

    def _get_scoped_queryset(
        self,
        include_user_filter=True,
        branch_reference=None,
        include_manager_full_scope=False,
    ):
        """
        Resolve session visibility scope by role:
        - Owner / Admin staff: all sessions in their business scope
        - Non-admin staff: only their own sessions
        """
        from business.models import Business
        from staff.models import Staff, StaffRole

        user = self.request.user
        requested_business_id = self.request.query_params.get('business_id')
        branch_id = (
            branch_reference
            if branch_reference is not None
            else self.request.query_params.get('branch_id')
        )

        owned_business_ids = list(
            Business.objects.filter(owner=user).values_list('id', flat=True)
        )
        staff_profile = Staff.objects.select_related('business').filter(
            user=user,
            is_active=True
        ).first()

        is_owner = len(owned_business_ids) > 0
        full_scope_staff_roles = {StaffRole.ADMIN}
        if include_manager_full_scope:
            full_scope_staff_roles.add(StaffRole.MANAGER)
        is_full_scope_staff = bool(staff_profile and staff_profile.role in full_scope_staff_roles)
        is_global_admin = bool(getattr(user, 'is_superuser', False))
        can_view_all_sessions = is_owner or is_full_scope_staff or is_global_admin

        if is_global_admin:
            queryset = Session.objects.all()
        elif is_owner:
            queryset = Session.objects.filter(business_id__in=owned_business_ids)
        elif staff_profile and staff_profile.business_id:
            queryset = Session.objects.filter(business_id=staff_profile.business_id)
        else:
            queryset = Session.objects.none()

        if include_user_filter and not can_view_all_sessions:
            queryset = queryset.filter(user=user)

        if requested_business_id:
            queryset = queryset.filter(business_id=requested_business_id)

        if branch_id:
            queryset = _apply_branch_filter(queryset, branch_id)

        return queryset

    def get_queryset(self):
        """Filter sessions by business scope, role, and optional branch."""
        return self._get_scoped_queryset()

    def perform_create(self, serializer):
        """
        Create session with per-user active session guard.
        Each user can only have one active session at a time.
        """
        branch = serializer.validated_data.get('branch')
        business = serializer.validated_data.get('business')

        if not branch:
            raise ValidationError({'branch': 'branch is required'})

        if not business and getattr(branch, 'business', None):
            business = branch.business

        existing_active = Session.objects.filter(
            user=self.request.user,
            status='active',
            business=business,
        ).select_related('branch').order_by('-started_at').first()

        if existing_active:
            branch_name = getattr(existing_active.branch, 'name', '') or str(existing_active.branch_id)
            raise ValidationError({
                'detail': (
                    'You already have an active session. '
                    f'Close it before starting a new one (branch: {branch_name}).'
                )
            })

        serializer.save(user=self.request.user, business=business)

    @action(detail=False, methods=['get'])
    def active(self, request):
        """
        Get active session for the requesting user in the requested branch.
        Branch filter is required and user scope is preserved.
        """
        branch_ref = _normalize_branch_reference(request.query_params.get('branch_id'))

        if branch_ref is None:
            return Response(
                {'error': 'branch_id is required and must be valid'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        session = self._get_scoped_queryset(
            include_user_filter=True,
            branch_reference=branch_ref,
        ).filter(status='active').order_by('-started_at').first()
        
        if not session:
            return Response(
                {'error': 'No active session found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        serializer = self.get_serializer(session)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def active_list(self, request):
        """
        Get active sessions in current visibility scope.
        Non-admin users see their own sessions; admins/owners can see all in scope.

        Query params:
        - branch_id (optional): if provided, limit to one branch.
        """
        queryset = self.get_queryset().filter(status='active').order_by('-started_at')
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def closed_list(self, request):
        """
        Get closed sessions in current visibility scope for history/reprint workflows.
        Cashiers see only their own closed sessions; admins, managers, and owners can see all in scope.

        Query params:
        - branch_id (optional): if provided, limit to one branch.
        - business_id (optional): if provided, limit to one business.
        """
        queryset = self._get_scoped_queryset(
            include_user_filter=True,
            include_manager_full_scope=True,
        ).filter(status='closed').order_by('-started_at')
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """Close a session"""
        session = self.get_object()
        
        if session.status == 'closed':
            return Response(
                {'error': 'Session is already closed'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Log incoming data
        print(f'[Sessions] Close action received data: {request.data}')
        
        # Prepare data for serializer
        data = request.data.copy()
        data['status'] = 'closed'
        
        # Ensure closing_stock is properly formatted
        if 'closing_stock' in data:
            print(f'[Sessions] Closing stock data: {data["closing_stock"]}')
        
        serializer = self.get_serializer(session, data=data, partial=True)
        if serializer.is_valid():
            print(f'[Sessions] Serializer valid, saving session')
            saved_session = serializer.save()
            print(f'[Sessions] Session saved. Closing stock: {saved_session.closing_stock}')
            return Response(serializer.data)
        
        print(f'[Sessions] Serializer errors: {serializer.errors}')
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
