"""
Stock Transfer Sync Views
Handles synchronization of stock transfers between frontend and backend
"""

from .models import InventoryItem, StockTransfer
from business.models import Business, Branch
from .services import InventoryService


def _is_valid_uuid(value):
    try:
        import uuid
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


# ============================================================================
# STOCK TRANSFER HANDLERS
# ============================================================================

def handle_create_stock_transfer(transfer_id, data, business, branch_id):
    """Handle creation of stock transfer from frontend"""
    try:
        from django.db import transaction
        from decimal import Decimal
        
        # Validate from branch exists
        try:
            from_branch = Branch.objects.get(id=branch_id, business=business)
        except Branch.DoesNotExist:
            return {
                'success': False,
                'error': f'From branch {branch_id} not found for this business'
            }
        
        # Validate to branch exists
        to_branch_id = data.get('toBranchId') or data.get('to_branch_id') or data.get('to_branch') or data.get('branchId')
        if not to_branch_id:
            return {
                'success': False,
                'error': 'To branch ID is required'
            }
        
        try:
            to_branch = Branch.objects.get(id=to_branch_id, business=business)
        except Branch.DoesNotExist:
            return {
                'success': False,
                'error': f'To branch {to_branch_id} not found for this business'
            }
        
        # Check if transfer already exists
        existing = (
            StockTransfer.objects.filter(id=transfer_id, business=business).first()
            if _is_valid_uuid(transfer_id)
            else None
        )
        if existing:
            print(f"[Sync Stock] Transfer {transfer_id} already exists, updating instead")
            return handle_update_stock_transfer(transfer_id, data, business, branch_id)
        
        # Get inventory item
        item_id = data.get('itemId') or data.get('item_id') or data.get('inventoryItemId') or data.get('inventory_item_id')
        if not item_id:
            return {
                'success': False,
                'error': 'Item ID is required'
            }
        
        try:
            inventory_item = InventoryItem.objects.get(id=item_id, business=business, branch=from_branch)
        except InventoryItem.DoesNotExist:
            return {
                'success': False,
                'error': f'Inventory item {item_id} not found in source branch'
            }
        
        quantity = Decimal(str(data.get('quantity', 0)))
        try:
            InventoryService._validate_eis_branch_transfer_mapping(
                from_branch,
                to_branch,
                business,
            )
            eis_transfer_result = InventoryService.submit_eis_transfer_if_required(
                from_branch,
                to_branch,
                inventory_item,
                quantity,
            )
        except Exception as exc:
            return {
                'success': False,
                'error': str(exc),
            }
        
        with transaction.atomic():
            # 1. Decrement stock at source branch
            inventory_item.stock_units -= quantity
            inventory_item.value = inventory_item.stock_units * (inventory_item.cost or 0)
            inventory_item.update_status()
            inventory_item.save()
            print(f"[Sync Stock] Decremented source branch stock: {item_id} by {quantity}")
            
            # 2. Find or create item at destination branch and increment stock
            # Use product name as the unique identifier for matching across branches
            destination_item, created = InventoryItem.objects.get_or_create(
                business=business,
                branch=to_branch,
                name=inventory_item.name,
                defaults={
                    'category': inventory_item.category,
                    'item_type': inventory_item.item_type,
                    'unit_type': inventory_item.unit_type,
                    'cost': inventory_item.cost,
                    'price': inventory_item.price,
                    'stock_units': quantity,
                    'value': quantity * (inventory_item.cost or 0),
                    'status': 'In Stock' if quantity > 0 else 'Out of Stock',
                    'supplier': inventory_item.supplier,
                    'manufacturer': inventory_item.manufacturer,
                    'batch': inventory_item.batch,
                    'expiry': inventory_item.expiry,
                    'sku': inventory_item.sku,
                    'barcode': inventory_item.barcode,
                    'is_recipe_ingredient': inventory_item.is_recipe_ingredient,
                    'is_produced': inventory_item.is_produced,
                    'is_sold_in_portions': inventory_item.is_sold_in_portions,
                    'portion_name': inventory_item.portion_name,
                    'portions_per_unit': inventory_item.portions_per_unit,
                    'brand': inventory_item.brand,
                    'recipe': inventory_item.recipe,
                    'image': inventory_item.image,
                    'on_menu': inventory_item.on_menu,
                    'reorder_level': inventory_item.reorder_level,
                }
            )
            
            if not created:
                # Item already exists at destination, just increment stock
                destination_item.stock_units += quantity
                destination_item.value = destination_item.stock_units * (destination_item.cost or 0)
                destination_item.update_status()
                print(f"[Sync Stock] Incremented existing destination item stock: {destination_item.id} by {quantity}")
            else:
                print(f"[Sync Stock] Created new destination item: {destination_item.id} with stock: {quantity}")
            
            destination_item.save()
            print(f"[Sync Stock] Destination branch stock updated: {destination_item.id}")
            
            # 3. Create transfer record
            transfer_data = {
                'business': business,
                'from_branch': from_branch,
                'to_branch': to_branch,
                'inventory_item': inventory_item,
                'quantity': quantity,
                'mra_notified': bool(eis_transfer_result),
                'initiated_by': data.get('initiatedBy') or data.get('initiated_by') or 'System',
            }
            if _is_valid_uuid(transfer_id):
                transfer_data['id'] = transfer_id
            
            transfer = StockTransfer.objects.create(**transfer_data)
            print(f"[Sync Stock] Created stock transfer {transfer_id}")
        
        return {
            'success': True,
            'server_id': str(transfer.id)
        }
        
    except Exception as e:
        print(f"[Sync Stock] Error creating stock transfer: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


def handle_update_stock_transfer(transfer_id, data, business, branch_id):
    """Handle update of stock transfer from frontend"""
    try:
        transfer = StockTransfer.objects.get(id=transfer_id, business=business)
        
        # Update fields if provided
        if 'quantity' in data:
            transfer.quantity = float(data['quantity'])
        if 'initiatedBy' in data:
            transfer.initiated_by = data['initiatedBy']
        
        transfer.save()
        print(f"[Sync Stock] Updated stock transfer {transfer_id}")
        
        return {
            'success': True,
            'server_id': str(transfer.id)
        }
        
    except StockTransfer.DoesNotExist:
        print(f"[Sync Stock] Transfer {transfer_id} not found, creating instead")
        return handle_create_stock_transfer(transfer_id, data, business, branch_id)
    except Exception as e:
        print(f"[Sync Stock] Error updating stock transfer: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def handle_delete_stock_transfer(transfer_id, business, branch_id):
    """Handle deletion of stock transfer from frontend"""
    try:
        transfer = StockTransfer.objects.get(id=transfer_id, business=business)
        transfer.delete()
        print(f"[Sync Stock] Deleted stock transfer {transfer_id}")
        
        return {
            'success': True,
            'server_id': transfer_id
        }
        
    except StockTransfer.DoesNotExist:
        print(f"[Sync Stock] Transfer {transfer_id} not found for deletion")
        return {
            'success': True,
            'server_id': transfer_id
        }
    except Exception as e:
        print(f"[Sync Stock] Error deleting stock transfer: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }
