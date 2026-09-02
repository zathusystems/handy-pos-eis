"""
Main Sync Views - Orchestrates synchronization between frontend and backend
Imports handlers from specialized sync view modules for each entity type
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from datetime import datetime
from django.db import transaction
from django.db.models import Q

from .models import Supplier, InventoryItem, PurchaseOrder
from .serializers import InventoryItemSerializer, PurchaseOrderSerializer
from business.models import Business

# Import handlers from specialized sync modules
from .sync_views_inventory import (
    handle_create_inventory_item,
    handle_update_inventory_item,
    handle_delete_inventory_item,
)
from .sync_views_suppliers import (
    handle_create_supplier,
    handle_update_supplier,
    handle_delete_supplier,
)
from .sync_views_purchases import (
    handle_create_purchase_order,
    handle_update_purchase_order,
    handle_delete_purchase_order,
)
from .sync_views_stock import (
    handle_create_stock_transfer,
    handle_update_stock_transfer,
    handle_delete_stock_transfer,
)
from .sync_views_waste import (
    handle_create_waste_record,
    handle_update_waste_record,
    handle_delete_waste_record,
)
from .sync_views_purchase_records import (
    handle_create_purchase_record,
    handle_update_purchase_record,
    handle_delete_purchase_record,
)


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
                "id": "item-uuid",
                "entity_type": "InventoryItem",
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
        
        print(f"[Sync Push] Received {len(changes)} changes from frontend")
        print(f"[Sync Push] Last synced: {last_synced_at}, Branch: {branch_id}")
        
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
        
        for change in changes:
            try:
                entity_type = change.get('entity_type')
                operation = change.get('op')
                data = change.get('data')
                entity_id = change.get('id')
                
                print(f"[Sync Push] Processing {operation} for {entity_type} {entity_id}")
                
                # Wrap each change in its own transaction to prevent one failure from blocking others
                with transaction.atomic():
                    result = None
                    
                    # Route to appropriate handler based on entity type
                    if entity_type == 'InventoryItem':
                        if operation == 'create':
                            result = handle_create_inventory_item(entity_id, data, business, branch_id)
                        elif operation == 'update':
                            result = handle_update_inventory_item(entity_id, data, business, branch_id)
                        elif operation == 'delete':
                            result = handle_delete_inventory_item(entity_id, business, branch_id)
                    
                    elif entity_type == 'PurchaseOrder':
                        if operation == 'create':
                            result = handle_create_purchase_order(entity_id, data, business, branch_id)
                        elif operation == 'update':
                            result = handle_update_purchase_order(entity_id, data, business, branch_id)
                        elif operation == 'delete':
                            result = handle_delete_purchase_order(entity_id, business, branch_id, data)
                    
                    elif entity_type == 'StockTransfer':
                        if operation == 'create':
                            result = handle_create_stock_transfer(entity_id, data, business, branch_id)
                        elif operation == 'update':
                            result = handle_update_stock_transfer(entity_id, data, business, branch_id)
                        elif operation == 'delete':
                            result = handle_delete_stock_transfer(entity_id, business, branch_id)
                    
                    elif entity_type == 'WasteRecord':
                        if operation == 'create':
                            result = handle_create_waste_record(entity_id, data, business, branch_id)
                        elif operation == 'update':
                            result = handle_update_waste_record(entity_id, data, business, branch_id)
                        elif operation == 'delete':
                            result = handle_delete_waste_record(entity_id, business, branch_id)
                    
                    elif entity_type == 'Supplier':
                        if operation == 'create':
                            result = handle_create_supplier(entity_id, data, business)
                        elif operation == 'update':
                            result = handle_update_supplier(entity_id, data, business)
                        elif operation == 'delete':
                            result = handle_delete_supplier(entity_id, business)
                    
                    elif entity_type == 'PurchaseRecord':
                        if operation == 'create':
                            result = handle_create_purchase_record(entity_id, data, business, branch_id)
                        elif operation == 'update':
                            result = handle_update_purchase_record(entity_id, data, business, branch_id)
                        elif operation == 'delete':
                            result = handle_delete_purchase_record(entity_id, business, branch_id)
                    
                    else:
                        results['errors'].append({
                            'id': entity_id,
                            'error': f'Unknown entity type: {entity_type}'
                        })
                        continue
                    
                    # Check if operation was recognized
                    if result is None:
                        results['errors'].append({
                            'id': entity_id,
                            'error': f'Unknown operation: {operation}'
                        })
                        continue
                    
                    # Process result
                    if result['success']:
                        results['acknowledged'].append({
                            'id': entity_id,
                            'status': 'success',
                            'server_id': result.get('server_id', entity_id)
                        })
                        print(f"[Sync Push] Successfully processed {operation} for {entity_id}")
                    else:
                        results['errors'].append({
                            'id': entity_id,
                            'error': result.get('error', 'Unknown error')
                        })
                        print(f"[Sync Push] Error processing {operation} for {entity_id}: {result.get('error')}")
                        
            except Exception as e:
                print(f"[Sync Push] Exception processing change: {str(e)}")
                import traceback
                traceback.print_exc()
                results['errors'].append({
                    'id': change.get('id'),
                    'error': str(e)
                })
        
        return Response({
            'synced_at': timezone.now().isoformat(),
            'results': results
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        print(f"[Sync Push] Fatal error: {str(e)}")
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
            "inventory_items": [...],
            "suppliers": [...],
            "purchase_orders": [...]
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
        
        print(f"[Sync Pull] Pulling changes since {since_dt} for branch {branch_id}")
        
        # Get user's business (owner or assigned staff business)
        business = _resolve_user_business(request.user)
        if not business:
            return Response(
                {'error': 'User does not have an associated business'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Resolve branch reference: client may send a non-PK identifier (e.g., a code like "BRN-10").
        # Try to match within the user's business by:
        # 1) direct PK, then 2) name match. If not found, return error.
        from django.core.exceptions import ValidationError
        from django.utils.translation import gettext as _
        from business.models import Branch
        
        resolved_branch = None
        # Normalize potential client formats:
        # - 'BRN-<number>' -> numeric PK
        # - '<number>' -> numeric PK
        # - '<name>' -> branch name
        branch_lookup_error = None
        if isinstance(branch_id, str):
            import re
            m = re.match(r"^BRN-(\d+)$", branch_id, flags=re.IGNORECASE)
            if m:
                try:
                    resolved_branch = Branch.objects.get(pk=int(m.group(1)), business=business)
                except Exception as e:
                    branch_lookup_error = e
            else:
                legacy_match = re.match(r"^branch-(\d+)$", branch_id, flags=re.IGNORECASE)
                if legacy_match:
                    try:
                        resolved_branch = Branch.objects.get(pk=int(legacy_match.group(1)), business=business)
                    except Exception as e:
                        branch_lookup_error = e
                elif branch_id.isdigit():
                    try:
                        resolved_branch = Branch.objects.get(pk=int(branch_id), business=business)
                    except Exception as e:
                        branch_lookup_error = e
        
        # Fallback: attempt direct PK match as given (covers non-str scenarios)
        if resolved_branch is None:
            try:
                resolved_branch = Branch.objects.get(pk=branch_id, business=business)
            except Exception as e:
                branch_lookup_error = e
        
        # Final fallbacks: handle "main" alias, then slug/name matching.
        if resolved_branch is None and isinstance(branch_id, str):
            normalized_branch = branch_id.strip().lower()
            if normalized_branch in {'main', 'main-branch', 'main_branch'}:
                resolved_branch = (
                    Branch.objects
                    .filter(business=business, name__iendswith='Main Branch')
                    .order_by('created_at', 'id')
                    .first()
                )

        if resolved_branch is None:
            try:
                resolved_branch = Branch.objects.get(slug__iexact=branch_id, business=business)
            except Branch.DoesNotExist:
                try:
                    resolved_branch = Branch.objects.get(name=branch_id, business=business)
                except Branch.DoesNotExist:
                    return Response(
                        {'error': f"Branch '{branch_id}' not found for this business"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
        
        # Fetch updated inventory items for the resolved branch
        inventory_items = InventoryItem.objects.filter(
            business=business,
            branch=resolved_branch,
            updated_at__gte=since_dt
        ).order_by('updated_at')
        
        print(f"[Sync Pull] Found {inventory_items.count()} updated inventory items")
        
        inventory_serializer = InventoryItemSerializer(inventory_items, many=True)
        inventory_data = inventory_serializer.data

        # Ensure branch_id is included for clients to correctly scope items to a branch.
        # The base serializer omits branch, which causes cross-device pulls to store items
        # without branchId and therefore hide them in branch-filtered UIs.
        for idx, item in enumerate(inventory_items):
            try:
                inventory_data[idx]['branch_id'] = str(item.branch_id)
            except Exception:
                # Best-effort: if index mismatch or serialization issue, skip quietly.
                pass
        
        # Fetch updated suppliers with balance information
        suppliers = Supplier.objects.filter(
            business=business,
            updated_at__gte=since_dt
        ).order_by('updated_at')
        
        print(f"[Sync Pull] Found {suppliers.count()} updated suppliers")
        
        # Serialize suppliers with balance information
        suppliers_data = []
        for supplier in suppliers:
            suppliers_data.append({
                'id': str(supplier.id),
                'name': supplier.name,
                'contact_person': supplier.contact_person,
                'email': supplier.email,
                'phone': supplier.phone,
                'address': supplier.address,
                'city': supplier.city,
                'region': supplier.region,
                'country': supplier.country,
                'is_active': supplier.is_active,
                'supplier_tin': supplier.supplier_tin,
                'mra_supplier_id': supplier.mra_supplier_id,
                'vat_registered': supplier.vat_registered,
                'total_amount_due': float(supplier.total_amount_due),
                'total_amount_paid': float(supplier.total_amount_paid),
                'balance_due': float(supplier.get_balance_due()),
                'updated_at': supplier.updated_at.isoformat(),
            })

        # Fetch updated purchase orders (include items updated since)
        purchase_orders = (
            PurchaseOrder.objects.filter(
                business=business,
                branch=resolved_branch,
            )
            .filter(Q(updated_at__gte=since_dt) | Q(items__updated_at__gte=since_dt))
            .distinct()
            .order_by('updated_at')
            .prefetch_related('items', 'supplier')
        )
        
        print(f"[Sync Pull] Found {purchase_orders.count()} updated purchase orders")
        purchase_orders_data = PurchaseOrderSerializer(purchase_orders, many=True).data
        
        return Response({
            'pulled_at': timezone.now().isoformat(),
            'changes': {
                'inventory_items': inventory_data,
                'suppliers': suppliers_data,
                'purchase_orders': purchase_orders_data
            }
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        print(f"[Sync Pull] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response(
            {'error': str(e)},
            status=status.HTTP_400_BAD_REQUEST
        )
