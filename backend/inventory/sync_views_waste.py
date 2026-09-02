"""
Waste Record Sync Views
Handles synchronization of waste records between frontend and backend
"""

from django.db import transaction
from .models import InventoryItem, WasteRecord, AuditLog
from business.models import Business, Branch


def _submit_eis_inventory_adjustment(*, business, branch, inventory_item, quantity, adjustment_type, reason, remarks=''):
    """Submit an EIS stock adjustment without blocking local waste sync."""
    try:
        from mra_eis.services import StockReceivingService

        result = StockReceivingService.submit_inventory_item_adjustment(
            business=business,
            branch=branch,
            inventory_item=inventory_item,
            quantity=quantity,
            adjustment_type=adjustment_type,
            reason=reason,
            remarks=remarks,
        )
        print(f"[Sync Waste] EIS stock adjustment result for {inventory_item.id}: {result}")
        return result
    except Exception as exc:
        print(f"[Sync Waste] Warning: EIS stock adjustment failed for {inventory_item.id}: {exc}")
        return {'submitted': False, 'error': str(exc)}


def handle_create_waste_record(waste_id, data, business, branch_id):
    """Handle creation of waste record from frontend"""
    try:
        from inventory.models import PurchaseOrderItem
        from decimal import Decimal
        
        # Validate branch exists
        try:
            branch = Branch.objects.get(id=branch_id, business=business)
        except Branch.DoesNotExist:
            return {
                'success': False,
                'error': f'Branch {branch_id} not found for this business'
            }
        
        # Check if waste record already exists
        existing = WasteRecord.objects.filter(id=waste_id, business=business).first()
        if existing:
            print(f"[Sync Waste] Waste record {waste_id} already exists, updating instead")
            return handle_update_waste_record(waste_id, data, business, branch_id)
        
        # Get inventory item - support both camelCase and snake_case
        item_id = data.get('itemId') or data.get('item_id')
        if not item_id:
            print(f"[Sync Waste] Item ID is required. Data keys: {list(data.keys())}")
            return {
                'success': False,
                'error': 'Item ID is required'
            }
        
        try:
            inventory_item = InventoryItem.objects.get(id=item_id, business=business)
        except InventoryItem.DoesNotExist:
            return {
                'success': False,
                'error': f'Inventory item {item_id} not found'
            }
        
        # Get the batch (PurchaseOrderItem) if provided - support both camelCase and snake_case
        batch_id = data.get('batchId') or data.get('batch_id')
        purchase_order_item = None
        if batch_id:
            try:
                purchase_order_item = PurchaseOrderItem.objects.get(id=batch_id)
                print(f"[Sync Waste] Found batch {batch_id} for waste record")
            except PurchaseOrderItem.DoesNotExist:
                print(f"[Sync Waste] Warning: Batch {batch_id} not found, proceeding without batch link")
        
        with transaction.atomic():
            # Create waste record
            quantity_wasted = Decimal(str(data.get('quantity', 0)))
            waste_data = {
                'id': waste_id,
                'business': business,
                'branch': branch,
                'inventory_item': inventory_item,
                'purchase_order_item': purchase_order_item,  # Link to specific batch for FIFO
                'session_id': data.get('sessionId') or data.get('session_id'),  # NEW: Link to session
                'quantity': quantity_wasted,
                'unit': data.get('unit', ''),
                'cost': Decimal(str(data.get('cost', 0))),
                'reason': data.get('reason', 'Other'),
                'notes': data.get('notes', ''),
                'recorded_by': data.get('recordedBy', 'System'),
            }
            
            waste = WasteRecord.objects.create(**waste_data)
            print(f"[Sync Waste] Created waste record {waste_id}")
            
            # Decrement the batch (PurchaseOrderItem) first if linked
            if purchase_order_item:
                old_batch_qty = purchase_order_item.quantity_remaining
                purchase_order_item.quantity_remaining -= quantity_wasted
                purchase_order_item.quantity_remaining = max(0, purchase_order_item.quantity_remaining)
                purchase_order_item.save()
                print(f"[Sync Waste] Decremented batch {batch_id}: quantity_remaining {old_batch_qty} -> {purchase_order_item.quantity_remaining}")
            else:
                print(f"[Sync Waste] Warning: No batch linked to waste record {waste_id}, only decrementing main inventory")
            
            # Decrement main inventory item stock and update status
            old_stock = inventory_item.stock_units
            inventory_item.stock_units -= quantity_wasted
            inventory_item.stock_units = max(0, inventory_item.stock_units)  # Prevent negative stock
            inventory_item.value = inventory_item.stock_units * (inventory_item.cost or 0)
            inventory_item.update_status()
            inventory_item.save()
            print(f"[Sync Waste] Decremented main inventory {item_id}: stock {old_stock} -> {inventory_item.stock_units}")
            
            # Log audit action
            AuditLog.objects.create(
                business=business,
                branch=branch,
                user=None,  # System action
                action_type='STOCK_WASTE',
                entity_type='WasteRecord',
                entity_id=str(waste.id),
                details={
                    'item_name': inventory_item.name,
                    'quantity': float(quantity_wasted),
                    'reason': data.get('reason', 'Other'),
                    'cost': float(data.get('cost', 0)),
                    'recorded_by': data.get('recordedBy', 'System'),
                    'batch_id': str(batch_id) if batch_id else None,
                }
            )
            print(f"[Sync Waste] Logged audit action for waste record {waste_id}")

        eis_stock_sync = _submit_eis_inventory_adjustment(
            business=business,
            branch=branch,
            inventory_item=inventory_item,
            quantity=quantity_wasted,
            adjustment_type='Decrease',
            reason=f"Waste: {data.get('reason', 'Other')}",
            remarks=f"Waste record {waste_id}: {data.get('notes', '')}",
        )
        
        return {
            'success': True,
            'server_id': str(waste.id),
            'eis_stock_sync': eis_stock_sync,
        }
        
    except Exception as e:
        print(f"[Sync Waste] Error creating waste record: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


def handle_update_waste_record(waste_id, data, business, branch_id):
    """Handle update of waste record from frontend"""
    try:
        from decimal import Decimal
        
        waste = WasteRecord.objects.get(id=waste_id, business=business)
        
        eis_stock_sync = None
        eis_adjustment_kwargs = None
        with transaction.atomic():
            # Track old quantity for inventory adjustment
            old_quantity = waste.quantity
            new_quantity = Decimal(str(data.get('quantity', old_quantity)))
            quantity_difference = new_quantity - old_quantity
            
            # Update waste record fields if provided
            if 'quantity' in data:
                waste.quantity = new_quantity
            if 'unit' in data:
                waste.unit = data['unit']
            if 'cost' in data:
                waste.cost = Decimal(str(data['cost']))
            if 'reason' in data:
                waste.reason = data['reason']
            if 'notes' in data:
                waste.notes = data['notes']
            if 'recordedBy' in data:
                waste.recorded_by = data['recordedBy']
            
            waste.save()
            print(f"[Sync Waste] Updated waste record {waste_id}")
            
            # Adjust inventory if quantity changed
            if quantity_difference != 0:
                from inventory.models import PurchaseOrderItem
                
                inventory_item = waste.inventory_item
                
                # Adjust batch (PurchaseOrderItem) if linked
                if waste.purchase_order_item:
                    old_batch_qty = waste.purchase_order_item.quantity_remaining
                    waste.purchase_order_item.quantity_remaining -= quantity_difference
                    waste.purchase_order_item.quantity_remaining = max(0, waste.purchase_order_item.quantity_remaining)
                    waste.purchase_order_item.save()
                    print(f"[Sync Waste] Adjusted batch {waste.purchase_order_item.id}: quantity_remaining adjusted by {quantity_difference} ({old_batch_qty} -> {waste.purchase_order_item.quantity_remaining})")
                
                # Adjust main inventory stock
                old_stock = inventory_item.stock_units
                inventory_item.stock_units -= quantity_difference
                inventory_item.stock_units = max(0, inventory_item.stock_units)
                inventory_item.value = inventory_item.stock_units * (inventory_item.cost or 0)
                inventory_item.update_status()
                inventory_item.save()
                print(f"[Sync Waste] Adjusted main inventory {inventory_item.id}: stock adjusted by {quantity_difference} ({old_stock} -> {inventory_item.stock_units})")
                
                # Log audit action for update
                AuditLog.objects.create(
                    business=business,
                    branch=waste.branch,
                    user=None,  # System action
                    action_type='STOCK_WASTE',
                    entity_type='WasteRecord',
                    entity_id=str(waste.id),
                    details={
                        'action': 'updated',
                        'item_name': inventory_item.name,
                        'old_quantity': float(old_quantity),
                        'new_quantity': float(new_quantity),
                        'quantity_difference': float(quantity_difference),
                        'reason': waste.reason,
                        'cost': float(waste.cost),
                        'batch_id': str(waste.purchase_order_item.id) if waste.purchase_order_item else None,
                    }
                )
                print(f"[Sync Waste] Logged audit action for waste record update {waste_id}")

                if quantity_difference > 0:
                    eis_adjustment_kwargs = {
                        'business': business,
                        'branch': waste.branch,
                        'inventory_item': inventory_item,
                        'quantity': quantity_difference,
                        'adjustment_type': 'Decrease',
                        'reason': f"Waste increased: {waste.reason}",
                        'remarks': f"Waste record {waste_id} quantity increased.",
                    }
                elif quantity_difference < 0:
                    eis_adjustment_kwargs = {
                        'business': business,
                        'branch': waste.branch,
                        'inventory_item': inventory_item,
                        'quantity': -quantity_difference,
                        'adjustment_type': 'Increase',
                        'reason': f"Waste reduced: {waste.reason}",
                        'remarks': f"Waste record {waste_id} quantity reduced.",
                    }

        if eis_adjustment_kwargs:
            eis_stock_sync = _submit_eis_inventory_adjustment(**eis_adjustment_kwargs)
        
        return {
            'success': True,
            'server_id': str(waste.id),
            'eis_stock_sync': eis_stock_sync,
        }
        
    except WasteRecord.DoesNotExist:
        print(f"[Sync Waste] Waste record {waste_id} not found, creating instead")
        return handle_create_waste_record(waste_id, data, business, branch_id)
    except Exception as e:
        print(f"[Sync Waste] Error updating waste record: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


def handle_delete_waste_record(waste_id, business, branch_id):
    """Handle deletion of waste record from frontend"""
    try:
        waste = WasteRecord.objects.get(id=waste_id, business=business)
        
        eis_stock_sync = None
        eis_adjustment_kwargs = None
        with transaction.atomic():
            # Reverse the batch update if linked
            if waste.purchase_order_item:
                old_batch_qty = waste.purchase_order_item.quantity_remaining
                waste.purchase_order_item.quantity_remaining += waste.quantity
                waste.purchase_order_item.save()
                print(f"[Sync Waste] Reversed batch update for {waste.purchase_order_item.id}: quantity_remaining increased by {waste.quantity} ({old_batch_qty} -> {waste.purchase_order_item.quantity_remaining})")
            
            # Reverse the main inventory update
            inventory_item = waste.inventory_item
            old_stock = inventory_item.stock_units
            inventory_item.stock_units += waste.quantity
            inventory_item.value = inventory_item.stock_units * (inventory_item.cost or 0)
            inventory_item.update_status()
            inventory_item.save()
            print(f"[Sync Waste] Reversed main inventory update for {inventory_item.id}: stock increased by {waste.quantity} ({old_stock} -> {inventory_item.stock_units})")
            eis_adjustment_kwargs = {
                'business': business,
                'branch': waste.branch,
                'inventory_item': inventory_item,
                'quantity': waste.quantity,
                'adjustment_type': 'Increase',
                'reason': f"Waste reversed: {waste.reason}",
                'remarks': f"Waste record {waste_id} deleted.",
            }
            
            # Log audit action for deletion
            AuditLog.objects.create(
                business=business,
                branch=waste.branch,
                user=None,  # System action
                action_type='STOCK_WASTE',
                entity_type='WasteRecord',
                entity_id=str(waste.id),
                details={
                    'action': 'deleted',
                    'item_name': inventory_item.name,
                    'quantity': float(waste.quantity),
                    'reason': waste.reason,
                    'cost': float(waste.cost),
                    'batch_id': str(waste.purchase_order_item.id) if waste.purchase_order_item else None,
                }
            )
            print(f"[Sync Waste] Logged audit action for waste record deletion {waste_id}")
            
            waste.delete()
            print(f"[Sync Waste] Deleted waste record {waste_id}")

        if eis_adjustment_kwargs:
            eis_stock_sync = _submit_eis_inventory_adjustment(**eis_adjustment_kwargs)
        
        return {
            'success': True,
            'server_id': waste_id,
            'eis_stock_sync': eis_stock_sync,
        }
        
    except WasteRecord.DoesNotExist:
        print(f"[Sync Waste] Waste record {waste_id} not found for deletion")
        return {
            'success': True,
            'server_id': waste_id
        }
    except Exception as e:
        print(f"[Sync Waste] Error deleting waste record: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }
