"""
MRA EIS-Compliant Inventory Views

Provides API endpoints for inventory operations with MRA compliance.
Maintains backward compatibility with existing views.
"""

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from django.http import Http404
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.utils import timezone
from decimal import Decimal
import uuid
import re

from business.models import Business, Branch
from .models import (
    Supplier, InventoryItem, MRAProductMapping, PurchaseOrder,
    PurchaseOrderItem, StockTransfer, WasteRecord, StockAudit,
    StockAuditItem, InventorySnapshot, AuditLog
)
from .serializers import (
    SupplierSerializer, SupplierDetailSerializer, SupplierCreateUpdateSerializer,
    MRAProductMappingSerializer, MRAProductMappingCreateSerializer,
    MRAProductMappingBulkCreateSerializer,
    MRAProductMappingApproveSerializer, InventoryItemSerializer,
    InventoryItemDetailSerializer, InventoryItemCreateUpdateSerializer,
    InventoryItemLockSerializer, InventorySnapshotSerializer,
    PurchaseOrderSerializer, PurchaseOrderDetailSerializer,
    PurchaseOrderCreateSerializer, PurchaseOrderItemSerializer,
    StockTransferSerializer, StockTransferCreateSerializer,
    WasteRecordSerializer, WasteRecordCreateSerializer,
    StockAuditSerializer, StockAuditCreateSerializer,
    StockAuditApproveSerializer, AuditLogSerializer,
    InventoryReportSerializer
)
from .services import InventoryService, InventoryAuditService


def _get_accessible_business_ids(user):
    """
    Return business IDs the user can access (owner, staff assignment, or superuser).
    """
    if getattr(user, 'is_superuser', False):
        return list(Business.objects.values_list('id', flat=True))

    business_ids = set(
        Business.objects.filter(owner=user).values_list('id', flat=True)
    )

    try:
        from staff.models import Staff

        staff_business_ids = Staff.objects.filter(
            user=user,
            is_active=True
        ).exclude(
            business_id__isnull=True
        ).values_list('business_id', flat=True)
        business_ids.update(staff_business_ids)
    except Exception:
        # Staff module may be unavailable in some contexts; owner scope still works.
        pass

    return list(business_ids)


def _normalize_branch_lookup(branch_reference):
    """
    Normalize incoming branch references to either:
    - integer PK (e.g. "12", "BRN-12")
    - string token (e.g. "main", slug/name)
    - None (empty/unset)
    """
    if branch_reference is None:
        return None

    raw_value = str(branch_reference).strip()
    if not raw_value:
        return None

    legacy_match = re.match(r"^BRN-(\d+)$", raw_value, flags=re.IGNORECASE)
    if legacy_match:
        return int(legacy_match.group(1))

    if raw_value.isdigit():
        return int(raw_value)

    return raw_value


def _apply_branch_filter(queryset, branch_reference, field_name='branch'):
    """
    Apply safe branch filtering without raising ValueError on non-numeric IDs.
    Supports numeric IDs, legacy BRN-<id>, "main", slug, and branch name.
    """
    lookup = _normalize_branch_lookup(branch_reference)
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


def _resolve_branch_for_business_or_404(business, branch_reference):
    """
    Resolve a branch within a business using the same lookup rules as list filters.
    """
    branch_qs = Branch.objects.filter(business=business)
    lookup = _normalize_branch_lookup(branch_reference)

    if isinstance(lookup, int):
        return get_object_or_404(branch_qs, id=lookup)

    if isinstance(lookup, str):
        normalized = lookup.lower()
        if normalized in {'main', 'main-branch', 'main_branch'}:
            main_branch = branch_qs.filter(
                name__iendswith='Main Branch'
            ).order_by('created_at', 'id').first()
            if main_branch:
                return main_branch
            raise Http404("Main branch not found.")

        return get_object_or_404(
            branch_qs.filter(
                Q(slug=lookup) |
                Q(name__iexact=lookup)
            )
        )

    # Keep previous behavior for unset/invalid values (returns 404, not 500).
    return get_object_or_404(branch_qs, id=branch_reference)


def _catalog_text(value):
    return str(value or '').strip()


def _catalog_key(value):
    return _catalog_text(value).upper()


def _normalize_eis_tax_type(value):
    normalized = _catalog_text(value).lower()
    if normalized in {'zero', 'zero_rated', 'zero-rated', 'vat_zero', 'vat-zero', '0'}:
        return 'zero'
    if normalized in {'exempt', 'vat_exempt', 'vat-exempt'}:
        return 'exempt'
    return 'standard'


def _normalize_eis_tax_rate(value, tax_type):
    if value in (None, ''):
        return Decimal('0.00') if tax_type in {'zero', 'exempt'} else Decimal('16.50')
    try:
        if isinstance(value, str):
            value = value.replace('%', '').strip()
        parsed = Decimal(str(value))
        if parsed < 0:
            return Decimal('0.00')
        return parsed.quantize(Decimal('0.01'))
    except Exception:
        return Decimal('0.00') if tax_type in {'zero', 'exempt'} else Decimal('16.50')


def _normalize_eis_unit(value):
    normalized = _catalog_text(value).lower()
    aliases = {
        'units': 'unit',
        'each': 'unit',
        'ea': 'unit',
        'kilogram': 'kg',
        'kilograms': 'kg',
        'kgs': 'kg',
        'litre': 'liter',
        'litres': 'liter',
        'ltr': 'liter',
        'l': 'liter',
        'metre': 'meter',
        'metres': 'meter',
        'm': 'meter',
    }
    normalized = aliases.get(normalized, normalized)
    valid_units = {'unit', 'kg', 'liter', 'meter', 'box', 'pack', 'bottle', 'can', 'carton'}
    return normalized if normalized in valid_units else 'unit'


def _normalize_eis_calc_method(value):
    normalized = _catalog_text(value).lower()
    return 'exclusive' if normalized.startswith('excl') else 'inclusive'


def _catalog_first(item, keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ''):
            return value
    return None


def _catalog_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value in (None, ''):
        return default
    normalized = _catalog_text(value).strip().lower()
    if normalized in {'false', '0', 'no', 'n', 'service'}:
        return False
    if normalized in {'true', '1', 'yes', 'y', 'product'}:
        return True
    return default


def _normalize_eis_catalog_item(item):
    if not isinstance(item, dict):
        return None

    code = _catalog_first(item, [
        'code', 'mra_product_code', 'mraProductCode',
        'product_code', 'productCode', 'productId', 'productID',
        'item_code', 'itemCode', 'hs_code', 'hsCode',
    ])
    code = _catalog_key(code)
    if not code:
        return None

    name = _catalog_text(_catalog_first(item, [
        'name', 'mra_product_name', 'mraProductName',
        'product_name', 'productName', 'description',
        'productDescription', 'ProductDescription',
    ]) or code)

    category = _catalog_text(_catalog_first(item, [
        'category', 'product_category', 'productCategory',
        'group', 'group_name', 'groupName',
    ]) or 'General')

    tax_type = _normalize_eis_tax_type(_catalog_first(item, [
        'default_tax_type', 'defaultTaxType',
        'tax_type', 'taxType', 'vat_type', 'vatType',
        'vat_category', 'vatCategory',
    ]))
    tax_rate = _normalize_eis_tax_rate(_catalog_first(item, [
        'default_tax_rate', 'defaultTaxRate',
        'tax_rate', 'taxRate', 'vat_rate', 'vatRate',
    ]), tax_type)
    is_product = _catalog_bool(_catalog_first(item, [
        'is_product', 'isProduct', 'product', 'isGoods', 'is_good',
    ]), True)

    approved_raw = _catalog_first(item, [
        'is_approved', 'isApproved', 'approved', 'isActive',
        'active', 'approvalStatus', 'status',
    ])
    if isinstance(approved_raw, bool):
        is_approved = approved_raw
    elif approved_raw in (None, ''):
        # Terminal-site product catalogs are expected to contain approved site products.
        is_approved = True
    else:
        is_approved = _catalog_text(approved_raw).lower() in {
            'approved', 'active', 'synced', 'true', '1', 'yes',
        }

    try:
        from mra_eis.services import ProductMappingService

        mra_levies = ProductMappingService.normalize_levies(_catalog_first(item, [
            'levies',
            'activatedLevies',
            'activated_levies',
            'productLevies',
            'product_levies',
            'levyTypes',
            'levy_types',
            'levyBreakDown',
            'levyBreakdown',
        ]))
    except Exception:
        mra_levies = []

    return {
        'code': code,
        'name': name,
        'category': category,
        'tax_type': tax_type,
        'tax_rate': tax_rate,
        'unit_measure': _normalize_eis_unit(_catalog_first(item, [
            'unit', 'unitMeasure', 'unit_measure', 'mra_unit_measure',
        ])),
        'tax_calculation_method': _normalize_eis_calc_method(_catalog_first(item, [
            'taxCalculationMethod', 'tax_calculation_method', 'calculationMethod',
        ])),
        'levies': mra_levies,
        'is_product': is_product,
        'is_approved': is_approved,
        'raw': item,
    }


def _extract_eis_catalog_products(config_data):
    if not config_data:
        return []

    queue = [config_data]
    extracted = []
    seen_codes = set()

    while queue:
        current = queue.pop(0)
        if isinstance(current, list):
            queue.extend(entry for entry in current if isinstance(entry, (dict, list)))
            continue
        if not isinstance(current, dict):
            continue

        normalized = _normalize_eis_catalog_item(current)
        if normalized and normalized['code'] not in seen_codes:
            seen_codes.add(normalized['code'])
            extracted.append(normalized)

        for value in current.values():
            if isinstance(value, (dict, list)):
                queue.append(value)

    return extracted


def _get_active_eis_catalog_products(business):
    from mra_eis.services import ConfigurationService

    products = []
    config_version = None
    source = None
    for config_type in ['terminal_site_products', 'product_codes']:
        config = ConfigurationService.get_active_configuration(business, config_type)
        if not config:
            continue
        extracted = _extract_eis_catalog_products(config.config_data)
        if extracted:
            products = extracted
            config_version = config.config_version
            source = config_type
            break
    return products, config_version, source


def _inventory_match_keys(item):
    keys = set()
    for value in [item.product_code, item.barcode, item.sku]:
        key = _catalog_key(value)
        if key:
            keys.add(key)
    return keys


# ============================================================================
# SUPPLIER VIEWSET
# ============================================================================

class SupplierViewSet(viewsets.ModelViewSet):
    """
    ViewSet for supplier management.
    
    Supports:
    - List suppliers
    - Create supplier
    - Retrieve supplier details
    - Update supplier
    - Delete supplier
    - Get supplier balance
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'contact_person', 'email', 'phone', 'city', 'region', 'country', 'supplier_tin']
    ordering_fields = ['name', 'created_at', 'total_amount_due']
    ordering = ['-created_at']
    EIS_MANAGED_MESSAGE = 'Suppliers are managed by MRA EIS. Use Sync EIS Suppliers.'

    @staticmethod
    def _business_eis_enabled(business):
        try:
            settings_obj = business.settings
        except Exception:
            settings_obj = None
        return bool(getattr(settings_obj, 'enable_eis', False))

    def get_queryset(self):
        """Filter suppliers by business"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        accessible_business_ids = _get_accessible_business_ids(user)

        queryset = Supplier.objects.select_related('business')
        if not accessible_business_ids:
            return queryset.none()
        
        if business_id:
            return queryset.filter(
                business_id=business_id,
                business_id__in=accessible_business_ids
            )
        
        return queryset.filter(
            business_id__in=accessible_business_ids
        )

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'retrieve':
            return SupplierDetailSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return SupplierCreateUpdateSerializer
        return SupplierSerializer

    def perform_create(self, serializer):
        """Create supplier for business"""
        user = self.request.user
        accessible_business_ids = _get_accessible_business_ids(user)

        if not accessible_business_ids:
            raise PermissionDenied('You do not have access to any business.')

        business_id = (
            self.request.query_params.get('business_id')
            or self.request.data.get('business_id')
            or self.request.data.get('business')
        )

        if business_id:
            business = get_object_or_404(
                Business.objects.filter(id__in=accessible_business_ids),
                id=business_id
            )
        elif len(accessible_business_ids) == 1:
            business = get_object_or_404(Business, id=accessible_business_ids[0])
        else:
            raise PermissionDenied(
                'business_id is required when you have access to multiple businesses.'
            )

        if self._business_eis_enabled(business):
            raise PermissionDenied(self.EIS_MANAGED_MESSAGE)

        serializer.save(business=business)

    def perform_update(self, serializer):
        supplier = self.get_object()
        if self._business_eis_enabled(supplier.business):
            raise PermissionDenied(self.EIS_MANAGED_MESSAGE)
        serializer.save()

    def perform_destroy(self, instance):
        if self._business_eis_enabled(instance.business):
            raise PermissionDenied(self.EIS_MANAGED_MESSAGE)
        instance.delete()

    @action(detail=False, methods=['post'], url_path='sync-from-mra')
    def sync_from_mra(self, request):
        """Fetch official MRA EIS suppliers and upsert them locally."""
        user = self.request.user
        accessible_business_ids = _get_accessible_business_ids(user)
        if not accessible_business_ids:
            raise PermissionDenied('You do not have access to any business.')

        business_id = (
            request.query_params.get('business_id')
            or request.data.get('business_id')
            or request.data.get('business')
        )
        if business_id:
            business = get_object_or_404(
                Business.objects.filter(id__in=accessible_business_ids),
                id=business_id,
            )
        elif len(accessible_business_ids) == 1:
            business = get_object_or_404(Business, id=accessible_business_ids[0])
        else:
            raise PermissionDenied(
                'business_id is required when you have access to multiple businesses.'
            )

        terminal = None
        terminal_id = request.query_params.get('terminal_id') or request.data.get('terminal_id')
        if terminal_id:
            from mra_eis.models import Terminal

            terminal = get_object_or_404(Terminal, id=terminal_id, business=business)

        try:
            from mra_eis.services import MRAIntegrationError, SupplierSyncService

            result = SupplierSyncService.sync_from_mra(business=business, terminal=terminal)
            return Response(result, status=status.HTTP_200_OK)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except MRAIntegrationError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as exc:
            return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=['get'])
    def balance(self, request, pk=None):
        """Get supplier balance"""
        supplier = self.get_object()
        return Response({
            'supplier_id': str(supplier.id),
            'name': supplier.name,
            'total_amount_due': str(supplier.total_amount_due),
            'total_amount_paid': str(supplier.total_amount_paid),
            'balance_due': str(supplier.get_balance_due()),
        })

    @action(detail=True, methods=['get'])
    def purchase_orders(self, request, pk=None):
        """Get supplier's purchase orders"""
        supplier = self.get_object()
        orders = supplier.purchase_orders.all()
        serializer = PurchaseOrderSerializer(orders, many=True)
        return Response(serializer.data)


# ============================================================================
# MRA PRODUCT MAPPING VIEWSET
# ============================================================================

class MRAProductMappingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for MRA product mapping.
    
    CRITICAL for MRA compliance.
    
    Supports:
    - List mappings
    - Create mapping
    - Retrieve mapping
    - Update mapping
    - Approve mapping
    - Sync mapping
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['mra_product_code', 'mra_product_name', 'inventory_item__name']
    ordering_fields = ['mra_product_code', 'is_approved', 'mra_synced', 'created_at']
    ordering = ['-created_at']
    EIS_MANAGED_MESSAGE = (
        'MRA product mappings are read-only when EIS is enabled. '
        'Update products in the MRA EIS portal, then use sync-from-eis-catalog.'
    )

    @staticmethod
    def _business_eis_enabled(business):
        try:
            settings_obj = business.settings
        except Exception:
            settings_obj = None
        return bool(getattr(settings_obj, 'enable_eis', False))

    def _reject_manual_mapping_write_if_eis_enabled(self, business):
        if self._business_eis_enabled(business):
            raise PermissionDenied(self.EIS_MANAGED_MESSAGE)

    def _reject_mapping_write_if_eis_enabled(self, mapping):
        self._reject_manual_mapping_write_if_eis_enabled(mapping.inventory_item.business)

    def get_queryset(self):
        """Filter mappings by business and branch"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        branch_id = self.request.query_params.get('branch_id')
        inventory_item = self.request.query_params.get('inventory_item')

        accessible_business_ids = _get_accessible_business_ids(user)
        queryset = MRAProductMapping.objects.select_related('inventory_item', 'branch')

        if not accessible_business_ids:
            return queryset.none()

        if business_id:
            queryset = queryset.filter(
                inventory_item__business_id=business_id,
                inventory_item__business_id__in=accessible_business_ids,
            )
        else:
            queryset = queryset.filter(inventory_item__business_id__in=accessible_business_ids)
        
        if branch_id:
            queryset = _apply_branch_filter(queryset, branch_id)
        
        if inventory_item:
            queryset = queryset.filter(inventory_item_id=inventory_item)
        
        return queryset

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'create':
            return MRAProductMappingCreateSerializer
        elif self.action == 'approve':
            return MRAProductMappingApproveSerializer
        return MRAProductMappingSerializer

    def _create_mapping_record(self, inventory_item, mapping_data, user):
        """Create a single MRA mapping and write an audit entry."""
        mra_product_code = (mapping_data.get('mra_product_code') or '').strip()
        mra_product_name = (mapping_data.get('mra_product_name') or inventory_item.name or '').strip()

        mapping = MRAProductMapping.objects.create(
            inventory_item=inventory_item,
            branch=inventory_item.branch,
            mra_product_code=mra_product_code,
            mra_product_name=mra_product_name,
            mra_tax_type=mapping_data['mra_tax_type'],
            mra_tax_rate=mapping_data['mra_tax_rate'],
            mra_unit_measure=mapping_data['mra_unit_measure'],
            tax_calculation_method=mapping_data.get('tax_calculation_method', 'inclusive'),
            mra_levies=mapping_data.get('mra_levies') or [],
            is_product=bool(mapping_data.get('is_product', True)),
            is_approved=False,
            mra_synced=False,
        )

        AuditLog.objects.create(
            business=inventory_item.business,
            branch=inventory_item.branch,
            user=user,
            action_type='MRA_SYNC',
            entity_type='MRAProductMapping',
            entity_id=str(mapping.id),
            details={
                'inventory_item_id': str(inventory_item.id),
                'mra_product_code': mapping.mra_product_code or '',
                'mra_tax_rate': str(mapping.mra_tax_rate),
                'tax_calculation_method': mapping.tax_calculation_method,
            },
            mra_related=True,
            mra_reference=mapping.mra_product_code or '',
        )
        return mapping

    def create(self, request, *args, **kwargs):
        """Create one mapping or many mappings in a single request."""
        try:
            accessible_business_ids = _get_accessible_business_ids(request.user)
            if not accessible_business_ids:
                raise PermissionDenied('You do not have access to any business.')

            raw_data = request.data
            is_bulk_payload = (
                isinstance(raw_data, list) or
                (isinstance(raw_data, dict) and isinstance(raw_data.get('mappings'), list))
            )

            if is_bulk_payload:
                if isinstance(raw_data, list):
                    serializer = MRAProductMappingBulkCreateSerializer(data={'mappings': raw_data})
                    serializer.is_valid(raise_exception=True)
                    mappings_payload = serializer.validated_data['mappings']
                else:
                    serializer = MRAProductMappingBulkCreateSerializer(data=raw_data)
                    serializer.is_valid(raise_exception=True)
                    mappings_payload = serializer.validated_data['mappings']

                inventory_item_ids = [str(entry['inventory_item_id']) for entry in mappings_payload]

                inventory_items = InventoryItem.objects.select_related('business', 'branch').filter(
                    id__in=inventory_item_ids,
                    business_id__in=accessible_business_ids,
                )
                inventory_items_by_id = {str(item.id): item for item in inventory_items}

                missing_inventory_ids = sorted(
                    set(item_id for item_id in inventory_item_ids if item_id not in inventory_items_by_id)
                )
                if missing_inventory_ids:
                    return Response(
                        {
                            'error': 'Some inventory items do not exist or are not accessible.',
                            'inventory_item_ids': missing_inventory_ids,
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                eis_managed_inventory_ids = sorted(
                    str(item.id)
                    for item in inventory_items_by_id.values()
                    if self._business_eis_enabled(item.business)
                )
                if eis_managed_inventory_ids:
                    raise PermissionDenied(self.EIS_MANAGED_MESSAGE)

                existing_mapping_ids = sorted(set(
                    str(item_id)
                    for item_id in MRAProductMapping.objects.filter(
                        inventory_item_id__in=inventory_item_ids
                    ).values_list('inventory_item_id', flat=True)
                ))
                if existing_mapping_ids:
                    return Response(
                        {
                            'error': 'Some inventory items already have MRA mappings.',
                            'inventory_item_ids': existing_mapping_ids,
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                created_mappings = []
                with transaction.atomic():
                    for mapping_data in mappings_payload:
                        inventory_item = inventory_items_by_id[str(mapping_data['inventory_item_id'])]
                        created_mappings.append(
                            self._create_mapping_record(
                                inventory_item=inventory_item,
                                mapping_data=mapping_data,
                                user=request.user,
                            )
                        )

                return Response(
                    {
                        'count': len(created_mappings),
                        'results': MRAProductMappingSerializer(created_mappings, many=True).data,
                    },
                    status=status.HTTP_201_CREATED
                )

            serializer = self.get_serializer(data=raw_data)
            serializer.is_valid(raise_exception=True)

            inventory_item = InventoryItem.objects.select_related('business', 'branch').filter(
                id=serializer.validated_data['inventory_item_id'],
                business_id__in=accessible_business_ids,
            ).first()
            if not inventory_item:
                return Response(
                    {'error': 'Inventory item not found or not accessible.'},
                    status=status.HTTP_404_NOT_FOUND
                )

            self._reject_manual_mapping_write_if_eis_enabled(inventory_item.business)

            if MRAProductMapping.objects.filter(inventory_item=inventory_item).exists():
                return Response(
                    {'error': 'This inventory item already has an MRA mapping.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            mapping = self._create_mapping_record(
                inventory_item=inventory_item,
                mapping_data=serializer.validated_data,
                user=request.user,
            )

            return Response(MRAProductMappingSerializer(mapping).data, status=status.HTTP_201_CREATED)
        except Http404:
            return Response(
                {'error': 'Inventory item not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except PermissionDenied:
            raise
        except ValidationError:
            raise
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    def update(self, request, *args, **kwargs):
        mapping = self.get_object()
        self._reject_mapping_write_if_eis_enabled(mapping)
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        mapping = self.get_object()
        self._reject_mapping_write_if_eis_enabled(mapping)
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        mapping = self.get_object()
        self._reject_mapping_write_if_eis_enabled(mapping)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve MRA product mapping"""
        mapping = self.get_object()
        self._reject_mapping_write_if_eis_enabled(mapping)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        mapping.is_approved = serializer.validated_data['is_approved']
        mapping.mra_synced = serializer.validated_data.get('mra_synced', False)
        
        if mapping.is_approved:
            mapping.approved_at = timezone.now()
        
        mapping.save()
        
        # Log to audit
        AuditLog.objects.create(
            business=mapping.inventory_item.business,
            branch=mapping.inventory_item.branch,
            user=request.user,
            action_type='MRA_SYNC',
            entity_type='MRAProductMapping',
            entity_id=str(mapping.id),
            details={
                'is_approved': mapping.is_approved,
                'mra_synced': mapping.mra_synced,
            },
            mra_related=True,
        )
        
        return Response(
            MRAProductMappingSerializer(mapping).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """
        Sync approved mapping to MRA utilities endpoint.

        In backend dry-run mode this marks mapping as synced/prepared locally
        without sending live data to MRA.
        """
        mapping = self.get_object()
        self._reject_mapping_write_if_eis_enabled(mapping)

        if not mapping.is_approved:
            return Response(
                {'error': 'Only approved mappings can be synced to MRA.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            from mra_eis.models import Terminal
            from mra_eis.services import ProductMappingService

            branch = mapping.branch or mapping.inventory_item.branch
            terminal = (
                Terminal.objects.filter(
                    business=mapping.inventory_item.business,
                    branch=branch,
                )
                .order_by('-updated_at')
                .first()
            )

            sync_result = ProductMappingService.sync_inventory_mapping_to_mra(
                inventory_mapping=mapping,
                terminal=terminal,
            )

            AuditLog.objects.create(
                business=mapping.inventory_item.business,
                branch=branch,
                user=request.user,
                action_type='MRA_SYNC',
                entity_type='MRAProductMapping',
                entity_id=str(mapping.id),
                details={
                    'action': 'sync',
                    'dry_run': sync_result.get('dry_run', True),
                    'endpoint': sync_result.get('endpoint'),
                },
                mra_related=True,
                mra_reference=mapping.mra_product_code,
            )

            return Response(
                {
                    'message': 'Mapping synced/prepared successfully.',
                    **sync_result,
                },
                status=status.HTTP_200_OK
            )
        except Exception as exc:
            return Response(
                {'error': str(exc)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=False, methods=['post'], url_path='sync-from-eis-catalog')
    def sync_from_eis_catalog(self, request):
        """
        Replace/seed local mappings from the approved MRA terminal-site catalog.

        This is the manageable path for live EIS: products are registered and
        approved in the MRA portal, then pulled down and matched to local items
        by barcode/product code/SKU first, then exact product name.
        """
        accessible_business_ids = _get_accessible_business_ids(request.user)
        if not accessible_business_ids:
            raise PermissionDenied('You do not have access to any business.')

        business_id = (
            request.query_params.get('business_id')
            or request.data.get('business_id')
            or request.data.get('business')
        )
        if business_id:
            business = get_object_or_404(
                Business.objects.filter(id__in=accessible_business_ids),
                id=business_id,
            )
        elif len(accessible_business_ids) == 1:
            business = get_object_or_404(Business, id=accessible_business_ids[0])
        else:
            return Response(
                {'error': 'business_id is required when you have access to multiple businesses.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        branch_reference = (
            request.query_params.get('branch_id')
            or request.data.get('branch_id')
            or request.data.get('branch')
        )
        branch = _resolve_branch_for_business_or_404(business, branch_reference) if branch_reference else None

        refresh_catalog_raw = request.data.get('refresh_catalog', True)
        refresh_catalog = str(refresh_catalog_raw).strip().lower() not in {'0', 'false', 'no', 'off'}
        refresh_error = None
        product_sync = None

        if refresh_catalog:
            try:
                from mra_eis.models import Terminal
                from mra_eis.services import ProductMappingService

                terminal_qs = Terminal.objects.filter(business=business)
                if branch:
                    terminal_qs = terminal_qs.filter(branch=branch)
                terminal = terminal_qs.order_by('-updated_at').first()

                if terminal:
                    product_sync = ProductMappingService.sync_terminal_site_products(
                        business=business,
                        terminal=terminal,
                    )
                else:
                    refresh_error = 'No MRA terminal found for this branch/business.'
            except Exception as exc:
                refresh_error = str(exc)

        catalog_products, config_version, catalog_source = _get_active_eis_catalog_products(business)
        if not catalog_products:
            message = (
                'No approved EIS product catalog is available yet. '
                'After MRA approves portal mappings, sync configurations/products again.'
            )
            if refresh_error:
                message = f'{message} Last refresh error: {refresh_error}'
            return Response(
                {
                    'error': message,
                    'created': 0,
                    'updated': 0,
                    'matched': 0,
                    'unmatched': [],
                    'product_sync': product_sync,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        inventory_qs = InventoryItem.objects.filter(business=business).select_related('branch')
        if branch:
            inventory_qs = inventory_qs.filter(branch=branch)
        inventory_items = list(inventory_qs)

        code_index = {}
        ambiguous_codes = set()
        name_index = {}
        ambiguous_names = set()

        for item in inventory_items:
            for key in _inventory_match_keys(item):
                if key in code_index and code_index[key].id != item.id:
                    ambiguous_codes.add(key)
                else:
                    code_index[key] = item

            name_key = _catalog_text(item.name).lower()
            if name_key:
                if name_key in name_index and name_index[name_key].id != item.id:
                    ambiguous_names.add(name_key)
                else:
                    name_index[name_key] = item

        created_count = 0
        updated_count = 0
        matched_count = 0
        unmatched = []
        taxpayer_incompatible = []
        now = timezone.now()

        from mra_eis.services import ProductMappingService as EISProductMappingService

        with transaction.atomic():
            for product in catalog_products:
                match = None
                match_reason = ''
                product_code = product['code']
                product_name = product['name']

                if product_code in ambiguous_codes:
                    unmatched.append({
                        'code': product_code,
                        'name': product_name,
                        'reason': 'ambiguous local product code/barcode/SKU',
                    })
                    continue

                match = code_index.get(product_code)
                if match:
                    match_reason = 'code'
                else:
                    name_key = _catalog_text(product_name).lower()
                    if name_key in ambiguous_names:
                        unmatched.append({
                            'code': product_code,
                            'name': product_name,
                            'reason': 'ambiguous local product name',
                        })
                        continue
                    match = name_index.get(name_key)
                    match_reason = 'name' if match else ''

                if not match:
                    unmatched.append({
                        'code': product_code,
                        'name': product_name,
                        'reason': 'no local product matched by code/barcode/SKU/name',
                    })
                    continue

                is_approved = bool(product['is_approved'])
                tax_type, tax_rate, tax_method, tax_adjusted_for_non_vat = (
                    EISProductMappingService.normalize_tax_for_taxpayer(
                        business,
                        product['tax_type'],
                        product['tax_rate'],
                        product['tax_calculation_method'],
                    )
                )
                mapping, created = MRAProductMapping.objects.update_or_create(
                    inventory_item=match,
                    defaults={
                        'branch': match.branch,
                        'mra_product_code': product_code,
                        'mra_product_name': product_name,
                        'mra_tax_type': tax_type,
                        'mra_tax_rate': tax_rate,
                        'mra_unit_measure': product['unit_measure'],
                        'tax_calculation_method': tax_method,
                        'mra_levies': product.get('levies') or [],
                        'is_product': bool(product.get('is_product', True)),
                        'is_approved': is_approved,
                        'mra_synced': is_approved,
                        'last_synced_at': now if is_approved else None,
                    },
                )

                fields_to_update = []
                if is_approved and not mapping.approved_at:
                    mapping.approved_at = now
                    fields_to_update.append('approved_at')
                elif not is_approved and mapping.approved_at:
                    mapping.approved_at = None
                    fields_to_update.append('approved_at')
                if fields_to_update:
                    mapping.save(update_fields=fields_to_update)

                compatibility_error = mapping.taxpayer_compatibility_error()
                is_taxpayer_compatible = not bool(compatibility_error)
                if compatibility_error:
                    taxpayer_incompatible.append({
                        'inventory_item_id': str(match.id),
                        'name': match.name,
                        'mra_product_code': mapping.mra_product_code,
                        'mra_product_name': mapping.mra_product_name,
                        'mra_tax_type': mapping.mra_tax_type,
                        'mra_tax_rate': str(mapping.mra_tax_rate),
                        'error': compatibility_error,
                    })

                matched_count += 1
                if created:
                    created_count += 1
                else:
                    updated_count += 1

                AuditLog.objects.create(
                    business=business,
                    branch=match.branch,
                    user=request.user,
                    action_type='MRA_SYNC',
                    entity_type='MRAProductMapping',
                    entity_id=str(mapping.id),
                    details={
                        'action': 'sync_from_eis_catalog',
                        'match_reason': match_reason,
                        'mra_product_code': product_code,
                        'mra_tax_type': tax_type,
                        'mra_tax_rate': str(tax_rate),
                        'tax_adjusted_for_non_vat': tax_adjusted_for_non_vat,
                        'is_taxpayer_compatible': is_taxpayer_compatible,
                        'taxpayer_compatibility_error': compatibility_error,
                        'catalog_source': catalog_source,
                        'catalog_version': config_version,
                    },
                    mra_related=True,
                    mra_reference=product_code,
                )

        return Response(
            {
                'message': 'EIS catalog sync completed.',
                'created': created_count,
                'updated': updated_count,
                'matched': matched_count,
                'unmatched': unmatched[:50],
                'unmatched_count': len(unmatched),
                'taxpayer_incompatible_count': len(taxpayer_incompatible),
                'taxpayer_incompatible': taxpayer_incompatible[:50],
                'catalog_source': catalog_source,
                'catalog_version': config_version,
                'product_sync': product_sync,
                'refresh_error': refresh_error,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['get'])
    def unapproved(self, request):
        """Get unapproved mappings"""
        mappings = self.get_queryset().filter(is_approved=False)
        serializer = self.get_serializer(mappings, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def unsynced(self, request):
        """Get unsynced mappings"""
        mappings = self.get_queryset().filter(mra_synced=False, is_approved=True)
        serializer = self.get_serializer(mappings, many=True)
        return Response(serializer.data)


# ============================================================================
# INVENTORY ITEM VIEWSET
# ============================================================================

class InventoryItemViewSet(viewsets.ModelViewSet):
    """
    ViewSet for inventory items.
    
    Supports:
    - List items
    - Create item
    - Retrieve item
    - Update item
    - Delete item
    - Lock price/tax
    - Get traceability
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'category', 'sku', 'barcode']
    ordering_fields = ['name', 'stock_units', 'price', 'status', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter items by business and branch"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        branch_id = self.request.query_params.get('branch_id')

        accessible_business_ids = _get_accessible_business_ids(user)
        if not accessible_business_ids:
            return InventoryItem.objects.none().select_related('business', 'branch', 'mra_mapping')
        
        queryset = InventoryItem.objects.filter(
            business_id__in=accessible_business_ids
        ).select_related('business', 'branch', 'mra_mapping')
        
        if business_id:
            queryset = queryset.filter(business_id=business_id)
        
        # Filter by branch if provided
        if branch_id:
            queryset = _apply_branch_filter(queryset, branch_id)
        
        return queryset

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'retrieve':
            return InventoryItemDetailSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return InventoryItemCreateUpdateSerializer
        elif self.action == 'lock':
            return InventoryItemLockSerializer
        return InventoryItemSerializer

    def perform_create(self, serializer):
        """Create inventory item"""
        business_id = self.request.query_params.get('business_id')
        branch_reference = (
            self.request.query_params.get('branch_id')
            or self.request.data.get('branch_id')
            or self.request.data.get('branch')
        )
        
        business = get_object_or_404(Business, id=business_id)
        branch = _resolve_branch_for_business_or_404(business, branch_reference)
        
        item = serializer.save(business=business, branch=branch)
        
        # Log to audit
        AuditLog.objects.create(
            business=business,
            branch=branch,
            user=self.request.user,
            action_type='INVENTORY_UPDATE',
            entity_type='InventoryItem',
            entity_id=str(item.id),
            details={'action': 'created', 'name': item.name},
        )

    def perform_update(self, serializer):
        """Update inventory item"""
        item = serializer.save()
        
        # Log to audit
        AuditLog.objects.create(
            business=item.business,
            branch=item.branch,
            user=self.request.user,
            action_type='INVENTORY_UPDATE',
            entity_type='InventoryItem',
            entity_id=str(item.id),
            details={'action': 'updated', 'name': item.name},
        )

    @action(detail=True, methods=['post'])
    def lock(self, request, pk=None):
        """Lock price and/or tax"""
        item = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        if 'price_locked' in serializer.validated_data:
            item.price_locked = serializer.validated_data['price_locked']
        
        if 'tax_locked' in serializer.validated_data:
            item.tax_locked = serializer.validated_data['tax_locked']
        
        item.save()
        
        # Log to audit
        AuditLog.objects.create(
            business=item.business,
            branch=item.branch,
            user=request.user,
            action_type='INVENTORY_UPDATE',
            entity_type='InventoryItem',
            entity_id=str(item.id),
            details={
                'action': 'locked',
                'price_locked': item.price_locked,
                'tax_locked': item.tax_locked,
            },
        )
        
        return Response(
            InventoryItemDetailSerializer(item).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['get'])
    def traceability(self, request, pk=None):
        """Get product traceability"""
        item = self.get_object()
        traceability = InventoryService.get_product_traceability(item)
        
        return Response({
            'product_id': str(item.id),
            'product_name': item.name,
            'snapshots_count': traceability['snapshots'].count(),
            'waste_count': traceability['waste'].count(),
            'transfers_count': traceability['transfers'].count(),
            'snapshots': InventorySnapshotSerializer(
                traceability['snapshots'], many=True
            ).data,
            'waste': WasteRecordSerializer(
                traceability['waste'], many=True
            ).data,
            'transfers': StockTransferSerializer(
                traceability['transfers'], many=True
            ).data,
        })

    @action(detail=False, methods=['get'])
    def low_stock(self, request):
        """Get low stock items"""
        items = self.get_queryset().filter(status='Low Stock')
        serializer = self.get_serializer(items, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def out_of_stock(self, request):
        """Get out of stock items"""
        items = self.get_queryset().filter(status='Out of Stock')
        serializer = self.get_serializer(items, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def mra_ready(self, request):
        """Get MRA-ready items"""
        items = self.get_queryset().filter(
            item_type='sellable',
            mra_mapping__is_approved=True,
            mra_mapping__mra_synced=True,
        )
        serializer = self.get_serializer(items, many=True)
        return Response(serializer.data)


# ============================================================================
# INVENTORY SNAPSHOT VIEWSET
# ============================================================================

class InventorySnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for inventory snapshots (read-only).
    
    CRITICAL for MRA audit trail.
    
    Supports:
    - List snapshots
    - Retrieve snapshot
    - Filter by invoice
    - Filter by product
    """
    permission_classes = [IsAuthenticated]
    serializer_class = InventorySnapshotSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['related_invoice_number', 'inventory_item__name']
    ordering_fields = ['created_at', 'related_invoice_number']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter snapshots by business"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        invoice_number = self.request.query_params.get('invoice_number')
        
        queryset = InventorySnapshot.objects.filter(
            inventory_item__business__owner=user
        ).select_related('inventory_item', 'branch')
        
        if business_id:
            queryset = queryset.filter(inventory_item__business_id=business_id)
        
        if invoice_number:
            queryset = queryset.filter(related_invoice_number=invoice_number)
        
        return queryset

    @action(detail=False, methods=['get'])
    def by_invoice(self, request):
        """Get snapshots by invoice number"""
        invoice_number = request.query_params.get('invoice_number')
        
        if not invoice_number:
            return Response(
                {'error': 'invoice_number parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        snapshots = self.get_queryset().filter(
            related_invoice_number=invoice_number
        )
        serializer = self.get_serializer(snapshots, many=True)
        return Response(serializer.data)


# ============================================================================
# PURCHASE ORDER VIEWSET
# ============================================================================

class PurchaseOrderViewSet(viewsets.ModelViewSet):
    """
    ViewSet for purchase orders.
    
    Supports:
    - List purchase orders
    - Create purchase order
    - Retrieve purchase order
    - Update purchase order
    - Receive purchase order
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['order_number', 'supplier__name']
    ordering_fields = ['created_at', 'status', 'total_cost']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter purchase orders by business"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        branch_id = self.request.query_params.get('branch_id')
        supplier_id = self.request.query_params.get('supplier_id')
        
        queryset = PurchaseOrder.objects.filter(
            business__owner=user
        ).select_related('business', 'branch', 'supplier')
        
        if business_id:
            queryset = queryset.filter(business_id=business_id)
        
        if branch_id:
            queryset = _apply_branch_filter(queryset, branch_id)
        
        if supplier_id:
            queryset = queryset.filter(supplier_id=supplier_id)
        
        return queryset

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'create':
            return PurchaseOrderCreateSerializer
        elif self.action == 'retrieve':
            return PurchaseOrderDetailSerializer
        return PurchaseOrderSerializer

    def _apply_supplier_compliance_defaults(self, po):
        """
        Backfill supplier compliance fields when a supplier is selected and
        explicit values were not provided by the client payload.
        """
        supplier = po.supplier
        if not supplier:
            return

        request_data = getattr(self.request, 'data', {}) or {}
        tin_explicitly_provided = any(key in request_data for key in ['supplier_tin', 'supplierTin'])
        mra_supplier_id_explicitly_provided = any(
            key in request_data for key in ['mra_supplier_id', 'mraSupplierId']
        )
        vat_explicitly_provided = any(
            key in request_data for key in ['supplier_vat_registered', 'supplierVatRegistered']
        )

        fields_to_update = []

        if not tin_explicitly_provided:
            supplier_tin = (supplier.supplier_tin or '').strip()
            if supplier_tin and (not po.supplier_tin or not str(po.supplier_tin).strip()):
                po.supplier_tin = supplier_tin
                fields_to_update.append('supplier_tin')

        if not mra_supplier_id_explicitly_provided and supplier.mra_supplier_id and not po.mra_supplier_id:
            po.mra_supplier_id = supplier.mra_supplier_id
            fields_to_update.append('mra_supplier_id')

        if not vat_explicitly_provided and po.supplier_vat_registered != supplier.vat_registered:
            po.supplier_vat_registered = supplier.vat_registered
            fields_to_update.append('supplier_vat_registered')

        if fields_to_update:
            po.save(update_fields=fields_to_update)

    def perform_create(self, serializer):
        """Create purchase order"""
        business_id = self.request.query_params.get('business_id')
        branch_reference = (
            self.request.query_params.get('branch_id')
            or self.request.data.get('branch_id')
            or self.request.data.get('branch')
        )
        
        business = get_object_or_404(Business, id=business_id)
        branch = _resolve_branch_for_business_or_404(business, branch_reference)
        
        po = serializer.save(business=business, branch=branch)
        self._apply_supplier_compliance_defaults(po)
        
        # Log to audit
        AuditLog.objects.create(
            business=business,
            branch=branch,
            user=self.request.user,
            action_type='PURCHASE_ORDER',
            entity_type='PurchaseOrder',
            entity_id=str(po.id),
            details={'action': 'created', 'po_number': str(po.order_number)},
        )

    def perform_update(self, serializer):
        """Update purchase order"""
        po = serializer.save()
        self._apply_supplier_compliance_defaults(po)

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """Receive purchase order"""
        po = self.get_object()
        
        if po.status == 'Received':
            return Response(
                {'error': 'Purchase order already received'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        po.status = 'Received'
        update_fields = ['status']
        if not po.received_date:
            po.received_date = timezone.now()
            update_fields.append('received_date')
        po.save(update_fields=update_fields)
        
        # Log to audit
        AuditLog.objects.create(
            business=po.business,
            branch=po.branch,
            user=request.user,
            action_type='PURCHASE_ORDER',
            entity_type='PurchaseOrder',
            entity_id=str(po.id),
            details={'action': 'received', 'po_number': str(po.order_number)},
        )
        
        return Response(
            PurchaseOrderDetailSerializer(po).data,
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get pending purchase orders"""
        orders = self.get_queryset().filter(status='Pending')
        serializer = self.get_serializer(orders, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def received(self, request):
        """Get received purchase orders"""
        orders = self.get_queryset().filter(status='Received')
        serializer = self.get_serializer(orders, many=True)
        return Response(serializer.data)


# ============================================================================
# WASTE RECORD VIEWSET
# ============================================================================

class WasteRecordViewSet(viewsets.ModelViewSet):
    """
    ViewSet for waste records.
    
    Supports:
    - List waste records
    - Create waste record
    - Retrieve waste record
    - Approve waste record
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['inventory_item__name', 'reason']
    ordering_fields = ['recorded_at', 'reason', 'quantity']
    ordering = ['-recorded_at']

    def get_queryset(self):
        """Filter waste records by business"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        branch_id = self.request.query_params.get('branch_id')
        
        queryset = WasteRecord.objects.filter(
            business__owner=user
        ).select_related('business', 'branch', 'inventory_item')
        
        if business_id:
            queryset = queryset.filter(business_id=business_id)
        
        if branch_id:
            queryset = _apply_branch_filter(queryset, branch_id)
        
        return queryset

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'create':
            return WasteRecordCreateSerializer
        return WasteRecordSerializer

    def create(self, request, *args, **kwargs):
        """Create waste record"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            inventory_item = InventoryItem.objects.get(
                id=serializer.validated_data['inventory_item_id']
            )
            
            waste = InventoryService.record_waste(
                inventory_item=inventory_item,
                quantity=serializer.validated_data['quantity'],
                reason=serializer.validated_data['reason'],
                cost=serializer.validated_data['cost'],
                notes=serializer.validated_data.get('notes', ''),
                approved_by=serializer.validated_data.get('approved_by'),
                user=request.user,
            )
            
            return Response(
                WasteRecordSerializer(waste).data,
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=False, methods=['get'])
    def by_reason(self, request):
        """Get waste records by reason"""
        reason = request.query_params.get('reason')
        
        if not reason:
            return Response(
                {'error': 'reason parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        waste = self.get_queryset().filter(reason=reason)
        serializer = self.get_serializer(waste, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def unapproved(self, request):
        """Get unapproved waste records"""
        waste = self.get_queryset().filter(approved_by='')
        serializer = self.get_serializer(waste, many=True)
        return Response(serializer.data)


# ============================================================================
# STOCK TRANSFER VIEWSET
# ============================================================================

class StockTransferViewSet(viewsets.ModelViewSet):
    """
    ViewSet for stock transfers.
    
    Supports:
    - List transfers
    - Create transfer
    - Retrieve transfer
    - Mark as notified
    """
    permission_classes = [IsAuthenticated]
    serializer_class = StockTransferSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['transfer_reference', 'inventory_item__name']
    ordering_fields = ['created_at', 'transfer_reference']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter transfers by business"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        
        queryset = StockTransfer.objects.filter(
            business__owner=user
        ).select_related('business', 'from_branch', 'to_branch', 'inventory_item')
        
        if business_id:
            queryset = queryset.filter(business_id=business_id)
        
        return queryset

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'create':
            return StockTransferCreateSerializer
        return StockTransferSerializer

    def create(self, request, *args, **kwargs):
        """Create stock transfer"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            from_branch = Branch.objects.get(id=serializer.validated_data['from_branch_id'])
            to_branch = Branch.objects.get(id=serializer.validated_data['to_branch_id'])
            inventory_item = InventoryItem.objects.get(
                id=serializer.validated_data['inventory_item_id']
            )
            
            transfer_reference = f"TRF-{uuid.uuid4().hex[:8].upper()}"
            
            transfer = InventoryService.transfer_stock(
                from_branch=from_branch,
                to_branch=to_branch,
                inventory_item=inventory_item,
                quantity=serializer.validated_data['quantity'],
                transfer_reference=transfer_reference,
                user=request.user,
            )
            
            return Response(
                StockTransferSerializer(transfer).data,
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def mark_notified(self, request, pk=None):
        """Mark transfer as notified to MRA"""
        transfer = self.get_object()
        transfer.mra_notified = True
        transfer.save()
        
        return Response(
            StockTransferSerializer(transfer).data,
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=['get'])
    def unnotified(self, request):
        """Get unnotified transfers"""
        transfers = self.get_queryset().filter(mra_notified=False)
        serializer = self.get_serializer(transfers, many=True)
        return Response(serializer.data)


# ============================================================================
# AUDIT LOG VIEWSET
# ============================================================================

class StockAuditViewSet(viewsets.ModelViewSet):
    """
    ViewSet for stock audits.
    
    Supports:
    - List audits
    - Create audit
    - Retrieve audit
    - Approve audit
    - Reject audit
    """
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['branch__name', 'status']
    ordering_fields = ['created_at', 'status', 'total_discrepancy_value']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter audits by business"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        branch_id = self.request.query_params.get('branch_id')
        
        queryset = StockAudit.objects.filter(
            branch__business__owner=user
        ).select_related('branch')
        
        if business_id:
            queryset = queryset.filter(branch__business_id=business_id)
        
        if branch_id:
            queryset = _apply_branch_filter(queryset, branch_id)
        
        return queryset

    def get_serializer_class(self):
        """Choose serializer based on action"""
        if self.action == 'create':
            return StockAuditCreateSerializer
        elif self.action in ['approve', 'reject']:
            return StockAuditApproveSerializer
        return StockAuditSerializer

    def create(self, request, *args, **kwargs):
        """Create stock audit"""
        print(f"[StockAudit.create] Request data: {request.data}")
        print(f"[StockAudit.create] Request user: {request.user}")
        
        serializer = self.get_serializer(data=request.data)
        print(f"[StockAudit.create] Serializer: {serializer}")
        
        is_valid = serializer.is_valid()
        print(f"[StockAudit.create] Is valid: {is_valid}")
        print(f"[StockAudit.create] Errors: {serializer.errors}")
        print(f"[StockAudit.create] Validated data: {serializer.validated_data if is_valid else 'N/A'}")
        
        if not is_valid:
            print(f"[StockAudit.create] Returning validation errors")
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            branch_id = serializer.validated_data['branch_id']
            print(f"[StockAudit.create] Branch ID: {branch_id}")
            
            branch = Branch.objects.get(id=branch_id)
            print(f"[StockAudit.create] Branch found: {branch}")
            
            audit = StockAudit.objects.create(
                business=branch.business,
                branch=branch,
                status='Pending',
                created_by=request.user.email,
                notes=serializer.validated_data.get('notes', ''),
                mra_visible=True,
                inventory_locked=False,
            )
            print(f"[StockAudit.create] Audit created: {audit.id}")
            
            # Log to audit
            AuditLog.objects.create(
                business=branch.business,
                branch=branch,
                user=request.user,
                action_type='STOCK_AUDIT',
                entity_type='StockAudit',
                entity_id=str(audit.id),
                details={'action': 'created', 'status': 'Pending'},
                mra_related=True,
            )
            print(f"[StockAudit.create] Audit log created")
            
            response_data = StockAuditSerializer(audit).data
            print(f"[StockAudit.create] Response data: {response_data}")
            
            return Response(
                response_data,
                status=status.HTTP_201_CREATED
            )
        except Branch.DoesNotExist as e:
            print(f"[StockAudit.create] Branch not found: {e}")
            return Response(
                {'error': 'Branch not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            print(f"[StockAudit.create] Exception: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve stock audit"""
        audit = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        audit.status = 'Approved'
        audit.approval_role = serializer.validated_data.get('approval_role', 'Manager')
        audit.approved_by = request.user.email
        audit.approved_at = timezone.now()
        audit.inventory_locked = True
        audit.save()
        
        # Log to audit
        AuditLog.objects.create(
            business=audit.business,
            branch=audit.branch,
            user=request.user,
            action_type='STOCK_AUDIT',
            entity_type='StockAudit',
            entity_id=str(audit.id),
            details={
                'action': 'approved',
                'approval_role': audit.approval_role,
                'inventory_locked': True,
            },
            mra_related=True,
        )
        
        return Response(
            StockAuditSerializer(audit).data,
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Reject stock audit"""
        audit = self.get_object()
        
        audit.status = 'Rejected'
        audit.approved_by = request.user.email
        audit.approved_at = timezone.now()
        audit.save()
        
        # Log to audit
        AuditLog.objects.create(
            business=audit.business,
            branch=audit.branch,
            user=request.user,
            action_type='STOCK_AUDIT',
            entity_type='StockAudit',
            entity_id=str(audit.id),
            details={'action': 'rejected'},
            mra_related=True,
        )
        
        return Response(
            StockAuditSerializer(audit).data,
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get pending audits"""
        audits = self.get_queryset().filter(status='Pending')
        serializer = self.get_serializer(audits, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def approved(self, request):
        """Get approved audits"""
        audits = self.get_queryset().filter(status='Approved')
        serializer = self.get_serializer(audits, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def locked(self, request):
        """Get audits with locked inventory"""
        audits = self.get_queryset().filter(inventory_locked=True)
        serializer = self.get_serializer(audits, many=True)
        return Response(serializer.data)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for audit logs (read-only).
    
    Supports:
    - List audit logs
    - Retrieve audit log
    - Filter by entity
    - Filter by action
    - Filter by MRA reference
    """
    permission_classes = [IsAuthenticated]
    serializer_class = AuditLogSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['entity_id', 'mra_reference']
    ordering_fields = ['created_at', 'action_type']
    ordering = ['-created_at']

    def get_queryset(self):
        """Filter audit logs by business"""
        user = self.request.user
        business_id = self.request.query_params.get('business_id')
        entity_type = self.request.query_params.get('entity_type')
        action_type = self.request.query_params.get('action_type')
        mra_related = self.request.query_params.get('mra_related')
        
        queryset = AuditLog.objects.filter(
            business__owner=user
        ).select_related('user')
        
        if business_id:
            queryset = queryset.filter(business_id=business_id)
        
        if entity_type:
            queryset = queryset.filter(entity_type=entity_type)
        
        if action_type:
            queryset = queryset.filter(action_type=action_type)
        
        if mra_related:
            queryset = queryset.filter(mra_related=mra_related.lower() == 'true')
        
        return queryset

    @action(detail=False, methods=['get'])
    def by_entity(self, request):
        """Get audit logs by entity"""
        entity_id = request.query_params.get('entity_id')
        
        if not entity_id:
            return Response(
                {'error': 'entity_id parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        logs = self.get_queryset().filter(entity_id=entity_id)
        serializer = self.get_serializer(logs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def mra_related(self, request):
        """Get MRA-related audit logs"""
        logs = self.get_queryset().filter(mra_related=True)
        serializer = self.get_serializer(logs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def by_invoice(self, request):
        """Get audit logs by invoice"""
        invoice_number = request.query_params.get('invoice_number')
        
        if not invoice_number:
            return Response(
                {'error': 'invoice_number parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        logs = self.get_queryset().filter(mra_reference=invoice_number)
        serializer = self.get_serializer(logs, many=True)
        return Response(serializer.data)
