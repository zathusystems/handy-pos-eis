"""
MRA EIS API Views - REST endpoints for MRA integration
"""
import re

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.db.models import Q
from .models import (
    Terminal, TerminalActivationCode, MRAConfiguration,
    MRAInvoice, OfflineInvoiceQueue, Receipt, InvoiceAuditLog,
    TerminalAuditLog, MRAAPIError
)
from .serializers import (
    TerminalSerializer, TerminalDetailSerializer, TerminalActivationSerializer,
    MRAConfigurationSerializer, MRAInvoiceSerializer,
    MRAInvoiceCreateSerializer, OfflineInvoiceQueueSerializer,
    ReceiptSerializer, InvoiceAuditLogSerializer, TerminalAuditLogSerializer,
    MRAAPIErrorSerializer, TerminalStatusSerializer, SyncStatusSerializer
)
from .services import (
    TerminalService, ConfigurationService, ProductMappingService,
    InvoiceService, ReceiptService, RetryService, POSOrderSubmissionService,
    EISSaleComplianceService, MRAIntegrationError, ReceiptLookupService,
    TransactionReconciliationService
)
from .services.core import _is_mra_network_failure
from rest_framework.views import APIView


def _get_accessible_business_queryset(user):
    """
    Return businesses user can operate on for MRA actions.
    Supports owners, superusers, and active staff assignments.
    """
    from business.models import Business

    if getattr(user, 'is_superuser', False):
        return Business.objects.all()

    owned_qs = Business.objects.filter(owner=user)
    if owned_qs.exists():
        return owned_qs

    try:
        from staff.models import Staff

        staff_business_ids = Staff.objects.filter(
            user=user,
            is_active=True
        ).values_list('business_id', flat=True)
        return Business.objects.filter(id__in=staff_business_ids)
    except Exception:
        return Business.objects.none()


def _normalize_mra_tax_type(value):
    normalized = str(value or '').strip().lower()
    if normalized in {'zero', 'zero_rated', 'zero-rated', 'vat_zero', 'vat-zero', '0'}:
        return 'zero'
    if normalized in {'exempt', 'vat_exempt', 'vat-exempt'}:
        return 'exempt'
    return 'standard'


def _normalize_mra_tax_rate(value, tax_type):
    if value is None or value == '':
        return 0.0 if tax_type in {'zero', 'exempt'} else 16.5

    try:
        if isinstance(value, str):
            value = value.replace('%', '').strip()
        parsed = float(value)
        if parsed < 0:
            return 0.0
        return parsed
    except (TypeError, ValueError):
        return 0.0 if tax_type in {'zero', 'exempt'} else 16.5


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {'1', 'true', 'yes', 'y', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'n', 'off'}:
        return False
    return default


def _normalize_branch_lookup(value):
    raw = str(value or '').strip()
    if not raw:
        return raw
    legacy_match = re.match(r'^(?:BRN|branch)-(\d+)$', raw, flags=re.IGNORECASE)
    if legacy_match:
        return legacy_match.group(1)
    return raw


def _normalize_product_code_item(item):
    if not isinstance(item, dict):
        return None

    code = (
        item.get('code')
        or item.get('mra_product_code')
        or item.get('product_code')
        or item.get('productCode')
        or item.get('productId')
        or item.get('productID')
        or item.get('item_code')
        or item.get('itemCode')
        or item.get('hs_code')
        or item.get('hsCode')
    )
    if code is None:
        return None

    code = str(code).strip().upper()
    if not code:
        return None

    name = (
        item.get('name')
        or item.get('mra_product_name')
        or item.get('product_name')
        or item.get('productName')
        or item.get('description')
        or code
    )
    name = str(name).strip() or code

    category = (
        item.get('category')
        or item.get('product_category')
        or item.get('productCategory')
        or item.get('group')
        or item.get('group_name')
        or item.get('groupName')
        or 'General'
    )
    category = str(category).strip() or 'General'

    tax_type = _normalize_mra_tax_type(
        item.get('default_tax_type')
        or item.get('defaultTaxType')
        or item.get('tax_type')
        or item.get('taxType')
        or item.get('vat_type')
        or item.get('vatType')
        or item.get('vat_category')
        or item.get('vatCategory')
    )
    tax_rate = _normalize_mra_tax_rate(
        item.get('default_tax_rate')
        or item.get('defaultTaxRate')
        or item.get('tax_rate')
        or item.get('taxRate')
        or item.get('vat_rate')
        or item.get('vatRate'),
        tax_type,
    )

    return {
        'code': code,
        'name': name,
        'category': category,
        'default_tax_type': tax_type,
        'default_tax_rate': tax_rate,
    }


