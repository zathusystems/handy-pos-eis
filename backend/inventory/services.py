"""
MRA-Compliant Inventory Service

Provides safe inventory operations that maintain MRA compliance:
- Product validation
- Inventory snapshots
- Stock reduction with validation
- Waste approval workflow
- Audit trail creation
"""

from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models import (
    InventoryItem, MRAProductMapping, InventorySnapshot,
    WasteRecord, StockTransfer, AuditLog
)


class InventoryService:
    """Service for MRA-compliant inventory operations"""

    @staticmethod
    def _business_eis_enabled(business) -> bool:
        settings_obj = getattr(business, 'settings', None)
        return bool(getattr(settings_obj, 'enable_eis', False) or getattr(business, 'mra_enrolled', False))

    @staticmethod
    def _branch_eis_site_id(branch) -> str:
        return str(
            getattr(branch, 'mra_site_id', '')
            or getattr(branch, 'mra_branch_code', '')
            or ''
        ).strip()

    @staticmethod
    def _validate_eis_branch_transfer_mapping(from_branch, to_branch, business) -> None:
        if not InventoryService._business_eis_enabled(business):
            return

        missing = []
        if not InventoryService._branch_eis_site_id(from_branch):
            missing.append(f'source branch "{from_branch.name}"')
        if not InventoryService._branch_eis_site_id(to_branch):
            missing.append(f'destination branch "{to_branch.name}"')
        if missing:
            raise ValidationError(
                'EIS branch transfer requires MRA site mapping for '
                + ' and '.join(missing)
                + '. Activate/map the branch before transferring stock.'
            )

    @staticmethod
    def submit_eis_transfer_if_required(from_branch, to_branch, inventory_item, quantity):
        """Submit branch/site inventory transfers to MRA when EIS is enabled."""
        business = inventory_item.business
        if not InventoryService._business_eis_enabled(business):
            return None

        InventoryService._validate_eis_branch_transfer_mapping(
            from_branch,
            to_branch,
            business,
        )

        barcode = str(
            getattr(inventory_item, 'product_code', '')
            or getattr(inventory_item, 'barcode', '')
            or getattr(inventory_item, 'sku', '')
            or ''
        ).strip()
        if not barcode:
            raise ValidationError(
                f'EIS transfer requires a product code/barcode for "{inventory_item.name}".'
            )

        from mra_eis.models import Terminal
        from mra_eis.services import ProductMappingService

        terminal = (
            Terminal.objects.filter(business=business, branch=from_branch, status='active')
            .order_by('-updated_at', '-created_at')
            .first()
            or Terminal.objects.filter(business=business, status='active')
            .order_by('-updated_at', '-created_at')
            .first()
        )
        if not terminal:
            raise ValidationError('Activate an EIS terminal before transferring stock.')

        transfer_item = {
            'barcode': barcode,
            'quantity': quantity,
        }
        price = getattr(inventory_item, 'price', None)
        if price not in (None, ''):
            transfer_item['price'] = price

        return ProductMappingService.transfer_inventory(
            business=business,
            terminal=terminal,
            items=[transfer_item],
            to_branch=to_branch,
            from_site_id=InventoryService._branch_eis_site_id(from_branch),
            from_warehouse_to_site=False,
        )

    @staticmethod
    def validate_product_for_sale(inventory_item):
        """
        Validate that a product is ready for MRA sale.
        
        Raises:
            ValidationError: If product is not MRA-ready
        """
        if inventory_item.item_type != 'sellable':
            raise ValidationError(
                f"Product {inventory_item.name} is not sellable"
            )
        
        if not hasattr(inventory_item, 'mra_mapping'):
            raise ValidationError(
                f"Product {inventory_item.name} has no MRA mapping"
            )
        
        mapping = inventory_item.mra_mapping
        
        if not mapping.is_approved:
            raise ValidationError(
                f"Product {inventory_item.name} is not MRA-approved"
            )
        
        if not mapping.mra_synced:
            raise ValidationError(
                f"Product {inventory_item.name} is not synced with MRA"
            )
        
        if inventory_item.price_locked and inventory_item.price is None:
            raise ValidationError(
                f"Product {inventory_item.name} has locked price but no price set"
            )
        
        return mapping

    @staticmethod
    @transaction.atomic
    def create_inventory_snapshot(
        inventory_item,
        quantity_sold,
        related_invoice_number,
        related_order_id=None,
        user=None
    ):
        """
        Create an inventory snapshot for a sale.
        
        This is CRITICAL for MRA audit trail.
        
        Args:
            inventory_item: InventoryItem instance
            quantity_sold: Decimal quantity
            related_invoice_number: Invoice number
            related_order_id: Optional POS order ID
            user: User performing operation
        
        Returns:
            InventorySnapshot instance
        
        Raises:
            ValidationError: If validation fails
        """
        # Validate product
        mapping = InventoryService.validate_product_for_sale(inventory_item)
        
        # Validate quantity
        if quantity_sold <= 0:
            raise ValidationError("Quantity sold must be positive")
        
        if quantity_sold > inventory_item.stock_units:
            raise ValidationError(
                f"Insufficient stock. Available: {inventory_item.stock_units}, "
                f"Requested: {quantity_sold}"
            )
        
        # Calculate after-sale quantity
        quantity_after_sale = inventory_item.stock_units - quantity_sold
        
        # Create snapshot
        snapshot = InventorySnapshot.objects.create(
            inventory_item=inventory_item,
            branch=inventory_item.branch,
            quantity_before_sale=inventory_item.stock_units,
            quantity_sold=quantity_sold,
            quantity_after_sale=quantity_after_sale,
            related_invoice_number=related_invoice_number,
            related_order_id=related_order_id or "",
            product_price=inventory_item.price,
            product_tax_rate=mapping.mra_tax_rate,
            product_tax_type=mapping.mra_tax_type,
        )
        
        # Log to audit
        AuditLog.objects.create(
            business=inventory_item.business,
            branch=inventory_item.branch,
            user=user,
            action_type='STOCK_RECEIVE',
            entity_type='InventorySnapshot',
            entity_id=str(snapshot.id),
            details={
                'quantity_sold': str(quantity_sold),
                'stock_before': str(inventory_item.stock_units),
                'stock_after': str(quantity_after_sale),
            },
            mra_related=True,
            mra_reference=related_invoice_number,
        )
        
        return snapshot

    @staticmethod
    @transaction.atomic
    def reduce_stock(
        inventory_item,
        quantity,
        reason='SALE',
        user=None
    ):
        """
        Reduce inventory stock with validation.
        
        Args:
            inventory_item: InventoryItem instance
            quantity: Decimal quantity to reduce
            reason: Reason for reduction
            user: User performing operation
        
        Raises:
            ValidationError: If validation fails
        """
        if quantity <= 0:
            raise ValidationError("Quantity must be positive")
        
        if quantity > inventory_item.stock_units:
            raise ValidationError(
                f"Cannot reduce by {quantity}. "
                f"Only {inventory_item.stock_units} available."
            )
        
        # Reduce stock
        old_stock = inventory_item.stock_units
        inventory_item.stock_units -= quantity
        inventory_item.update_status()
        
        # Log to audit
        AuditLog.objects.create(
            business=inventory_item.business,
            branch=inventory_item.branch,
            user=user,
            action_type='STOCK_RECEIVE',
            entity_type='InventoryItem',
            entity_id=str(inventory_item.id),
            details={
                'reason': reason,
                'quantity_reduced': str(quantity),
                'stock_before': str(old_stock),
                'stock_after': str(inventory_item.stock_units),
            },
        )

    @staticmethod
    @transaction.atomic
    def record_waste(
        inventory_item,
        quantity,
        reason,
        cost,
        notes="",
        approved_by=None,
        user=None
    ):
        """
        Record waste with approval workflow.
        
        Args:
            inventory_item: InventoryItem instance
            quantity: Decimal quantity wasted
            reason: Waste reason (Expired, Damaged, etc.)
            cost: Decimal cost of waste
            notes: Optional notes
            approved_by: Manager/Auditor name
            user: User recording waste
        
        Returns:
            WasteRecord instance
        
        Raises:
            ValidationError: If validation fails
        """
        if quantity <= 0:
            raise ValidationError("Quantity must be positive")

        InventoryService._validate_eis_branch_transfer_mapping(
            from_branch,
            to_branch,
            inventory_item.business,
        )
        
        if quantity > inventory_item.stock_units:
            raise ValidationError(
                f"Cannot waste {quantity}. "
                f"Only {inventory_item.stock_units} available."
            )
        
        if cost < 0:
            raise ValidationError("Cost cannot be negative")
        
        # Create waste record
        waste = WasteRecord.objects.create(
            business=inventory_item.business,
            branch=inventory_item.branch,
            inventory_item=inventory_item,
            quantity=quantity,
            cost=cost,
            reason=reason,
            notes=notes,
            affects_tax=True,  # Waste affects tax
            approved_by=approved_by or "",
            recorded_by=user.email if user else "system",
        )
        
        # Reduce stock
        InventoryService.reduce_stock(
            inventory_item,
            quantity,
            reason=f'WASTE_{reason}',
            user=user
        )
        
        # Log to audit
        AuditLog.objects.create(
            business=inventory_item.business,
            branch=inventory_item.branch,
            user=user,
            action_type='STOCK_WASTE',
            entity_type='WasteRecord',
            entity_id=str(waste.id),
            details={
                'reason': reason,
                'quantity': str(quantity),
                'cost': str(cost),
                'approved_by': approved_by or "pending",
            },
            mra_related=True,
        )
        
        return waste

    @staticmethod
    @transaction.atomic
    def transfer_stock(
        from_branch,
        to_branch,
        inventory_item,
        quantity,
        transfer_reference,
        user=None
    ):
        """
        Transfer stock between branches with MRA tracking.
        
        Args:
            from_branch: Source branch
            to_branch: Destination branch
            inventory_item: InventoryItem instance
            quantity: Decimal quantity
            transfer_reference: Unique reference
            user: User performing transfer
        
        Returns:
            StockTransfer instance
        
        Raises:
            ValidationError: If validation fails
        """
        if quantity <= 0:
            raise ValidationError("Quantity must be positive")
        
        if quantity > inventory_item.stock_units:
            raise ValidationError(
                f"Cannot transfer {quantity}. "
                f"Only {inventory_item.stock_units} available."
            )

        eis_transfer_result = InventoryService.submit_eis_transfer_if_required(
            from_branch,
            to_branch,
            inventory_item,
            quantity,
        )
        
        # Create transfer record
        transfer = StockTransfer.objects.create(
            business=inventory_item.business,
            from_branch=from_branch,
            to_branch=to_branch,
            inventory_item=inventory_item,
            quantity=quantity,
            transfer_reference=transfer_reference,
            mra_notified=bool(eis_transfer_result),
            initiated_by=user.email if user else "system",
        )
        
        # Reduce stock from source
        InventoryService.reduce_stock(
            inventory_item,
            quantity,
            reason='TRANSFER_OUT',
            user=user
        )

        destination_item, created = InventoryItem.objects.get_or_create(
            business=inventory_item.business,
            branch=to_branch,
            name=inventory_item.name,
            defaults={
                'category': inventory_item.category,
                'item_type': inventory_item.item_type,
                'stock_units': quantity,
                'unit_type': inventory_item.unit_type,
                'reorder_level': inventory_item.reorder_level,
                'status': 'In Stock' if quantity > 0 else 'Out of Stock',
                'cost': inventory_item.cost,
                'price': inventory_item.price,
                'value': quantity * (inventory_item.cost or 0),
                'is_variable_price': inventory_item.is_variable_price,
                'is_fuel': getattr(inventory_item, 'is_fuel', False),
                'is_oil': getattr(inventory_item, 'is_oil', False),
                'supplier': inventory_item.supplier,
                'manufacturer': inventory_item.manufacturer,
                'batch': inventory_item.batch,
                'brand': inventory_item.brand,
                'expiry': inventory_item.expiry,
                'sku': inventory_item.sku,
                'barcode': inventory_item.barcode,
            },
        )
        if not created:
            destination_item.stock_units += quantity
            destination_item.value = destination_item.stock_units * (destination_item.cost or 0)
            destination_item.update_status()
            destination_item.save(update_fields=['stock_units', 'value', 'status'])
        
        # Log to audit
        AuditLog.objects.create(
            business=inventory_item.business,
            branch=from_branch,
            user=user,
            action_type='STOCK_TRANSFER',
            entity_type='StockTransfer',
            entity_id=str(transfer.id),
            details={
                'from_branch': from_branch.name,
                'to_branch': to_branch.name,
                'quantity': str(quantity),
                'reference': transfer_reference,
            },
            mra_related=True,
            mra_reference=transfer_reference,
        )
        
        return transfer

    @staticmethod
    def get_product_traceability(inventory_item):
        """
        Get complete traceability for a product.
        
        Returns all snapshots, waste, and transfers.
        """
        snapshots = InventorySnapshot.objects.filter(
            inventory_item=inventory_item
        ).order_by('-created_at')
        
        waste = WasteRecord.objects.filter(
            inventory_item=inventory_item
        ).order_by('-recorded_at')
        
        transfers = StockTransfer.objects.filter(
            inventory_item=inventory_item
        ).order_by('-created_at')
        
        return {
            'snapshots': snapshots,
            'waste': waste,
            'transfers': transfers,
        }

    @staticmethod
    def get_invoice_traceability(invoice_number):
        """
        Get all inventory operations for an invoice.
        
        This is what MRA auditors will query.
        """
        snapshots = InventorySnapshot.objects.filter(
            related_invoice_number=invoice_number
        ).order_by('created_at')
        
        audit_logs = AuditLog.objects.filter(
            mra_reference=invoice_number
        ).order_by('-created_at')
        
        return {
            'snapshots': snapshots,
            'audit_logs': audit_logs,
        }

    @staticmethod
    def validate_stock_consistency(branch):
        """
        Validate stock consistency for a branch.
        
        Checks for:
        - Negative stock
        - Orphaned snapshots
        - Unmatched transfers
        """
        issues = []
        
        # Check for negative stock
        negative_items = InventoryItem.objects.filter(
            branch=branch,
            stock_units__lt=0
        )
        if negative_items.exists():
            issues.append(f"Found {negative_items.count()} items with negative stock")
        
        # Check for orphaned snapshots
        orphaned_snapshots = InventorySnapshot.objects.filter(
            branch=branch,
        ).exclude(
            related_invoice_number__in=[
                'INV-' + str(i) for i in range(10000)
            ]
        )
        if orphaned_snapshots.exists():
            issues.append(f"Found {orphaned_snapshots.count()} orphaned snapshots")
        
        return {
            'is_consistent': len(issues) == 0,
            'issues': issues,
        }


