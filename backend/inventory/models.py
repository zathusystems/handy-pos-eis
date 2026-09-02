"""
MRA EIS-Certified Inventory Models

This module provides MRA-compliant inventory management with:
- Explicit MRA product mapping
- Immutable pricing and tax rules
- Complete traceability from stock to invoice
- Inventory snapshots for audit
- Supplier compliance tracking
- Waste and transfer audit trails
"""

import uuid
from decimal import Decimal
from django.db import models
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.validators import MinValueValidator
from business.models import Business, Branch

User = get_user_model()


# ============================================================================
# SUPPLIER MANAGEMENT (Enhanced for MRA)
# ============================================================================

class Supplier(models.Model):
    """Supplier model with MRA compliance tracking"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='suppliers')
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    
    # MRA Compliance Fields
    supplier_tin = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Supplier's Tax Identification Number (for VAT reclaim)"
    )
    mra_supplier_id = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Supplier ID assigned by MRA EIS, required for goods receiving when available"
    )
    vat_registered = models.BooleanField(
        default=False,
        help_text="Is supplier VAT registered?"
    )
    
    # Payment tracking
    total_amount_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        unique_together = ('business', 'name')
        indexes = [
            models.Index(fields=['business', 'is_active']),
            models.Index(fields=['supplier_tin']),
            models.Index(fields=['mra_supplier_id'], name='inventory_s_mra_sup_fa42c2_idx'),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return self.name

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def get_balance_due(self):
        """Calculate balance due for this supplier"""
        return self.total_amount_due - self.total_amount_paid

    def get_total_purchase_orders_amount(self):
        """Calculate total amount from all purchase orders"""
        from django.db.models import Sum
        total = self.purchase_orders.aggregate(
            total=Sum('total_cost')
        )['total'] or 0
        return total

    def get_unpaid_purchase_orders_amount(self):
        """Calculate total amount due from unpaid purchase orders"""
        from django.db.models import Sum
        total = self.purchase_orders.exclude(
            payment_status='Paid'
        ).aggregate(
            total=Sum('amount_due')
        )['total'] or 0
        return total


# ============================================================================
# MRA PRODUCT MAPPING (NEW - CRITICAL FOR CERTIFICATION)
# ============================================================================

class MRAProductMapping(models.Model):
    """
    Maps internal inventory items to MRA-approved products.
    This is CRITICAL for MRA certification.
    """
    TAX_TYPES = [
        ('standard', 'Standard Rated'),
        ('zero', 'Zero Rated (0%)'),
        ('exempt', 'Exempt'),
    ]

    UNIT_MEASURES = [
        ('unit', 'Unit'),
        ('kg', 'Kilogram'),
        ('liter', 'Liter'),
        ('meter', 'Meter'),
        ('box', 'Box'),
        ('pack', 'Pack'),
        ('bottle', 'Bottle'),
        ('can', 'Can'),
        ('carton', 'Carton'),
    ]

    TAX_CALCULATION_METHODS = [
        ('inclusive', 'Tax Inclusive (Price includes tax)'),
        ('exclusive', 'Tax Exclusive (Tax added to price)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inventory_item = models.OneToOneField(
        'InventoryItem',
        on_delete=models.CASCADE,
        related_name='mra_mapping',
        help_text="Link to internal inventory item"
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name='mra_mappings',
        null=True,
        blank=True,
        help_text="Branch this mapping belongs to"
    )
    
    # MRA Product Information (IMMUTABLE)
    mra_product_code = models.CharField(
        max_length=100,
        help_text="MRA-assigned product code",
        null=True,
        blank=True
    )
    mra_product_name = models.CharField(
        max_length=255,
        help_text="MRA-approved product name",
        null=True,
        blank=True
    )
    mra_tax_type = models.CharField(
        max_length=20,
        choices=TAX_TYPES,
        help_text="MRA tax classification"
    )
    mra_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="MRA tax rate (immutable)"
    )
    mra_unit_measure = models.CharField(
        max_length=20,
        choices=UNIT_MEASURES,
        default='unit',
        help_text="MRA unit of measure"
    )
    
    # Tax Calculation Method (NEW - IMMUTABLE)
    tax_calculation_method = models.CharField(
        max_length=20,
        choices=TAX_CALCULATION_METHODS,
        default='inclusive',
        help_text="How is tax calculated for this product? (Immutable once approved)"
    )
    mra_levies = models.JSONField(
        default=list,
        blank=True,
        help_text="MRA levy metadata for this product, normalized as levyTypeId/levyRate rows"
    )
    is_product = models.BooleanField(
        default=True,
        help_text="True for physical products; false for MRA service items that do not carry stock."
    )
    # Approval Status
    is_approved = models.BooleanField(
        default=False,
        help_text="Has MRA approved this mapping?"
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When was this mapping approved?"
    )
    
    # Sync Status
    mra_synced = models.BooleanField(
        default=False,
        help_text="Has this been synced to MRA?"
    )
    last_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last sync timestamp"
    )
    
    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['mra_product_code']),
            models.Index(fields=['is_approved']),
            models.Index(fields=['mra_synced']),
        ]

    def __str__(self):
        display_name = self.mra_product_name or 'Unassigned MRA Product'
        display_code = self.mra_product_code or 'No Code'
        return f"{display_name} ({display_code})"

    def uses_standard_vat(self):
        """Return True when the MRA mapping is standard VAT with a positive rate."""
        try:
            tax_rate = Decimal(str(self.mra_tax_rate or 0))
        except Exception:
            tax_rate = Decimal('0')
        tax_type = str(self.mra_tax_type or '').strip().lower()
        return tax_type not in {
            'zero',
            'zero_rated',
            'zero-rated',
            'vat_zero',
            'exempt',
            'vat_exempt',
        } and tax_rate > 0

    def is_taxpayer_compatible(self):
        """Return True for MRA-approved product tax metadata.

        MRA confirmed non-VAT taxpayers can still submit EIS sales. The POS must
        preserve the tax metadata returned by MRA terminal-site products instead
        of blocking locally based on taxpayer VAT status.
        """
        return True

    def taxpayer_compatibility_error(self):
        """Compatibility warning placeholder retained for API stability."""
        return ''

    def is_ready_for_sale(self):
        """Check if product is ready to be sold"""
        return self.is_approved and self.mra_synced


# ============================================================================
# INVENTORY ITEM (Enhanced for MRA)
# ============================================================================

class InventoryItem(models.Model):
    """Main inventory item model with MRA compliance"""
    ITEM_TYPES = [
        ('ingredient', 'Ingredient'),
        ('sellable', 'Sellable Product'),
    ]

    STATUS_CHOICES = [
        ('In Stock', 'In Stock'),
        ('Low Stock', 'Low Stock'),
        ('Out of Stock', 'Out of Stock'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='inventory_items')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='inventory_items')
    
    # Basic info
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=100)
    item_type = models.CharField(max_length=20, choices=ITEM_TYPES)
    
    # Stock info
    stock_units = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=0,
        validators=[MinValueValidator(Decimal('0'))]
    )
    unit_type = models.CharField(max_length=50, blank=True)
    reorder_level = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='In Stock')
    
    # Pricing (with MRA controls)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_variable_price = models.BooleanField(default=False)
    is_fuel = models.BooleanField(default=False)
    is_oil = models.BooleanField(default=False)
    
    # MRA Price Control (NEW)
    price_locked = models.BooleanField(
        default=False,
        help_text="Is price locked by MRA? (prevents local changes)"
    )
    tax_locked = models.BooleanField(
        default=True,
        help_text="Is tax rate locked? (should always be True for sellables)"
    )
    
    # Supplier & Batch info
    supplier = models.CharField(max_length=255, blank=True, null=True)
    manufacturer = models.CharField(max_length=255, blank=True, null=True)
    batch = models.CharField(max_length=100, blank=True, null=True)
    expiry = models.DateField(null=True, blank=True)
    
    # Business-type specific fields
    sku = models.CharField(max_length=100, blank=True, null=True)
    barcode = models.CharField(max_length=100, blank=True, null=True)
    product_code = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
        help_text="Unique product code for identification"
    )
    
    # Restaurant/Bar fields
    is_recipe_ingredient = models.BooleanField(default=False)
    is_produced = models.BooleanField(default=False)
    
    # Bar & Liquor specific fields
    is_sold_in_portions = models.BooleanField(default=False)
    portion_name = models.CharField(max_length=50, blank=True, null=True)
    portions_per_unit = models.IntegerField(null=True, blank=True)
    
    # Beauty Salon fields
    brand = models.CharField(max_length=255, blank=True, null=True)
    
    # Recipe (for sellable items)
    recipe = models.JSONField(default=list, blank=True)
    
    # Menu image
    image = models.TextField(blank=True, null=True)
    
    # Metadata
    on_menu = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        unique_together = ('business', 'branch', 'name')
        indexes = [
            models.Index(fields=['business', 'branch']),
            models.Index(fields=['status']),
            models.Index(fields=['item_type']),
            models.Index(fields=['barcode']),
            models.Index(fields=['sku']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.name} ({self.branch.name})"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def update_status(self):
        """Update stock status based on current stock levels"""
        if self.stock_units > self.reorder_level:
            self.status = 'In Stock'
        elif self.stock_units > 0:
            self.status = 'Low Stock'
        else:
            self.status = 'Out of Stock'
        self.save()

    def is_mra_ready(self):
        """Check if item is ready for MRA sales"""
        if self.item_type != 'sellable':
            return False
        try:
            mapping = self.mra_mapping
        except ObjectDoesNotExist:
            return False

        if mapping is None:
            return False

        return mapping.is_ready_for_sale()

    def get_available_portions(self):
        """Calculate available portions for Bar & Liquor items"""
        if self.is_sold_in_portions and self.portions_per_unit and self.portions_per_unit > 0:
            return int(self.stock_units * self.portions_per_unit)
        return None

    def get_portion_info(self):
        """Get portion information for Bar & Liquor items"""
        if self.is_sold_in_portions:
            return {
                'portion_name': self.portion_name,
                'portions_per_unit': self.portions_per_unit,
                'available_portions': self.get_available_portions(),
                'full_units': self.stock_units,
            }
        return None


# ============================================================================
# PURCHASE ORDER (Enhanced for MRA)
# ============================================================================

class PurchaseOrder(models.Model):
    """Purchase order with MRA compliance tracking"""
    EIS_STOCK_RECEIPT_SOURCE_POS = 'pos_goods_receiving'
    EIS_STOCK_RECEIPT_SOURCE_SUPPLIER = 'supplier_sale'
    EIS_STOCK_RECEIPT_SOURCE_CHOICES = [
        (EIS_STOCK_RECEIPT_SOURCE_POS, 'POS submits informal/manual purchase to EIS'),
        (EIS_STOCK_RECEIPT_SOURCE_SUPPLIER, 'B2B stock transfer already posted in EIS'),
    ]

    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Pending', 'Pending Approval'),
        ('Approved', 'Approved'),
        ('Received', 'Partially/Fully Received'),
        ('Completed', 'Completed'),
        ('Cancelled', 'Cancelled'),
    ]

    PAYMENT_STATUS = [
        ('Unpaid', 'Unpaid'),
        ('Partial', 'Partially Paid'),
        ('Paid', 'Paid'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='purchase_orders')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='purchase_orders')
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, related_name='purchase_orders', blank=True)
    
    # Order info
    order_number = models.UUIDField(unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    
    # Totals
    total_items = models.IntegerField(default=0)
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Payment
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='Unpaid')
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reference_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Supplier invoice or reference number"
    )
    vat_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="VAT amount for this purchase"
    )
    
    # MRA Compliance Fields (NEW)
    supplier_tin = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Supplier TIN for VAT reclaim"
    )
    mra_supplier_id = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="Supplier ID assigned by MRA EIS at the time stock was received"
    )
    supplier_vat_registered = models.BooleanField(
        default=False,
        help_text="Is supplier VAT registered?"
    )
    eis_stock_receipt_source = models.CharField(
        max_length=32,
        choices=EIS_STOCK_RECEIPT_SOURCE_CHOICES,
        default=EIS_STOCK_RECEIPT_SOURCE_POS,
        help_text="Controls whether POS submits receive-stock to EIS or records only local batch details because a B2B EIS stock transfer already updated MRA stock.",
    )
    
    # Notes
    notes = models.TextField(blank=True)
    
    # Dates
    created_by = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    received_date = models.DateTimeField(null=True, blank=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'branch']),
            models.Index(fields=['supplier']),
            models.Index(fields=['status']),
            models.Index(fields=['supplier_tin']),
            models.Index(fields=['mra_supplier_id'], name='inventory_p_mra_sup_3d9da3_idx'),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"PO-{self.order_number} ({self.supplier.name if self.supplier else 'N/A'})"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def calculate_totals(self):
        """Recalculate order totals from items"""
        items = self.items.all()
        self.total_items = items.count()
        self.total_cost = sum(item.total_cost for item in items)
        self.amount_due = self.total_cost - self.amount_paid
        self.save()


class PurchaseOrderItem(models.Model):
    """Individual items in a purchase order"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='purchase_order_items')
    
    # Session tracking (NEW - for session-connected stock tracking)
    session_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Session ID when stock was received"
    )
    
    # Quantity tracking
    quantity_ordered = models.DecimalField(max_digits=12, decimal_places=3)
    quantity_received = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    quantity_remaining = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    
    # Cost info
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2)
    total_cost = models.DecimalField(max_digits=12, decimal_places=2)

    # VAT info (per-item)
    TAX_CALCULATION_METHOD = [
        ('inclusive', 'Inclusive'),
        ('exclusive', 'Exclusive'),
    ]
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    tax_calculation_method = models.CharField(
        max_length=10,
        choices=TAX_CALCULATION_METHOD,
        default='exclusive'
    )
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    
    # Batch & Expiry
    batch_number = models.CharField(max_length=100, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['purchase_order']),
            models.Index(fields=['inventory_item']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.inventory_item.name} - {self.quantity_ordered} units"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def save(self, *args, **kwargs):
        """Auto-calculate total cost"""
        self.total_cost = self.quantity_ordered * self.cost_per_unit
        tax_rate = self.tax_rate or Decimal('0')
        if tax_rate:
            if self.tax_calculation_method == 'inclusive':
                divisor = Decimal('1') + (tax_rate / Decimal('100'))
                self.tax_amount = self.total_cost - (self.total_cost / divisor) if divisor != 0 else Decimal('0')
            else:
                self.tax_amount = self.total_cost * (tax_rate / Decimal('100'))
        else:
            self.tax_amount = Decimal('0')
        super().save(*args, **kwargs)


# ============================================================================
# INVENTORY SNAPSHOT (NEW - CRITICAL FOR MRA AUDIT)
# ============================================================================

class InventorySnapshot(models.Model):
    """
    Point-in-time inventory state when a sale is made.
    This is CRITICAL for MRA audit trail.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inventory_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.CASCADE,
        related_name='snapshots'
    )
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='inventory_snapshots')
    
    # Snapshot data (immutable)
    quantity_before_sale = models.DecimalField(max_digits=12, decimal_places=3)
    quantity_sold = models.DecimalField(max_digits=12, decimal_places=3)
    quantity_after_sale = models.DecimalField(max_digits=12, decimal_places=3)
    
    # MRA Traceability
    related_invoice_number = models.CharField(
        max_length=100,
        help_text="Invoice number this snapshot is linked to"
    )
    related_order_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="POS order ID"
    )
    
    # Product state at time of sale
    product_price = models.DecimalField(max_digits=10, decimal_places=2)
    product_tax_rate = models.DecimalField(max_digits=5, decimal_places=2)
    product_tax_type = models.CharField(max_length=20)
    
    # Audit
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['inventory_item', 'branch']),
            models.Index(fields=['related_invoice_number']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"Snapshot: {self.inventory_item.name} - Invoice {self.related_invoice_number}"


# ============================================================================
# STOCK TRANSFER (Enhanced for MRA)
# ============================================================================

class StockTransfer(models.Model):
    """Records of stock transfers between branches with MRA tracking"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='stock_transfers')
    from_branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='transfers_out')
    to_branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='transfers_in')
    
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='transfers')
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    
    # MRA Compliance (NEW)
    transfer_reference = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        help_text="Unique transfer reference for MRA"
    )
    mra_notified = models.BooleanField(
        default=False,
        help_text="Has MRA been notified of this transfer?"
    )
    
    initiated_by = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['from_branch', 'created_at']),
            models.Index(fields=['to_branch', 'created_at']),
            models.Index(fields=['transfer_reference']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.inventory_item.name}: {self.from_branch.name} → {self.to_branch.name}"


# ============================================================================
# WASTE RECORD (Enhanced for MRA)
# ============================================================================

class WasteRecord(models.Model):
    """Records of wasted/spoiled inventory with MRA compliance"""
    WASTE_REASONS = [
        ('Expired', 'Expired'),
        ('Damaged', 'Damaged'),
        ('Spoilage', 'Spoilage'),
        ('Error', 'Error'),
        ('Other', 'Other'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='waste_records')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='waste_records')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='waste_records')
    purchase_order_item = models.ForeignKey(PurchaseOrderItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='waste_records')
    
    # Session tracking (NEW - for session-connected waste tracking)
    session_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Session ID when waste was recorded"
    )
    
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit = models.CharField(max_length=50, blank=True)
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    
    reason = models.CharField(max_length=20, choices=WASTE_REASONS)
    notes = models.TextField(blank=True)
    
    # MRA Compliance (NEW)
    affects_tax = models.BooleanField(
        default=False,
        help_text="Does this waste affect tax reporting?"
    )
    approved_by = models.CharField(
        max_length=255,
        blank=True,
        help_text="Manager/Auditor who approved this waste"
    )
    
    recorded_by = models.CharField(max_length=255)
    recorded_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-recorded_at']
        indexes = [
            models.Index(fields=['branch', 'recorded_at']),
            models.Index(fields=['reason']),
            models.Index(fields=['affects_tax']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"Waste: {self.inventory_item.name} - {self.quantity} {self.unit}"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def save(self, *args, **kwargs):
        """Ensure waste never creates negative stock"""
        if self.quantity > self.inventory_item.stock_units:
            raise ValueError(
                f"Cannot waste {self.quantity} units. "
                f"Only {self.inventory_item.stock_units} available."
            )
        super().save(*args, **kwargs)


# ============================================================================
# STOCK AUDIT (Enhanced for MRA)
# ============================================================================

class StockAudit(models.Model):
    """Stock take/audit records with MRA authority control"""
    STATUS_CHOICES = [
        ('Pending', 'Pending Approval'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]

    APPROVAL_ROLES = [
        ('Manager', 'Manager'),
        ('Auditor', 'Auditor'),
        ('MRA', 'MRA Official'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='stock_audits')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='stock_audits')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    total_discrepancy_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # MRA Compliance (NEW)
    approval_role = models.CharField(
        max_length=50,
        choices=APPROVAL_ROLES,
        blank=True,
        help_text="Role of person approving audit"
    )
    mra_visible = models.BooleanField(
        default=True,
        help_text="Is this audit visible to MRA?"
    )
    inventory_locked = models.BooleanField(
        default=False,
        help_text="Is inventory locked after approval?"
    )
    
    created_by = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    
    approved_by = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    
    notes = models.TextField(blank=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'status']),
            models.Index(fields=['mra_visible']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"Stock Audit - {self.branch.name} ({self.status})"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


class StockAuditItem(models.Model):
    """Individual items in a stock audit"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    audit = models.ForeignKey(StockAudit, on_delete=models.CASCADE, related_name='items')
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE)
    
    system_stock = models.DecimalField(max_digits=12, decimal_places=3)
    counted_stock = models.DecimalField(max_digits=12, decimal_places=3)
    discrepancy = models.DecimalField(max_digits=12, decimal_places=3)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['inventory_item__name']
        indexes = [
            models.Index(fields=['audit']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.inventory_item.name} - Discrepancy: {self.discrepancy}"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


# ============================================================================
# AUDIT LOG (Enhanced for MRA)
# ============================================================================

class AuditLog(models.Model):
    """Audit log for tracking all inventory operations with MRA awareness"""
    ACTION_TYPES = [
        ('STOCK_WASTE', 'Stock Waste'),
        ('STOCK_RECEIVE', 'Stock Receive'),
        ('STOCK_TRANSFER', 'Stock Transfer'),
        ('STOCK_AUDIT', 'Stock Audit'),
        ('INVENTORY_UPDATE', 'Inventory Update'),
        ('PRICE_CHANGE', 'Price Change'),
        ('TAX_CHANGE', 'Tax Change'),
        ('MRA_SYNC', 'MRA Sync'),
    ]

    ENTITY_TYPES = [
        ('WasteRecord', 'Waste Record'),
        ('PurchaseOrder', 'Purchase Order'),
        ('StockTransfer', 'Stock Transfer'),
        ('InventoryItem', 'Inventory Item'),
        ('MRAProductMapping', 'MRA Product Mapping'),
        ('InventorySnapshot', 'Inventory Snapshot'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='inventory_audit_logs')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='inventory_audit_logs')
    
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action_type = models.CharField(max_length=50, choices=ACTION_TYPES)
    entity_type = models.CharField(max_length=50, choices=ENTITY_TYPES)
    entity_id = models.CharField(max_length=255)
    
    details = models.JSONField(default=dict, blank=True)
    
    # MRA Compliance (NEW)
    mra_related = models.BooleanField(
        default=False,
        help_text="Is this operation related to MRA?"
    )
    mra_reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="MRA invoice or reference number"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'created_at']),
            models.Index(fields=['branch', 'created_at']),
            models.Index(fields=['action_type']),
            models.Index(fields=['entity_type']),
            models.Index(fields=['mra_related']),
            models.Index(fields=['mra_reference']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.action_type} - {self.entity_type} ({self.entity_id})"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])