def _extract_product_codes_from_config(config_data):
    if not config_data:
        return []

    queue = [config_data]
    extracted = []
    seen_codes = set()

    while queue:
        current = queue.pop(0)

        if isinstance(current, list):
            for entry in current:
                if isinstance(entry, (dict, list)):
                    queue.append(entry)
            continue

        if not isinstance(current, dict):
            continue

        normalized_item = _normalize_product_code_item(current)
        if normalized_item:
            code = normalized_item['code']
            if code not in seen_codes:
                seen_codes.add(code)
                extracted.append(normalized_item)

        for value in current.values():
            if isinstance(value, (dict, list)):
                queue.append(value)

    return extracted


class TerminalViewSet(viewsets.ModelViewSet):
    """
    ViewSet for terminal management.
    Handles activation, status, and configuration.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = TerminalSerializer

    def get_queryset(self):
        """Filter terminals by business"""
        business_ids = _get_accessible_business_queryset(self.request.user).values_list('id', flat=True)
        return Terminal.objects.filter(
            business_id__in=business_ids
        ).select_related('business', 'branch')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TerminalDetailSerializer
        elif self.action == 'activate':
            return TerminalActivationSerializer
        return TerminalSerializer

    @action(detail=False, methods=['post'])
    def activate(self, request):
        """Activate a new terminal using TAC"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            # Get business and branch from request
            business_id = request.query_params.get('business_id')
            branch_id = request.query_params.get('branch_id')

            from business.models import Business, Branch
            accessible_businesses = _get_accessible_business_queryset(request.user)
            business = get_object_or_404(accessible_businesses, id=business_id)
            branch = get_object_or_404(Branch, id=branch_id, business=business)

            terminal = TerminalService.activate_terminal(
                business=business,
                branch=branch,
                tac_code=serializer.validated_data['tac_code'],
                pos_name=serializer.validated_data['pos_name'],
                pos_version=serializer.validated_data['pos_version'],
                os_type=serializer.validated_data['os_type'],
                device_serial=serializer.validated_data['device_serial'],
                mac_address=serializer.validated_data.get('mac_address', '')
            )

            return Response(
                TerminalDetailSerializer(terminal).data,
                status=status.HTTP_201_CREATED
            )
        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        except MRAIntegrationError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_502_BAD_GATEWAY
            )

    @action(detail=True, methods=['post'])
    def refresh_token(self, request, pk=None):
        """Refresh MRA authentication token"""
        terminal = self.get_object()
        try:
            TerminalService.refresh_token(terminal)
            return Response(
                TerminalDetailSerializer(terminal).data,
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def reset_activation(self, request, pk=None):
        """Remove a local failed terminal activation so onboarding can be retried."""
        terminal = self.get_object()
        try:
            result = TerminalService.reset_failed_activation(terminal)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['get'])
    def status(self, request, pk=None):
        """Get terminal status, using MRA utilities ping for online state."""
        terminal = self.get_object()
        should_ping = _parse_bool(request.query_params.get('ping'), True)
        health_check = TerminalService.check_terminal_health(terminal) if should_ping else None
        if health_check is not None:
            terminal.refresh_from_db()

        pending_offline = OfflineInvoiceQueue.objects.filter(
            terminal=terminal,
            status='queued'
        ).count()

        serializer = TerminalStatusSerializer({
            'id': str(terminal.id),
            'business': str(terminal.business_id),
            'branch': str(terminal.branch_id),
            'terminal_id': terminal.terminal_id,
            'mra_terminal_id': terminal.mra_terminal_id,
            'device_serial': terminal.device_serial,
            'mac_address': terminal.mac_address,
            'pos_name': terminal.pos_name,
            'pos_version': terminal.pos_version,
            'os_type': terminal.os_type,
            'status': terminal.status,
            'is_online': terminal.is_online,
            'has_mra_token': bool(terminal.mra_token),
            'online_invoice_counter': terminal.online_invoice_counter,
            'offline_invoice_counter': terminal.offline_invoice_counter,
            'pending_offline_invoices': pending_offline,
            'activated_at': terminal.activated_at,
            'token_expires_at': terminal.token_expires_at,
            'last_sync_at': terminal.last_sync_at,
            'blocking_status': TerminalService.get_cached_blocking_status(terminal),
            'health_check': health_check,
        })

        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def health_check(self, request, pk=None):
        """Run the official MRA utilities ping for this terminal."""
        terminal = self.get_object()
        health_check = TerminalService.check_terminal_health(terminal)
        terminal.refresh_from_db()
        return Response(
            {
                **health_check,
                'terminal': TerminalDetailSerializer(terminal).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'])
    def update_online_status(self, request, pk=None):
        """Update terminal online/offline status"""
        terminal = self.get_object()
        is_online = request.data.get('is_online', True)

        TerminalService.update_online_status(terminal, is_online)

        return Response(
            TerminalDetailSerializer(terminal).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'])
    def check_blocking_status(self, request, pk=None):
        """Fetch MRA terminal block message and unblock status."""
        terminal = self.get_object()
        try:
            result = TerminalService.sync_terminal_blocking_status(terminal)
            result['terminal'] = TerminalDetailSerializer(terminal).data
            return Response(result, status=status.HTTP_200_OK)
        except MRAIntegrationError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def reconcile_last_transactions(self, request, pk=None):
        """Reconcile local sale state with MRA's last online/offline transactions."""
        terminal = self.get_object()
        modes = request.data.get('modes') or request.data.get('mode') or ['online', 'offline']
        if isinstance(modes, str):
            modes = [modes]

        result = TransactionReconciliationService.reconcile_terminal(terminal, modes=modes)
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def lookup_invoice(self, request, pk=None):
        """Look up a submitted MRA receipt by fiscal invoice number."""
        terminal = self.get_object()
        invoice_number = (
            request.data.get('invoiceNumber')
            or request.data.get('invoice_number')
            or request.data.get('receiptNumber')
            or request.data.get('receipt_number')
            or ''
        )
        try:
            result = ReceiptLookupService.lookup_invoice_by_number(terminal, invoice_number)
            return Response(result, status=status.HTTP_200_OK)
        except MRAIntegrationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['post'])
    def get_void_receipts(self, request, pk=None):
        """Fetch MRA cancelled/void receipt requests for certification evidence."""
        terminal = self.get_object()
        try:
            result = ReceiptLookupService.get_void_receipts(
                terminal,
                invoice_number=(
                    request.data.get('invoiceNumber')
                    or request.data.get('invoice_number')
                    or request.data.get('receiptNumber')
                    or request.data.get('receipt_number')
                    or ''
                ),
                status_value=request.data.get('status'),
                start_date=request.data.get('startDate') or request.data.get('start_date') or '',
                end_date=request.data.get('endDate') or request.data.get('end_date') or '',
                page=request.data.get('page') or 1,
                page_size=request.data.get('pageSize') or request.data.get('page_size') or 25,
            )
            return Response(result, status=status.HTTP_200_OK)
        except MRAIntegrationError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['get'])
    def audit_logs(self, request, pk=None):
        """Get terminal audit logs"""
        terminal = self.get_object()
        logs = TerminalAuditLog.objects.filter(terminal=terminal).order_by('-created_at')[:100]

        serializer = TerminalAuditLogSerializer(logs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def submit_initial_inventory(self, request, pk=None):
        """Submit taxpayer initial inventory products to MRA EIS."""
        terminal = self.get_object()
        tin = request.data.get('TIN') or request.data.get('tin') or terminal.business.tin or ''
        products = request.data.get('Products') or request.data.get('products') or []
        is_last_batch = request.data.get(
            'isLastBatch',
            request.data.get('IsLastBatch', request.data.get('is_last_batch', False)),
        )

        try:
            result = ProductMappingService.submit_initial_inventory(
                business=terminal.business,
                terminal=terminal,
                tin=tin,
                products=products,
                is_last_batch=_parse_bool(is_last_batch),
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['post'])
    def import_initial_inventory(self, request, pk=None):
        """Import MRA uploaded initial inventory into POS inventory and mappings."""
        terminal = self.get_object()
        data = request.data
        if isinstance(data, list):
            products = data
            mark_as_mra_synced = False
        else:
            products = data.get('Products') or data.get('products') or data.get('items') or []
            mark_as_mra_synced = data.get(
                'markAsMraSynced',
                data.get('mark_as_mra_synced', data.get('mraSynced', False)),
            )

        try:
            result = ProductMappingService.import_initial_inventory_to_pos(
                business=terminal.business,
                terminal=terminal,
                products=products,
                user=request.user,
                mark_as_mra_synced=_parse_bool(mark_as_mra_synced),
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['post'])
    def pull_approved_products(self, request, pk=None):
        """Pull MRA portal-approved terminal/site products into POS inventory."""
        terminal = self.get_object()
        refresh_from_mra = request.data.get(
            'refreshFromMra',
            request.data.get('refresh_from_mra', True),
        )

        try:
            result = ProductMappingService.pull_approved_products_to_inventory(
                business=terminal.business,
                terminal=terminal,
                user=request.user,
                refresh_from_mra=_parse_bool(refresh_from_mra),
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['post'])
    def add_product(self, request, pk=None):
        """Submit a new product to MRA EIS add-product endpoint."""
        terminal = self.get_object()
        product_payload = request.data.get('product') if isinstance(request.data, dict) else None
        if not isinstance(product_payload, dict):
            product_payload = request.data

        try:
            result = ProductMappingService.add_product_to_mra(
                business=terminal.business,
                terminal=terminal,
                product=product_payload,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except MRAIntegrationError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['get'])
    def hs_codes(self, request, pk=None):
        """Fetch MRA HS codes used by the add-product endpoint."""
        terminal = self.get_object()
        try:
            result = ProductMappingService.fetch_hs_codes(
                business=terminal.business,
                terminal=terminal,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except MRAIntegrationError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['get'])
    def units_of_measure(self, request, pk=None):
        """Fetch MRA units of measure used by the add-product endpoint."""
        terminal = self.get_object()
        try:
            result = ProductMappingService.fetch_units_of_measure(
                business=terminal.business,
                terminal=terminal,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except MRAIntegrationError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['post'])
    def reconcile_inventory(self, request, pk=None):
        """Compare local approved POS inventory with EIS warehouse stock."""
        terminal = self.get_object()
        branch = terminal.branch
        branch_id = request.data.get('branch_id') or request.data.get('branchId')
        if branch_id:
            from business.models import Branch
            branch = get_object_or_404(Branch, id=branch_id, business=terminal.business)

        try:
            result = ProductMappingService.reconcile_inventory_with_eis(
                business=terminal.business,
                terminal=terminal,
                branch=branch,
            )
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['get'])
    def warehouse_inventory(self, request, pk=None):
        """Fetch official MRA warehouse stock for this taxpayer."""
        terminal = self.get_object()
        try:
            page_size = int(request.query_params.get('page_size') or request.query_params.get('pageSize') or 200)
        except (TypeError, ValueError):
            page_size = 200
        try:
            max_pages = int(request.query_params.get('max_pages') or request.query_params.get('maxPages') or 25)
        except (TypeError, ValueError):
            max_pages = 25

        try:
            result = ProductMappingService.fetch_warehouse_inventory(
                business=terminal.business,
                terminal=terminal,
                page_size=page_size,
                max_pages=max_pages,
            )
            return Response(result, status=status.HTTP_200_OK)
        except MRAIntegrationError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['post'])
    def transfer_inventory(self, request, pk=None):
        """Transfer official MRA inventory between warehouse and mapped sites."""
        terminal = self.get_object()
        from_branch = None
        to_branch = None
        from_branch_id = request.data.get('fromBranchId') or request.data.get('from_branch_id')
        to_branch_id = request.data.get('toBranchId') or request.data.get('to_branch_id') or request.data.get('branch_id')
        if from_branch_id:
            from business.models import Branch
            from_branch = get_object_or_404(Branch, id=_normalize_branch_lookup(from_branch_id), business=terminal.business)
        if to_branch_id:
            from business.models import Branch
            to_branch = get_object_or_404(Branch, id=_normalize_branch_lookup(to_branch_id), business=terminal.business)

        try:
            from_warehouse_to_site = _parse_bool(
                request.data.get('fromWarehouseToSite', request.data.get('from_warehouse_to_site', True)),
                True,
            )
            from_site_id = request.data.get('fromSiteId') or request.data.get('from_site_id') or ''
            if not from_warehouse_to_site and from_branch is not None and not str(from_site_id or '').strip():
                from_site_id = (
                    getattr(from_branch, 'mra_site_id', '')
                    or getattr(from_branch, 'mra_branch_code', '')
                    or ConfigurationService.get_terminal_site_id(terminal.business, from_branch)
                    or ''
                )

            to_site_id = request.data.get('toSiteId') or request.data.get('to_site_id') or ''
            if to_branch is not None and not str(to_site_id or '').strip():
                to_site_id = (
                    getattr(to_branch, 'mra_site_id', '')
                    or getattr(to_branch, 'mra_branch_code', '')
                    or ConfigurationService.get_terminal_site_id(terminal.business, to_branch)
                    or ''
                )

            result = ProductMappingService.transfer_inventory(
                business=terminal.business,
                terminal=terminal,
                items=request.data.get('items') or [],
                to_branch=to_branch,
                to_site_id=to_site_id,
                from_site_id=from_site_id,
                from_warehouse_to_site=from_warehouse_to_site,
            )
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except MRAIntegrationError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class MRAConfigurationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for MRA configurations.
    Read-only - configurations are fetched from MRA.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = MRAConfigurationSerializer

    def get_queryset(self):
        """Filter configurations by business"""
        business_ids = _get_accessible_business_queryset(self.request.user).values_list('id', flat=True)
        queryset = MRAConfiguration.objects.filter(
            business_id__in=business_ids,
            is_active=True
        )

        business_id = self.request.query_params.get('business_id')
        if business_id:
            queryset = queryset.filter(business_id=business_id)

        return queryset.order_by('-effective_from')

    @action(detail=False, methods=['post'])
    def sync_from_mra(self, request):
        """Fetch and sync configurations from MRA"""
        business_id = request.query_params.get('business_id')
        terminal_id = request.query_params.get('terminal_id')
        accessible_businesses = _get_accessible_business_queryset(request.user)
        business = get_object_or_404(accessible_businesses, id=business_id)

        config_types = request.data.get('config_types', None)
        terminal = None
        if terminal_id:
            terminal = get_object_or_404(Terminal, id=terminal_id, business=business)
        else:
            terminal = (
                Terminal.objects.filter(business=business)
                .exclude(mra_token='')
                .order_by('-updated_at')
                .first()
            )

        try:
            sync_log = ConfigurationService.fetch_and_store_configuration(
                business=business,
                config_types=config_types,
                terminal=terminal,
            )
            product_sync = None
            if request.data.get('sync_products', False):
                product_sync = ProductMappingService.sync_terminal_site_products(
                    business=business,
                    terminal=terminal,
                )

            return Response(
                {
                    'status': sync_log.status,
                    'config_types': sync_log.config_types,
                    'completed_at': sync_log.completed_at,
                    'product_sync': product_sync,
                },
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=False, methods=['post'])
    def ensure_fresh(self, request):
        """Refresh MRA configuration if it is missing or older than policy allows."""
        business_id = request.query_params.get('business_id') or request.data.get('business_id')
        terminal_id = request.query_params.get('terminal_id') or request.data.get('terminal_id')
        accessible_businesses = _get_accessible_business_queryset(request.user)
        business = get_object_or_404(accessible_businesses, id=business_id)
        terminal = None
        if terminal_id:
            terminal = get_object_or_404(Terminal, id=terminal_id, business=business)
        try:
            result = ConfigurationService.ensure_fresh_configuration(
                business,
                terminal=terminal,
                require_success=_parse_bool(request.data.get('require_success'), False),
            )
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class MRAInvoiceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for MRA invoices.
    Handles creation, submission, and offline queuing.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = MRAInvoiceSerializer

    def get_queryset(self):
        """Filter invoices by business"""
        business_ids = _get_accessible_business_queryset(self.request.user).values_list('id', flat=True)
        return MRAInvoice.objects.filter(
            business_id__in=business_ids
        ).select_related('terminal', 'branch')

    def get_serializer_class(self):
        if self.action == 'create':
            return MRAInvoiceCreateSerializer
        return MRAInvoiceSerializer

    def create(self, request, *args, **kwargs):
        """Create an invoice"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        terminal_id = request.query_params.get('terminal_id')
        business_ids = _get_accessible_business_queryset(request.user).values_list('id', flat=True)
        terminal = get_object_or_404(Terminal, id=terminal_id, business_id__in=business_ids)

        try:
            invoice = InvoiceService.create_invoice(
                terminal=terminal,
                seller_tin=serializer.validated_data['seller_tin'],
                seller_name=serializer.validated_data['seller_name'],
                items=serializer.validated_data['items'],
                buyer_tin=serializer.validated_data.get('buyer_tin'),
                buyer_name=serializer.validated_data.get('buyer_name'),
                is_online=serializer.validated_data.get('is_online', True)
            )

            return Response(
                MRAInvoiceSerializer(invoice).data,
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Submit invoice to MRA"""
        invoice = self.get_object()

        try:
            InvoiceService.submit_invoice(invoice)
            return Response(
                MRAInvoiceSerializer(invoice).data,
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def queue_offline(self, request, pk=None):
        """Queue invoice for offline sync"""
        invoice = self.get_object()

        try:
            queue_entry = InvoiceService.queue_offline_invoice(invoice)
            return Response(
                OfflineInvoiceQueueSerializer(queue_entry).data,
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=False, methods=['post'])
    def sync_offline(self, request):
        """Sync offline invoices for a terminal"""
        terminal_id = request.query_params.get('terminal_id')
        business_ids = _get_accessible_business_queryset(request.user).values_list('id', flat=True)
        terminal = get_object_or_404(Terminal, id=terminal_id, business_id__in=business_ids)

        try:
            result = InvoiceService.sync_offline_invoices(terminal)

            pending = OfflineInvoiceQueue.objects.filter(
                terminal=terminal,
                status='queued'
            ).count()

            serializer = SyncStatusSerializer({
                'synced_count': result['synced'],
                'failed_count': result['failed'],
                'pending_count': pending,
                'last_sync_at': terminal.last_sync_at,
            })

            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['get'])
    def audit_logs(self, request, pk=None):
        """Get invoice audit logs"""
        invoice = self.get_object()
        logs = InvoiceAuditLog.objects.filter(
            mra_invoice=invoice
        ).order_by('-created_at')

        serializer = InvoiceAuditLogSerializer(logs, many=True)
        return Response(serializer.data)


class ReceiptViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for receipts.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ReceiptSerializer

    def get_queryset(self):
        """Filter receipts by business"""
        business_ids = _get_accessible_business_queryset(self.request.user).values_list('id', flat=True)
        return Receipt.objects.filter(
            mra_invoice__business_id__in=business_ids
        ).select_related('mra_invoice')

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """Generate receipt for an invoice"""
        invoice_id = request.query_params.get('invoice_id')
        invoice = get_object_or_404(MRAInvoice, id=invoice_id)

        try:
            receipt = ReceiptService.generate_receipt(invoice)
            return Response(
                ReceiptSerializer(receipt).data,
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class OfflineInvoiceQueueViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for offline invoice queue.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = OfflineInvoiceQueueSerializer

    def get_queryset(self):
        """Filter queue by business"""
        business_ids = _get_accessible_business_queryset(self.request.user).values_list('id', flat=True)
        return OfflineInvoiceQueue.objects.filter(
            terminal__business_id__in=business_ids
        ).select_related('terminal', 'mra_invoice')

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get pending offline invoices for a terminal"""
        terminal_id = request.query_params.get('terminal_id')
        business_ids = _get_accessible_business_queryset(request.user).values_list('id', flat=True)
        terminal = get_object_or_404(Terminal, id=terminal_id, business_id__in=business_ids)

        queue = OfflineInvoiceQueue.objects.filter(
            terminal=terminal,
            status__in=['queued', 'failed']
        ).order_by('queue_position')

        serializer = self.get_serializer(queue, many=True)
        return Response(serializer.data)


class MRAAPIErrorViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for API errors.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = MRAAPIErrorSerializer

    def get_queryset(self):
        """Filter errors by business"""
        business_ids = _get_accessible_business_queryset(self.request.user).values_list('id', flat=True)
        return MRAAPIError.objects.filter(
            terminal__business_id__in=business_ids
        ).select_related('terminal')

    @action(detail=False, methods=['get'])
    def unresolved(self, request):
        """Get unresolved errors"""
        errors = self.get_queryset().filter(is_resolved=False).order_by('-created_at')[:50]
        serializer = self.get_serializer(errors, many=True)
        return Response(serializer.data)


class MRAProductCodesView(APIView):
    """
    API endpoint for fetching available MRA product codes.
    This is used by the product mapping form to show available MRA products.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Get available MRA product codes.
        
        Returns a list of MRA-approved product codes that can be used for mapping.
        This data is typically fetched from MRA or stored in a configuration.
        """
        include_meta = str(request.query_params.get('include_meta', '')).lower() in {'1', 'true', 'yes'}
        search_query = request.query_params.get('search', '').lower().strip()
        business_id = request.query_params.get('business_id')
        accessible_businesses = _get_accessible_business_queryset(request.user)

        business = None
        if business_id:
            business = get_object_or_404(accessible_businesses, id=business_id)
        else:
            business = accessible_businesses.first()

        catalog_source = 'fallback_catalog'
        config_version = None
        mra_products = []

        # Primary source: active synced MRA product code configuration for the business.
        if business:
            product_config = ConfigurationService.get_active_configuration(business, 'product_codes')
            if product_config:
                extracted_products = _extract_product_codes_from_config(product_config.config_data)
                if extracted_products:
                    mra_products = extracted_products
                    catalog_source = 'mra_configuration'
                    config_version = product_config.config_version

        strict_product_codes = bool(getattr(settings, 'MRA_EIS_STRICT_PRODUCT_CODES', False))
        if strict_product_codes and not mra_products:
            message = (
                'No active MRA product code configuration found. '
                'Run configuration sync before creating or updating MRA mappings.'
            )
            if include_meta:
                return Response(
                    {
                        'results': [],
                        'count': 0,
                        'source': 'strict_mode',
                        'config_version': config_version,
                        'business_id': str(business.id) if business else None,
                        'error': message,
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE
                )

            return Response(
                {
                    'error': message,
                    'source': 'strict_mode',
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        # Fallback source: local static catalog so mapping can continue offline.
        fallback_products = [
            # BEVERAGES
            {
                'code': 'BEVERAGE-001',
                'name': 'Soft Drink',
                'category': 'Beverages',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'BEVERAGE-002',
                'name': 'Juice',
                'category': 'Beverages',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'BEVERAGE-003',
                'name': 'Water',
                'category': 'Beverages',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            {
                'code': 'BEVERAGE-004',
                'name': 'Alcoholic Beverage',
                'category': 'Beverages',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'BEVERAGE-005',
                'name': 'Coffee',
                'category': 'Beverages',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'BEVERAGE-006',
                'name': 'Tea',
                'category': 'Beverages',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            
            # FOOD
            {
                'code': 'FOOD-001',
                'name': 'Bread',
                'category': 'Food',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            {
                'code': 'FOOD-002',
                'name': 'Milk',
                'category': 'Food',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            {
                'code': 'FOOD-003',
                'name': 'Meat',
                'category': 'Food',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            {
                'code': 'FOOD-004',
                'name': 'Vegetables',
                'category': 'Food',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            {
                'code': 'FOOD-005',
                'name': 'Fruits',
                'category': 'Food',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            {
                'code': 'FOOD-006',
                'name': 'Prepared Meal',
                'category': 'Food',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'FOOD-007',
                'name': 'Snacks',
                'category': 'Food',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            
            # PHARMACY
            {
                'code': 'PHARMA-001',
                'name': 'Medicine',
                'category': 'Pharmacy',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            {
                'code': 'PHARMA-002',
                'name': 'Vitamin',
                'category': 'Pharmacy',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            {
                'code': 'PHARMA-003',
                'name': 'Medical Device',
                'category': 'Pharmacy',
                'default_tax_type': 'zero',
                'default_tax_rate': 0,
            },
            
            # FUEL
            {
                'code': 'FUEL-001',
                'name': 'Petrol',
                'category': 'Fuel',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'FUEL-002',
                'name': 'Diesel',
                'category': 'Fuel',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'FUEL-003',
                'name': 'Kerosene',
                'category': 'Fuel',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            
            # SERVICES
            {
                'code': 'SERVICE-001',
                'name': 'Haircut',
                'category': 'Services',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'SERVICE-002',
                'name': 'Repair Service',
                'category': 'Services',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'SERVICE-003',
                'name': 'Consultation',
                'category': 'Services',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'SERVICE-004',
                'name': 'Delivery',
                'category': 'Services',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            
            # RETAIL
            {
                'code': 'RETAIL-001',
                'name': 'Clothing',
                'category': 'Retail',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'RETAIL-002',
                'name': 'Electronics',
                'category': 'Retail',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
            {
                'code': 'RETAIL-003',
                'name': 'Household Items',
                'category': 'Retail',
                'default_tax_type': 'standard',
                'default_tax_rate': 16.5,
            },
        ]

        if not mra_products:
            mra_products = fallback_products

        # Filter by search query if provided
        if search_query:
            mra_products = [
                p for p in mra_products
                if search_query in p['code'].lower()
                or search_query in p['name'].lower()
                or search_query in p.get('category', '').lower()
            ]

        if include_meta:
            return Response(
                {
                    'results': mra_products,
                    'count': len(mra_products),
                    'source': catalog_source,
                    'config_version': config_version,
                    'business_id': str(business.id) if business else None,
                },
                status=status.HTTP_200_OK
            )

        return Response(mra_products, status=status.HTTP_200_OK)


class PreparePendingPOSOrdersView(APIView):
    """
    Prepare pending POS orders for MRA submission without forcing live submission.

    This endpoint is rollout-safe when dry-run is enabled.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        business_id = request.query_params.get('business_id')
        branch_id = request.query_params.get('branch_id')
        limit = request.data.get('limit', 100)

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100

        accessible_businesses = _get_accessible_business_queryset(request.user)
        if not accessible_businesses.exists():
            return Response(
                {'error': 'User has no accessible business for MRA preparation.'},
                status=status.HTTP_403_FORBIDDEN
            )

        business = None
        if business_id:
            business = get_object_or_404(accessible_businesses, id=business_id)
        else:
            business = accessible_businesses.first()

        branch = None
        if branch_id:
            from business.models import Branch
            branch = get_object_or_404(Branch, id=branch_id, business=business)

        result = POSOrderSubmissionService.prepare_pending_pos_orders(
            business=business,
            branch=branch,
            limit=limit
        )

        return Response(
            {
                'business_id': str(business.id) if business else None,
                'branch_id': str(branch.id) if branch else None,
                **result,
            },
            status=status.HTTP_200_OK
        )


class MRAUtilityView(APIView):
    """Thin wrappers around MRA utility validation endpoints."""
    permission_classes = [IsAuthenticated]

    def _resolve_business_and_terminal(self, request):
        business_id = request.query_params.get('business_id') or request.data.get('business_id')
        terminal_id = request.query_params.get('terminal_id') or request.data.get('terminal_id')
        accessible_businesses = _get_accessible_business_queryset(request.user)
        business = get_object_or_404(accessible_businesses, id=business_id)
        terminal = None
        if terminal_id:
            terminal = get_object_or_404(Terminal, id=terminal_id, business=business)
        return business, terminal

    def post(self, request, action_name):
        business, terminal = self._resolve_business_and_terminal(request)
        try:
            if action_name == 'check-tin-authorization':
                result = EISSaleComplianceService.check_tin_authorization_requirement(
                    business=business,
                    tin=request.data.get('tin') or request.data.get('buyerTIN') or request.data.get('buyerTin') or '',
                    terminal=terminal,
                )
            elif action_name == 'validate-authorization-code':
                result = EISSaleComplianceService.validate_authorization_code(
                    business=business,
                    authorization_code=(
                        request.data.get('authorizationCode')
                        or request.data.get('buyerAuthorizationCode')
                        or request.data.get('authorization_code')
                        or ''
                    ),
                    terminal=terminal,
                )
            elif action_name == 'validate-vat5':
                result = EISSaleComplianceService.validate_vat5_certificate(
                    business=business,
                    project_number=request.data.get('projectNumber') or request.data.get('project_number') or '',
                    certificate_number=(
                        request.data.get('certificateNumber')
                        or request.data.get('vat5CertificateNumber')
                        or request.data.get('certificate_number')
                        or ''
                    ),
                    quantity=request.data.get('quantity') or request.data.get('vat5Quantity') or 0,
                    terminal=terminal,
                )
            elif action_name == 'ping':
                if terminal is None:
                    raise ValueError('terminal_id is required for MRA ping.')
                result = TerminalService.check_terminal_health(terminal)
            else:
                return Response({'error': 'Unknown MRA utility action.'}, status=status.HTTP_404_NOT_FOUND)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            if _is_mra_network_failure(e):
                message = 'B2B sales need MRA online.' if action_name in {
                    'check-tin-authorization',
                    'validate-authorization-code',
                } else 'MRA is offline.'
                return Response(
                    {
                        'error': message,
                        'code': 'mra_network_unreachable',
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