class InventoryAuditService:
    """Service for MRA audit queries"""

    @staticmethod
    def get_sales_by_product(inventory_item, start_date=None, end_date=None):
        """Get all sales of a product"""
        snapshots = InventorySnapshot.objects.filter(
            inventory_item=inventory_item
        )
        
        if start_date:
            snapshots = snapshots.filter(created_at__gte=start_date)
        
        if end_date:
            snapshots = snapshots.filter(created_at__lte=end_date)
        
        return snapshots.order_by('created_at')

    @staticmethod
    def get_waste_records(branch, start_date=None, end_date=None):
        """Get all waste records for a branch"""
        waste = WasteRecord.objects.filter(branch=branch)
        
        if start_date:
            waste = waste.filter(recorded_at__gte=start_date)
        
        if end_date:
            waste = waste.filter(recorded_at__lte=end_date)
        
        return waste.order_by('-recorded_at')

    @staticmethod
    def get_stock_transfers(business, start_date=None, end_date=None):
        """Get all stock transfers for a business"""
        transfers = StockTransfer.objects.filter(business=business)
        
        if start_date:
            transfers = transfers.filter(created_at__gte=start_date)
        
        if end_date:
            transfers = transfers.filter(created_at__lte=end_date)
        
        return transfers.order_by('-created_at')

    @staticmethod
    def get_audit_trail(business, entity_type=None, start_date=None, end_date=None):
        """Get audit trail for a business"""
        logs = AuditLog.objects.filter(business=business)
        
        if entity_type:
            logs = logs.filter(entity_type=entity_type)
        
        if start_date:
            logs = logs.filter(created_at__gte=start_date)
        
        if end_date:
            logs = logs.filter(created_at__lte=end_date)
        
        return logs.order_by('-created_at')

    @staticmethod
    def verify_tax_calculation(snapshot):
        """Verify tax was calculated correctly for a snapshot"""
        if snapshot.product_tax_type == 'exempt':
            expected_tax = Decimal('0')
        elif snapshot.product_tax_type == 'zero':
            expected_tax = Decimal('0')
        else:
            # Standard rate
            expected_tax = (
                snapshot.quantity_sold *
                snapshot.product_price *
                (snapshot.product_tax_rate / Decimal('100'))
            )
        
        return {
            'quantity_sold': snapshot.quantity_sold,
            'product_price': snapshot.product_price,
            'tax_rate': snapshot.product_tax_rate,
            'tax_type': snapshot.product_tax_type,
            'expected_tax': expected_tax,
        }
