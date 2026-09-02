"""
Supplier Sync Views
Handles synchronization of suppliers between frontend and backend
"""

from .models import Supplier


def _parse_boolean(value, default=False):
    """Parse boolean-like values from sync payloads safely."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return default


def _parse_positive_int(value):
    if value in (None, '', 'null', 'undefined'):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _payload_get(data, *keys):
    for key in keys:
        if key in data:
            return data.get(key)
    return None


def _business_eis_enabled(business):
    try:
        settings_obj = business.settings
    except Exception:
        settings_obj = None
    return bool(getattr(settings_obj, 'enable_eis', False))


def _eis_managed_rejection():
    return {
        'success': False,
        'error': 'Suppliers are managed by MRA EIS. Use Sync EIS Suppliers.'
    }


def handle_create_supplier(supplier_id, data, business):
    """Handle creation of supplier from frontend"""
    try:
        if _business_eis_enabled(business):
            return _eis_managed_rejection()

        # Check if supplier already exists
        existing = Supplier.objects.filter(id=supplier_id, business=business).first()
        if existing:
            print(f"[Sync] Supplier {supplier_id} already exists, updating instead")
            return handle_update_supplier(supplier_id, data, business)
        
        # Create new supplier
        contact_person = _payload_get(data, 'contact_person', 'contactPerson')
        supplier_tin = _payload_get(data, 'supplier_tin', 'supplierTin')
        vat_registered = _payload_get(data, 'vat_registered', 'vatRegistered')
        mra_supplier_id = _payload_get(data, 'mra_supplier_id', 'mraSupplierId')
        region = _payload_get(data, 'region', 'region_state', 'regionState', 'state')

        supplier_data = {
            'id': supplier_id,
            'business': business,
            'name': data.get('name', 'Unnamed Supplier'),
            'contact_person': contact_person or '',
            'email': data.get('email', ''),
            'phone': data.get('phone', ''),
            'address': data.get('address', ''),
            'city': data.get('city', ''),
            'region': region or '',
            'country': data.get('country', ''),
            'is_active': data.get('is_active', True),
            'supplier_tin': supplier_tin if supplier_tin not in ('', None) else None,
            'mra_supplier_id': _parse_positive_int(mra_supplier_id),
            'vat_registered': _parse_boolean(vat_registered, default=False),
        }
        
        # Remove empty strings
        supplier_data = {k: v for k, v in supplier_data.items() if v != ''}
        
        supplier = Supplier.objects.create(**supplier_data)
        print(f"[Sync] Created supplier {supplier_id} for business {business.id}")
        
        return {
            'success': True,
            'server_id': str(supplier.id),
            'business_id': str(business.id)
        }
        
    except Exception as e:
        print(f"[Sync] Error creating supplier: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


def handle_update_supplier(supplier_id, data, business):
    """Handle update of supplier from frontend"""
    try:
        if _business_eis_enabled(business):
            return _eis_managed_rejection()

        supplier = Supplier.objects.get(id=supplier_id, business=business)
        
        # Update fields if provided
        if 'name' in data:
            supplier.name = data['name']
        if 'contact_person' in data or 'contactPerson' in data:
            supplier.contact_person = _payload_get(data, 'contact_person', 'contactPerson') or ''
        if 'email' in data:
            supplier.email = data['email']
        if 'phone' in data:
            supplier.phone = data['phone']
        if 'address' in data:
            supplier.address = data['address']
        if 'city' in data:
            supplier.city = data['city']
        if 'region' in data or 'region_state' in data or 'regionState' in data or 'state' in data:
            supplier.region = _payload_get(data, 'region', 'region_state', 'regionState', 'state') or ''
        if 'country' in data:
            supplier.country = data['country']
        if 'is_active' in data:
            supplier.is_active = data['is_active']
        if 'supplier_tin' in data or 'supplierTin' in data:
            supplier_tin = data.get('supplier_tin')
            if supplier_tin is None:
                supplier_tin = data.get('supplierTin')
            supplier.supplier_tin = supplier_tin if supplier_tin not in ('', None) else None
        if 'mra_supplier_id' in data or 'mraSupplierId' in data:
            mra_supplier_id = data.get('mra_supplier_id')
            if mra_supplier_id is None:
                mra_supplier_id = data.get('mraSupplierId')
            supplier.mra_supplier_id = _parse_positive_int(mra_supplier_id)
        if 'vat_registered' in data or 'vatRegistered' in data:
            vat_registered = data.get('vat_registered')
            if vat_registered is None:
                vat_registered = data.get('vatRegistered')
            supplier.vat_registered = _parse_boolean(vat_registered, default=False)
        
        # Handle payment application to outstanding orders
        if 'apply_payment' in data and data['apply_payment']:
            payment_amount = float(data.get('payment_amount', 0))
            print(f"[Sync] Applying payment of {payment_amount} to supplier {supplier_id}")
            
            # Get all unpaid/partially paid purchase orders for this supplier
            outstanding_orders = supplier.purchase_orders.exclude(
                payment_status='Paid'
            ).order_by('created_at')
            
            remaining_payment = payment_amount
            orders_updated = 0
            
            for order in outstanding_orders:
                if remaining_payment <= 0:
                    break
                
                # Calculate how much to pay on this order
                payment_on_order = min(remaining_payment, order.amount_due)
                
                # Update order payment
                order.amount_paid += payment_on_order
                order.amount_due -= payment_on_order
                
                # Update payment status
                if order.amount_due <= 0:
                    order.payment_status = 'Paid'
                    order.amount_due = 0
                elif order.amount_paid > 0:
                    order.payment_status = 'Partial'
                
                order.save()
                remaining_payment -= payment_on_order
                orders_updated += 1
                print(f"[Sync] Updated order {order.id}: paid={order.amount_paid}, due={order.amount_due}")
            
            print(f"[Sync] Applied payment to {orders_updated} orders, remaining: {remaining_payment}")
        
        # Update payment tracking fields
        if 'total_amount_due' in data:
            supplier.total_amount_due = float(data['total_amount_due'])
        if 'total_amount_paid' in data:
            supplier.total_amount_paid = float(data['total_amount_paid'])
        
        supplier.save()
        print(f"[Sync] Updated supplier {supplier_id}")
        
        return {
            'success': True,
            'server_id': str(supplier.id),
            'balance_due': float(supplier.get_balance_due()),
            'total_amount_due': float(supplier.total_amount_due),
            'total_amount_paid': float(supplier.total_amount_paid)
        }
        
    except Supplier.DoesNotExist:
        print(f"[Sync] Supplier {supplier_id} not found, creating instead")
        return handle_create_supplier(supplier_id, data, business)
    except Exception as e:
        print(f"[Sync] Error updating supplier: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


def handle_delete_supplier(supplier_id, business):
    """Handle deletion of supplier from frontend"""
    try:
        if _business_eis_enabled(business):
            return _eis_managed_rejection()

        supplier = Supplier.objects.get(id=supplier_id, business=business)
        supplier.delete()
        print(f"[Sync] Deleted supplier {supplier_id}")
        
        return {
            'success': True,
            'server_id': supplier_id
        }
        
    except Supplier.DoesNotExist:
        print(f"[Sync] Supplier {supplier_id} not found for deletion")
        return {
            'success': True,
            'server_id': supplier_id
        }
    except Exception as e:
        print(f"[Sync] Error deleting supplier: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }
