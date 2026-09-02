import uuid
from django.db import models
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone
from business.models import Business, Branch

User = get_user_model()


class DiscountRule(models.Model):
    """Admin-managed POS discount rule used for fiscal sale calculations."""
    DISCOUNT_TYPE_CHOICES = [
        ('percentage', 'Percentage'),
        ('fixed', 'Fixed Amount'),
    ]
    SCOPE_CHOICES = [
        ('all', 'All Products'),
        ('products', 'Selected Products'),
        ('categories', 'Selected Categories'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='discount_rules')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='discount_rules', null=True, blank=True)
    name = models.CharField(max_length=120)
    discount_type = models.CharField(max_length=20, choices=DISCOUNT_TYPE_CHOICES)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    applies_to = models.CharField(max_length=20, choices=SCOPE_CHOICES, default='all')
    product_ids = models.JSONField(default=list, blank=True)
    categories = models.JSONField(default=list, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    requires_manager_approval = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_discount_rules')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['business', 'branch', 'is_active']),
            models.Index(fields=['discount_type']),
            models.Index(fields=['starts_at', 'ends_at']),
        ]

    def __str__(self):
        return f'{self.name} ({self.discount_type}: {self.value})'

    def clean(self):
        super().clean()
        if self.value < 0:
            raise ValidationError('Discount value cannot be negative.')
        if self.discount_type == 'percentage' and self.value > 99:
            raise ValidationError('Percentage discount cannot exceed 99%.')
        if self.applies_to == 'products' and not isinstance(self.product_ids, list):
            raise ValidationError('Product discount scope must contain a product list.')
        if self.applies_to == 'categories' and not isinstance(self.categories, list):
            raise ValidationError('Category discount scope must contain a category list.')
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError('Discount end date must be after start date.')

    def is_currently_active(self, at_time=None):
        at_time = at_time or timezone.now()
        if not self.is_active:
            return False
        if self.starts_at and self.starts_at > at_time:
            return False
        if self.ends_at and self.ends_at < at_time:
            return False
        return True


class Session(models.Model):
    """POS Session model for tracking sales sessions"""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('closed', 'Closed'),
    ]

    # UUID field for frontend-backend sync
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relations
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='sessions')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='sessions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sessions')
    
    # Session info
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    pump_name = models.CharField(max_length=100, blank=True, null=True)
    
    # Cash tracking
    opening_float = models.DecimalField(max_digits=12, decimal_places=2)
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2)
    actual_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    closing_float = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    difference = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    
    # Sales tracking
    total_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_cash_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_card_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_mobile_money_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_on_account_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_other_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_tips = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Stock tracking (JSON for flexibility)
    opening_stock = models.JSONField(default=list, blank=True)  # List of {itemId, name, quantity}
    closing_stock = models.JSONField(default=list, blank=True)  # List of {itemId, name, quantity}
    
    # Timestamps
    started_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['branch', 'status']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['started_at']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"Session {self.id} - {self.user} ({self.status})"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


class Order(models.Model):
    """POS Sales Order model"""
    STATUS_CHOICES = [
        ('New', 'New'),
        ('Preparing', 'Preparing'),
        ('Ready', 'Ready'),
        ('Completed', 'Completed'),
        ('Voided', 'Voided'),
        ('Cancelled', 'Cancelled'),
    ]

    PAYMENT_METHODS = [
        ('Cash', 'Cash'),
        ('Card', 'Card'),
        ('Mobile Money', 'Mobile Money'),
        ('On Account', 'On Account'),
        ('Other', 'Other'),
    ]

    ORDER_TYPE_CHOICES = [
        ('sale', 'POS Sale'),
        ('kitchen', 'Kitchen Preparation'),
        ('invoice', 'Invoice Sale'),
    ]

    # UUID field for frontend-backend sync
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relations
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='orders')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='orders')
    session = models.ForeignKey(Session, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    
    # Order info
    order_number = models.IntegerField()
    order_type = models.CharField(max_length=20, choices=ORDER_TYPE_CHOICES, default='sale')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='New')
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS)
    pump_name = models.CharField(max_length=100, blank=True, null=True)

    # Buyer/customer details (optional)
    customer_name = models.CharField(max_length=255, blank=True, null=True)
    customer_phone = models.CharField(max_length=50, blank=True, null=True)
    customer_tin = models.CharField(max_length=50, blank=True, null=True)
    customer_email = models.CharField(max_length=255, blank=True, null=True)
    customer_address = models.CharField(max_length=255, blank=True, null=True)
    customer_notes = models.TextField(blank=True, null=True)
    buyer_name = models.CharField(max_length=255, blank=True, null=True)
    buyer_tin = models.CharField(max_length=50, blank=True, null=True)

    # EIS special sale fields
    buyer_authorization_code = models.CharField(max_length=100, blank=True, null=True)
    is_export = models.BooleanField(default=False)
    is_relief_supply = models.BooleanField(default=False)
    vat5_project_number = models.CharField(max_length=100, blank=True, null=True)
    vat5_certificate_number = models.CharField(max_length=100, blank=True, null=True)
    vat5_quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    eis_validation_metadata = models.JSONField(default=dict, blank=True)
    
    # Pricing
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_metadata = models.JSONField(default=dict, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    
    # Tax snapshot (MRA compliance - NEVER calculate tax dynamically)
    tax_rate_name = models.CharField(max_length=100, blank=True)
    tax_rate_value = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_type = models.CharField(
        max_length=20,
        choices=[
            ('VAT_STANDARD', 'VAT Standard Rated'),
            ('VAT_ZERO', 'VAT Zero Rated'),
            ('VAT_EXEMPT', 'VAT Exempt'),
        ],
        blank=True,
    )
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Cost tracking
    cogs = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Invoice tracking
    is_invoice_sale = models.BooleanField(default=False)
    invoice_id = models.CharField(max_length=255, blank=True, null=True)
    is_paid = models.BooleanField(default=False)
    
    # MRA EIS FISCAL INVOICE IDENTITY
    fiscal_invoice_number = models.CharField(max_length=100, unique=True, blank=True, null=True)
    eis_uuid = models.CharField(max_length=100, blank=True, null=True)
    eis_status = models.CharField(
        max_length=20,
        choices=[
            ('PENDING', 'Pending Submission'),
            ('SUBMITTED', 'Submitted to MRA'),
            ('ACCEPTED', 'Accepted by MRA'),
            ('REJECTED', 'Rejected by MRA'),
        ],
        default='PENDING',
        blank=True,
    )
    eis_submitted_at = models.DateTimeField(null=True, blank=True)
    qr_code_payload = models.TextField(blank=True, null=True)
    digital_signature = models.TextField(blank=True, null=True)
    is_fiscal_locked = models.BooleanField(default=False)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'status']),
            models.Index(fields=['branch', 'order_type']),
            models.Index(fields=['session']),
            models.Index(fields=['created_at']),
            models.Index(fields=['fiscal_invoice_number']),
            models.Index(fields=['eis_status']),
            models.Index(fields=['is_dirty']),
        ]
        unique_together = ('branch', 'order_number')

    def __str__(self):
        return f"Order #{self.order_number} - {self.order_type} - {self.status}"

    def mark_dirty(self):
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def save(self, *args, **kwargs):
        """Enforce immutability for locked fiscal invoices"""
        if self.pk and not kwargs.get('force_insert', False):
            try:
                existing = Order.objects.get(pk=self.pk)
                if existing.is_fiscal_locked:
                    raise ValidationError(
                        "Cannot modify a locked fiscal invoice. "
                        "This order has been submitted to MRA."
                    )
            except Order.DoesNotExist:
                pass

        if self.eis_status == 'SUBMITTED':
            self.is_fiscal_locked = True
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = list(set(update_fields) | {'is_fiscal_locked'})

        super().save(*args, **kwargs)


class OrderItem(models.Model):
    """Individual items in an order"""
    TAX_CALCULATION_METHODS = [
        ('inclusive', 'Tax Inclusive (Price includes tax)'),
        ('exclusive', 'Tax Exclusive (Tax added to price)'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    
    # Item info
    inventory_item_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Unit price")
    notes = models.TextField(blank=True)
    discount_rule_id = models.CharField(max_length=100, blank=True, null=True)
    discount_name = models.CharField(max_length=120, blank=True)
    discount_type = models.CharField(max_length=20, blank=True)
    discount_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # MRA PRODUCT MAPPING
    mra_product_code = models.CharField(max_length=100, blank=True, null=True)
    vat_category = models.CharField(
        max_length=20,
        choices=[
            ('STANDARD', 'Standard Rated'),
            ('ZERO', 'Zero Rated'),
            ('EXEMPT', 'Exempt'),
        ],
        default='STANDARD',
        blank=True,
    )
    
    # TAX INFORMATION (MRA Compliance - Immutable snapshot)
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text="Tax rate percentage (e.g., 16.50 for 16.5%)"
    )
    tax_type = models.CharField(
        max_length=20,
        choices=[
            ('standard', 'Standard Rated'),
            ('zero', 'Zero Rated'),
            ('exempt', 'Exempt'),
        ],
        default='standard',
        blank=True,
        help_text="MRA tax classification"
    )
    tax_calculation_method = models.CharField(
        max_length=20,
        choices=TAX_CALCULATION_METHODS,
        default='inclusive',
        help_text="How tax is calculated for this item (Immutable snapshot)"
    )
    
    # CALCULATED TAX AMOUNTS (Immutable snapshot for audit trail)
    subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Net amount (before tax)"
    )
    tax_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Tax amount for this item"
    )
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Gross amount (subtotal + tax)"
    )
    batch_consumption = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Exact batch consumption trace for this order line. "
            "Each entry: {inventory_item_id, batch_id, quantity}."
        ),
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(default=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['order']),
            models.Index(fields=['inventory_item_id']),
            models.Index(fields=['mra_product_code']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.name} x {self.quantity}"

    def mark_dirty(self):
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


class CreditNote(models.Model):
    """MRA-compliant Credit Note for sales returns or refunds"""
    REASON_CHOICES = [
        ('return', 'Sales Return'),
        ('refund', 'Customer Refund'),
        ('discount', 'Discount Adjustment'),
        ('error', 'Invoice Error Correction'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='credit_notes')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='credit_notes')
    original_order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='credit_notes')
    
    # Credit note details
    credit_note_number = models.CharField(max_length=100, unique=True)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    description = models.TextField()
    
    # Financial details
    credit_amount = models.DecimalField(max_digits=12, decimal_places=2)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_credit = models.DecimalField(max_digits=12, decimal_places=2)
    
    # MRA EIS compliance
    fiscal_credit_number = models.CharField(max_length=100, unique=True, blank=True, null=True)
    eis_uuid = models.CharField(max_length=100, blank=True, null=True)
    eis_status = models.CharField(
        max_length=20,
        choices=[
            ('PENDING', 'Pending Submission'),
            ('SUBMITTED', 'Submitted to MRA'),
            ('ACCEPTED', 'Accepted by MRA'),
            ('REJECTED', 'Rejected by MRA'),
        ],
        default='PENDING'
    )
    eis_submitted_at = models.DateTimeField(null=True, blank=True)
    qr_code_payload = models.TextField(blank=True, null=True)
    digital_signature = models.TextField(blank=True, null=True)
    is_fiscal_locked = models.BooleanField(default=False)
    
    # Audit trail
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'eis_status']),
            models.Index(fields=['original_order']),
            models.Index(fields=['fiscal_credit_number']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"Credit Note {self.credit_note_number} - {self.reason}"

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get('force_insert', False):
            existing = CreditNote.objects.filter(pk=self.pk).only('is_fiscal_locked').first()
            if existing and existing.is_fiscal_locked:
                update_fields = set(kwargs.get('update_fields') or [])
                if not update_fields or not update_fields.issubset({'is_dirty', 'updated_at'}):
                    raise ValidationError("Cannot modify a locked fiscal credit note.")

        if self.eis_status in {'SUBMITTED', 'ACCEPTED'}:
            self.is_fiscal_locked = True
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = list(set(update_fields) | {'is_fiscal_locked'})

        super().save(*args, **kwargs)

    def mark_dirty(self):
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


class DebitNote(models.Model):
    """MRA-compliant Debit Note for under-charges"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='debit_notes')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='debit_notes')
    original_order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='debit_notes')
    
    # Debit note details
    debit_note_number = models.CharField(max_length=100, unique=True)
    description = models.TextField()
    
    # Financial details
    additional_amount = models.DecimalField(max_digits=12, decimal_places=2)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_debit = models.DecimalField(max_digits=12, decimal_places=2)
    
    # MRA EIS compliance
    fiscal_debit_number = models.CharField(max_length=100, unique=True, blank=True, null=True)
    eis_uuid = models.CharField(max_length=100, blank=True, null=True)
    eis_status = models.CharField(
        max_length=20,
        choices=[
            ('PENDING', 'Pending Submission'),
            ('SUBMITTED', 'Submitted to MRA'),
            ('ACCEPTED', 'Accepted by MRA'),
            ('REJECTED', 'Rejected by MRA'),
        ],
        default='PENDING'
    )
    eis_submitted_at = models.DateTimeField(null=True, blank=True)
    qr_code_payload = models.TextField(blank=True, null=True)
    digital_signature = models.TextField(blank=True, null=True)
    is_fiscal_locked = models.BooleanField(default=False)
    
    # Audit trail
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'eis_status']),
            models.Index(fields=['original_order']),
            models.Index(fields=['fiscal_debit_number']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"Debit Note {self.debit_note_number}"

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get('force_insert', False):
            existing = DebitNote.objects.filter(pk=self.pk).only('is_fiscal_locked').first()
            if existing and existing.is_fiscal_locked:
                update_fields = set(kwargs.get('update_fields') or [])
                if not update_fields or not update_fields.issubset({'is_dirty', 'updated_at'}):
                    raise ValidationError("Cannot modify a locked fiscal debit note.")

        if self.eis_status in {'SUBMITTED', 'ACCEPTED'}:
            self.is_fiscal_locked = True
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = list(set(update_fields) | {'is_fiscal_locked'})

        super().save(*args, **kwargs)

    def mark_dirty(self):
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


class VoidTransaction(models.Model):
    """MRA-compliant Void Transaction for cancelled sales"""
    VOID_REASON_CHOICES = [
        ('customer_request', 'Customer Request'),
        ('item_returned', 'Item Returned'),
        ('wrong_order', 'Wrong Order'),
        ('system_error', 'System Error'),
        ('duplicate', 'Duplicate Entry'),
        ('payment_failed', 'Payment Failed'),
        ('other', 'Other'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='void_transactions')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='void_transactions')
    original_order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='void_transactions')
    
    # Void details
    void_number = models.CharField(max_length=100, unique=True)
    void_reason = models.CharField(max_length=50, choices=VOID_REASON_CHOICES)
    reason_description = models.TextField(help_text="Detailed explanation of why the sale was voided")
    supporting_documents = models.JSONField(
        default=list,
        blank=True,
        help_text="Optional supporting document references sent with the MRA EIS void/cancel request",
    )

    # Cancellation details
    voided_amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Total amount voided")
    voided_vat = models.DecimalField(max_digits=12, decimal_places=2, help_text="VAT amount voided")
    refund_method = models.CharField(
        max_length=20,
        choices=[
            ('cash', 'Cash Refund'),
            ('card', 'Card Refund'),
            ('credit', 'Store Credit'),
            ('none', 'No Refund'),
        ],
        default='none',
        help_text="How the customer will be refunded"
    )
    refund_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Amount to be refunded to customer"
    )
    refund_processed = models.BooleanField(default=False, help_text="Has the refund been processed?")
    refund_processed_at = models.DateTimeField(null=True, blank=True, help_text="When was refund processed?")
    
    # MRA EIS compliance
    fiscal_void_number = models.CharField(max_length=100, unique=True, blank=True, null=True)
    eis_uuid = models.CharField(max_length=100, blank=True, null=True)
    eis_status = models.CharField(
        max_length=20,
        choices=[
            ('PENDING', 'Pending Submission'),
            ('SUBMITTED', 'Submitted to MRA'),
            ('ACCEPTED', 'Accepted by MRA'),
            ('REJECTED', 'Rejected by MRA'),
        ],
        default='PENDING'
    )
    eis_submitted_at = models.DateTimeField(null=True, blank=True)
    qr_code_payload = models.TextField(blank=True, null=True)
    digital_signature = models.TextField(blank=True, null=True)
    is_fiscal_locked = models.BooleanField(default=False)
    
    # Audit trail
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='void_transactions_created')
    refund_processed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='void_transactions_refunded')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(default=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', 'eis_status']),
            models.Index(fields=['original_order']),
            models.Index(fields=['fiscal_void_number']),
            models.Index(fields=['refund_processed']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"Void Transaction {self.void_number} - {self.void_reason}"

    def save(self, *args, **kwargs):
        if self.pk and not kwargs.get('force_insert', False):
            existing = VoidTransaction.objects.filter(pk=self.pk).only('is_fiscal_locked').first()
            if existing and existing.is_fiscal_locked:
                update_fields = set(kwargs.get('update_fields') or [])
                if not update_fields or not update_fields.issubset({'is_dirty', 'updated_at'}):
                    raise ValidationError("Cannot modify a locked fiscal void transaction.")

        if self.eis_status in {'SUBMITTED', 'ACCEPTED'}:
            self.is_fiscal_locked = True
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = list(set(update_fields) | {'is_fiscal_locked'})

        super().save(*args, **kwargs)

    def mark_dirty(self):
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])
    
    def process_refund(self, user):
        """Mark refund as processed"""
        self.refund_processed = True
        self.refund_processed_at = timezone.now()
        self.refund_processed_by = user
        self.save()
        self.mark_dirty()
