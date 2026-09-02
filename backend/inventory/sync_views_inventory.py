"""
Inventory Item Sync Views
Handles synchronization of inventory items between frontend and backend
"""

import math
import uuid
from django.db import IntegrityError

from .models import InventoryItem
from business.models import Business, Branch


def _parse_finite_float(value, field_name, default=None):
    """
    Parse a finite float from incoming sync payload values.
    Returns `default` for empty/invalid/non-finite values.
    """
    if value in (None, '', 'null', 'undefined'):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        print(f"[Sync] Warning: Invalid numeric value for {field_name}: {value}")
        return default
    if not math.isfinite(parsed):
        print(f"[Sync] Warning: Non-finite numeric value for {field_name}: {value}")
        return default
    return parsed

def _parse_bool(value, default=False):
    """
    Parse a boolean value from sync payloads.
    Handles bool/int/string safely (avoids bool("false") == True).
    """
    if value in (None, 'null', 'undefined'):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('true', '1', 'yes', 'y', 'on'):
            return True
        if normalized in ('false', '0', 'no', 'n', 'off', ''):
            return False
    return default


def _is_valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def handle_create_inventory_item(item_id, data, business, branch_id):
    """Handle creation of inventory item from frontend"""
    try:
        print(f"[Sync CREATE] Received data for item {item_id}: {data}")
        print(f"[Sync CREATE] Data keys: {list(data.keys())}")
        print(f"[Sync CREATE] product_code: {data.get('product_code')}, barcode: {data.get('barcode')}, sku: {data.get('sku')}")
        
        # Try to get branch by ID (could be integer or string UUID)
        branch = None
        try:
            # First try as integer (for backward compatibility)
            if isinstance(branch_id, str) and branch_id.isdigit():
                branch = Branch.objects.get(id=int(branch_id), business=business)
            else:
                # Try as string UUID
                branch = Branch.objects.get(id=branch_id, business=business)
        except (Branch.DoesNotExist, ValueError):
            # If branch doesn't exist, try to get the first branch for this business
            branch = business.branches.first()
            if not branch:
                return {
                    'success': False,
                    'error': f'No branch found for this business'
                }
            print(f"[Sync] Branch {branch_id} not found, using default branch {branch.id}")
        
        # Check if item already exists by ID
        existing = InventoryItem.objects.filter(
            id=item_id,
            business=business
        ).first()
        
        if existing:
            print(f"[Sync] Item {item_id} already exists, updating instead")
            return handle_update_inventory_item(item_id, data, business, branch_id)
        
        # Check if item with same name already exists in this branch
        item_name = data.get('name', 'Unnamed Item')
        existing_by_name = InventoryItem.objects.filter(
            name=item_name,
            business=business,
            branch=branch
        ).first()
        
        if existing_by_name:
            print(f"[Sync] Item with name '{item_name}' already exists in this branch (ID: {existing_by_name.id}), updating instead")
            return handle_update_inventory_item(existing_by_name.id, data, business, branch_id)
        
        # Helper function to clean empty strings to None
        def clean_value(val):
            if val == '' or val == 'null' or val == 'undefined':
                return None
            return val
        
        stock_units_raw = data.get('stock_units')
        if stock_units_raw is None:
            stock_units_raw = data.get('stockUnits', 0)

        item_type_raw = data.get('item_type')
        if item_type_raw is None:
            item_type_raw = data.get('itemType', 'ingredient')

        unit_type_raw = data.get('unit_type')
        if unit_type_raw is None:
            unit_type_raw = data.get('unitType')

        reorder_level_raw = data.get('reorder_level')
        if reorder_level_raw is None:
            reorder_level_raw = data.get('reorderLevel', 0)

        is_variable_price_raw = data.get('is_variable_price')
        if is_variable_price_raw is None:
            is_variable_price_raw = data.get('isVariablePrice', False)

        is_fuel_raw = data.get('is_fuel')
        if is_fuel_raw is None:
            is_fuel_raw = data.get('isFuel', False)

        is_oil_raw = data.get('is_oil')
        if is_oil_raw is None:
            is_oil_raw = data.get('isOil', False)

        is_produced_raw = data.get('is_produced')
        if is_produced_raw is None:
            is_produced_raw = data.get('isProduced', False)

        on_menu_raw = data.get('on_menu')
        if on_menu_raw is None:
            on_menu_raw = data.get('onMenu', False)

        is_sold_in_portions_raw = data.get('is_sold_in_portions')
        if is_sold_in_portions_raw is None:
            is_sold_in_portions_raw = data.get('isSoldInPortions', False)

        portion_name_raw = data.get('portion_name')
        if portion_name_raw is None:
            portion_name_raw = data.get('portionName')

        portions_per_unit_raw = data.get('portions_per_unit')
        if portions_per_unit_raw is None:
            portions_per_unit_raw = data.get('portionsPerUnit')

        # Create new item with proper field mapping
        # Supports both snake_case and camelCase from frontend sync.
        item_data = {
            'id': item_id,
            'business': business,
            'branch': branch,
            'name': data.get('name', 'Unnamed Item'),
            'category': clean_value(data.get('category')) or '',
            'item_type': item_type_raw or 'ingredient',
            'manufacturer': clean_value(data.get('manufacturer')),
            'supplier': clean_value(data.get('supplier')),
            'stock_units': _parse_finite_float(stock_units_raw, 'stock_units', 0) or 0,
            'unit_type': clean_value(unit_type_raw),
            'reorder_level': _parse_finite_float(reorder_level_raw, 'reorder_level', 0) or 0,
            'cost': _parse_finite_float(data.get('cost'), 'cost', None),
            'value': _parse_finite_float(data.get('value', 0), 'value', 0) or 0,
            'status': data.get('status', 'In Stock'),
            'expiry': clean_value(data.get('expiry')),
            'batch': clean_value(data.get('batch')),
            'brand': clean_value(data.get('brand')),
            'price': _parse_finite_float(data.get('price'), 'price', None),
            'recipe': data.get('recipe') or [],
            'image': clean_value(data.get('image')),
            'is_variable_price': _parse_bool(is_variable_price_raw, False),
            'is_fuel': _parse_bool(is_fuel_raw, False),
            'is_oil': _parse_bool(is_oil_raw, False),
            'is_produced': _parse_bool(is_produced_raw, False),
            'on_menu': _parse_bool(on_menu_raw, False),
            'is_sold_in_portions': _parse_bool(is_sold_in_portions_raw, False),
            'portion_name': clean_value(portion_name_raw),
            'portions_per_unit': _parse_finite_float(portions_per_unit_raw, 'portions_per_unit', None),
            'product_code': clean_value(data.get('product_code')) or clean_value(data.get('productCode')),
            'barcode': clean_value(data.get('barcode')),
            'sku': clean_value(data.get('sku')),
        }
        
        # Remove None and empty string values to use model defaults
        item_data = {k: v for k, v in item_data.items() if v is not None and v != ''}
        
        product_code = item_data.get('product_code')
        if product_code:
            duplicate_product_code_item = (
                InventoryItem.objects
                .filter(product_code=product_code)
                .exclude(id=item_id)
                .first()
            )
            if duplicate_product_code_item:
                print(
                    f"[Sync CREATE] Duplicate product_code '{product_code}' detected "
                    f"(existing item: {duplicate_product_code_item.id}). "
                    "Clearing product_code to allow sync."
                )
                item_data.pop('product_code', None)

        item = InventoryItem.objects.create(**item_data)
        print(f"[Sync] Created inventory item {item_id}")
        
        return {
            'success': True,
            'server_id': str(item.id)
        }
        
    except Exception as e:
        if isinstance(e, IntegrityError) and 'product_code' in str(e).lower():
            print(
                f"[Sync CREATE] IntegrityError due to product_code for item {item_id}. "
                "Retrying create without product_code."
            )
            try:
                item_data = {
                    'id': item_id,
                    'business': business,
                    'branch': branch,
                    'name': data.get('name', 'Unnamed Item'),
                    'category': data.get('category') or '',
                    'item_type': data.get('item_type') or data.get('itemType') or 'ingredient',
                    'stock_units': _parse_finite_float(data.get('stock_units', data.get('stockUnits', 0)), 'stock_units', 0) or 0,
                    'status': data.get('status', 'In Stock'),
                }
                item = InventoryItem.objects.create(**item_data)
                print(f"[Sync CREATE] Created inventory item {item_id} without product_code after retry")
                return {
                    'success': True,
                    'server_id': str(item.id)
                }
            except Exception as retry_error:
                print(f"[Sync CREATE] Retry without product_code failed for item {item_id}: {retry_error}")
        print(f"[Sync] Error creating inventory item: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


def handle_update_inventory_item(item_id, data, business, branch_id):
    """Handle update of inventory item from frontend"""
    try:
        print(f"[Sync] Updating inventory item {item_id} with data keys: {list(data.keys())}")
        
        if _is_valid_uuid(item_id):
            item = InventoryItem.objects.get(
                id=item_id,
                business=business,
                branch_id=branch_id
            )
        else:
            item_name = data.get('name')
            item = InventoryItem.objects.get(
                name=item_name,
                business=business,
                branch_id=branch_id
            )
        
        print(f"[Sync] Found item {item_id}, current stock_units: {item.stock_units}")
        
        # Update fields - accepts both snake_case and camelCase payloads
        if 'name' in data:
            item.name = data['name']
        if 'category' in data:
            item.category = data['category']
        if 'item_type' in data or 'itemType' in data:
            next_item_type = data.get('item_type') or data.get('itemType')
            if next_item_type:
                item.item_type = next_item_type
        if 'manufacturer' in data:
            item.manufacturer = data['manufacturer']
        if 'supplier' in data:
            item.supplier = data['supplier']
        if 'stock_units' in data or 'stockUnits' in data:
            stock_value = data.get('stock_units') if 'stock_units' in data else data.get('stockUnits')
            parsed_stock = _parse_finite_float(stock_value, 'stock_units', None)
            if parsed_stock is None:
                print(f"[Sync] Skipping stock_units update for item {item_id} due to invalid value: {stock_value}")
            else:
                current_stock = _parse_finite_float(item.stock_units, 'current_stock_units', 0) or 0
                allow_stock_decrease = _parse_bool(
                    data.get('allow_stock_decrease', data.get('allowStockDecrease')),
                    False
                )
                # Sales/consumption decrements are authoritative on backend via POS order,
                # waste, and stock-transfer handlers. Ignore blind decrements coming from
                # generic inventory sync updates to prevent duplicate stock reduction.
                if parsed_stock < current_stock and not allow_stock_decrease:
                    print(
                        f"[Sync] Ignoring stock_units decrease for item {item_id}: "
                        f"{current_stock} -> {parsed_stock}. "
                        "Use dedicated stock movement endpoints for decrements."
                    )
                else:
                    print(f"[Sync] Updating stock_units from {item.stock_units} to {parsed_stock}")
                    item.stock_units = parsed_stock
        if 'unit_type' in data or 'unitType' in data:
            item.unit_type = data.get('unit_type') or data.get('unitType')
        if 'reorder_level' in data or 'reorderLevel' in data:
            reorder_value = data.get('reorder_level') if 'reorder_level' in data else data.get('reorderLevel')
            parsed_reorder = _parse_finite_float(reorder_value, 'reorder_level', None)
            if parsed_reorder is not None:
                item.reorder_level = parsed_reorder
        if 'cost' in data:
            item.cost = _parse_finite_float(data.get('cost'), 'cost', None)
        if 'value' in data:
            parsed_value = _parse_finite_float(data.get('value'), 'value', None)
            if parsed_value is not None:
                item.value = parsed_value
        if 'status' in data:
            item.status = data['status']
        if 'expiry' in data:
            item.expiry = data['expiry']
        if 'batch' in data:
            item.batch = data['batch']
        if 'brand' in data:
            item.brand = data['brand']
        if 'price' in data:
            item.price = _parse_finite_float(data.get('price'), 'price', None)
        if 'recipe' in data:
            item.recipe = data['recipe']
        if 'image' in data:
            item.image = data['image']
        if 'is_variable_price' in data or 'isVariablePrice' in data:
            item.is_variable_price = _parse_bool(
                data.get('is_variable_price', data.get('isVariablePrice')),
                item.is_variable_price
            )
        if 'is_fuel' in data or 'isFuel' in data:
            item.is_fuel = _parse_bool(
                data.get('is_fuel', data.get('isFuel')),
                item.is_fuel
            )
        if 'is_oil' in data or 'isOil' in data:
            item.is_oil = _parse_bool(
                data.get('is_oil', data.get('isOil')),
                item.is_oil
            )
        if 'is_produced' in data or 'isProduced' in data:
            item.is_produced = _parse_bool(
                data.get('is_produced', data.get('isProduced')),
                item.is_produced
            )
        if 'on_menu' in data or 'onMenu' in data:
            item.on_menu = _parse_bool(
                data.get('on_menu', data.get('onMenu')),
                item.on_menu
            )
        if 'is_sold_in_portions' in data or 'isSoldInPortions' in data:
            item.is_sold_in_portions = _parse_bool(
                data.get('is_sold_in_portions', data.get('isSoldInPortions')),
                item.is_sold_in_portions
            )
        if 'portion_name' in data or 'portionName' in data:
            item.portion_name = data.get('portion_name') if 'portion_name' in data else data.get('portionName')
        if 'portions_per_unit' in data or 'portionsPerUnit' in data:
            portions_value = data.get('portions_per_unit') if 'portions_per_unit' in data else data.get('portionsPerUnit')
            item.portions_per_unit = _parse_finite_float(portions_value, 'portions_per_unit', None)
        if 'product_code' in data or 'productCode' in data:
            incoming_product_code = data.get('product_code') or data.get('productCode')
            incoming_product_code = incoming_product_code if incoming_product_code not in ('', 'null', 'undefined') else None
            if incoming_product_code:
                duplicate_product_code_item = (
                    InventoryItem.objects
                    .filter(product_code=incoming_product_code)
                    .exclude(id=item.id)
                    .first()
                )
                if duplicate_product_code_item:
                    print(
                        f"[Sync UPDATE] Duplicate product_code '{incoming_product_code}' for item {item_id} "
                        f"(existing item: {duplicate_product_code_item.id}). Keeping current product_code."
                    )
                else:
                    item.product_code = incoming_product_code
                    print(f"[Sync UPDATE] Updated product_code to: {item.product_code}")
            else:
                item.product_code = None
                print(f"[Sync UPDATE] Cleared product_code for item {item_id}")
        if 'barcode' in data:
            item.barcode = data['barcode']
            print(f"[Sync UPDATE] Updated barcode to: {item.barcode}")
        if 'sku' in data:
            item.sku = data['sku']
            print(f"[Sync UPDATE] Updated sku to: {item.sku}")
        
        try:
            item.save()
        except IntegrityError as integrity_error:
            if 'product_code' in str(integrity_error).lower():
                print(
                    f"[Sync UPDATE] IntegrityError on product_code for item {item_id}. "
                    "Retrying save without product_code update."
                )
                item.product_code = None
                item.save()
            else:
                raise
        print(f"[Sync] Updated inventory item {item_id}, new stock_units: {item.stock_units}")
        
        return {
            'success': True,
            'server_id': str(item.id)
        }
        
    except InventoryItem.DoesNotExist:
        print(f"[Sync] Item {item_id} not found, creating instead")
        return handle_create_inventory_item(item_id, data, business, branch_id)
    except Exception as e:
        print(f"[Sync] Error updating inventory item: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def handle_delete_inventory_item(item_id, business, branch_id):
    """Handle deletion of inventory item from frontend"""
    try:
        # Primary lookup by id + business; branch is advisory here.
        # If client sends a mismatched/non-normalized branch_id (e.g. legacy format),
        # we still must delete the intended item by ID to avoid "resurrecting" records.
        item = InventoryItem.objects.filter(
            id=item_id,
            business=business
        ).first()

        if not item:
            print(f"[Sync] Item {item_id} not found for deletion")
            return {
                'success': True,
                'server_id': item_id
            }

        if str(item.branch_id) != str(branch_id):
            print(
                f"[Sync] Delete branch mismatch for item {item_id}: "
                f"payload branch={branch_id}, item branch={item.branch_id}. "
                f"Proceeding with delete by item ID."
            )
        
        item.delete()
        print(f"[Sync] Deleted inventory item {item_id}")
        
        return {
            'success': True,
            'server_id': item_id
        }
        
    except Exception as e:
        print(f"[Sync] Error deleting inventory item: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }
