"""
MRA EIS-Certified Business Models

This module implements MRA compliance requirements:
- Taxpayer identity (TIN, VAT registration)
- Branch-level tax reporting units
- Immutable tax rules
- Invoice immutability after payment/submission
- EIS enrollment tracking
"""

from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify
from django.core.exceptions import ValidationError

User = get_user_model()


# ============================================================================
# BUSINESS MODEL (Enhanced for MRA EIS)
# ============================================================================

class Business(models.Model):
    """
    Represents a business/taxpayer entity.
    CRITICAL: Distinguishes POS vendor from TAXPAYER being reported to MRA.
    """
    BUSINESS_TYPES = [
        ('restaurant', 'Restaurant'),
        ('grocery', 'Grocery'),
        ('pharmacy', 'Pharmacy'),
        ('supermarket', 'Supermarket'),
        ('bar_liquor', 'Bar & Liquor'),
        ('beauty_salon', 'Beauty Salon and Spa'),
        ('generic', 'Generic'),
    ]

    MRA_TAXPAYER_TYPES = [
        ('VAT', 'VAT Registered'),
        ('NON_VAT', 'Non VAT Registered'),
    ]

    # Basic info
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='businesses')
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    business_type = models.CharField(max_length=50, choices=BUSINESS_TYPES, default='generic')
    description = models.TextField(blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    address = models.TextField(blank=True)
    country = models.CharField(max_length=100, default='Malawi', help_text="Country where the business is located")
    website = models.URLField(blank=True)
    logo = models.ImageField(upload_to='business_logos/', null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    # ========== MRA EIS IDENTITY (CRITICAL FOR CERTIFICATION) ==========
    # Distinguish: POS vendor vs TAXPAYER (business being reported)
    tin = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        help_text="Taxpayer Identification Number (MRA) - MUST be unique"
    )
    vat_registration_number = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="VAT Registration Number from MRA"
    )
    vat_registered = models.BooleanField(
        default=False,
        help_text="Is this business VAT registered?"
    )
    
    # EIS Status
    mra_taxpayer_type = models.CharField(
        max_length=50,
        choices=MRA_TAXPAYER_TYPES,
        default='NON_VAT',
        help_text="MRA taxpayer classification"
    )
    mra_enrolled = models.BooleanField(
        default=False,
        help_text="Is this business enrolled in MRA EIS?"
    )
    mra_enrolled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When was this business enrolled in MRA EIS?"
    )
    
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
        verbose_name_plural = 'Businesses'
        indexes = [
            models.Index(fields=['tin']),
            models.Index(fields=['mra_enrolled']),
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

    def save(self, *args, **kwargs):
        """Auto-generate slug if not provided"""
        # Normalize TIN to avoid storing whitespace or empty strings.
        if self.tin is not None:
            normalized_tin = str(self.tin).strip()
            self.tin = normalized_tin or None

        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            
            # Ensure unique slug
            while Business.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            
            self.slug = slug
        
        super().save(*args, **kwargs)


# ============================================================================
# BRANCH MODEL (Enhanced for MRA EIS)
# ============================================================================

class Branch(models.Model):
    """
    Represents a branch/location.
    MRA treats each branch as a separate tax reporting unit.
    """
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='branches')
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    address = models.TextField()
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    # ========== MRA EIS BRANCH IDENTIFICATION ==========
    mra_branch_code = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="MRA-assigned branch code for tax reporting"
    )
    mra_device_location = models.CharField(
        max_length=255,
        blank=True,
        help_text="Physical location description for MRA records"
    )
    mra_site_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="MRA EIS terminal site ID mapped to this local branch"
    )
    mra_site_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="MRA EIS terminal site name mapped to this local branch"
    )
    mra_terminal_id = models.CharField(
        max_length=100,
        blank=True,
        help_text="Last activated MRA terminal ID for this branch"
    )
    mra_terminal_position = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="MRA terminal position returned during terminal activation"
    )
    is_eis_warehouse = models.BooleanField(
        default=False,
        help_text="Treat this branch as an EIS warehouse/location for stock transfers"
    )
    eis_mapping_source = models.CharField(
        max_length=50,
        blank=True,
        help_text="Where the current EIS branch mapping came from"
    )
    eis_mapping_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this branch's EIS mapping was last updated"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        unique_together = ('business', 'slug')
        indexes = [
            models.Index(fields=['business', 'is_active']),
            models.Index(fields=['mra_branch_code']),
            models.Index(fields=['mra_site_id']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.business.name} - {self.name}"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def save(self, *args, **kwargs):
        """Auto-generate or regenerate slug based on name"""
        # Always regenerate slug from name to keep it in sync
        base_slug = slugify(self.name)
        slug = base_slug
        counter = 1
        
        # Ensure unique slug within the business
        while Branch.objects.filter(
            business=self.business,
            slug=slug
        ).exclude(pk=self.pk).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        
        self.slug = slug
        
        super().save(*args, **kwargs)


# ============================================================================
# TAX RATE MODEL (Enhanced for MRA EIS)
# ============================================================================

class TaxRate(models.Model):
    """
    Tax rate rules with immutability enforcement.
    CRITICAL: Once used in an invoice, tax rates cannot be modified.
    """
    TAX_TYPE_CHOICES = (
        ('VAT_STANDARD', 'VAT Standard Rated'),
        ('VAT_ZERO', 'VAT Zero Rated'),
        ('VAT_EXEMPT', 'VAT Exempt'),
    )

    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='tax_rates'
    )
    name = models.CharField(max_length=100)
    rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="VAT percentage. Use 0.00 for zero-rated or exempt."
    )
    tax_type = models.CharField(
        max_length=20,
        choices=TAX_TYPE_CHOICES,
        default='VAT_STANDARD'
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Default VAT rate for taxable items"
    )
    
    # Effective dates
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    
    # MRA Mapping
    mra_tax_code = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="MRA tax code for this rate"
    )
    
    # Immutability enforcement
    locked = models.BooleanField(
        default=False,
        help_text="Is this tax rate locked? (cannot be edited after use)"
    )
    
    # Status
    is_active = models.BooleanField(default=True)
    
    # Audit
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-is_default', '-effective_from']
        constraints = [
            models.UniqueConstraint(
                fields=['business'],
                condition=models.Q(is_default=True, is_active=True),
                name='one_active_default_tax_per_business'
            )
        ]
        indexes = [
            models.Index(fields=['business', 'is_active']),
            models.Index(fields=['locked']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.name} ({self.rate}%) - {self.tax_type}"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def save(self, *args, **kwargs):
        """Prevent editing of locked tax rates"""
        if self.pk:
            existing = TaxRate.objects.get(pk=self.pk)
            if existing.locked and existing.rate != self.rate:
                raise ValidationError("Cannot modify a locked tax rate. Create a new tax rate instead.")
        
        super().save(*args, **kwargs)


# ============================================================================
# BUSINESS SETTINGS MODEL (Enhanced for MRA EIS)
# ============================================================================

class BusinessSettings(models.Model):
    """
    Business configuration with EIS controls.
    """
    business = models.OneToOneField(Business, on_delete=models.CASCADE, related_name='settings')
    currency = models.CharField(max_length=3, default='MWK')
    timezone = models.CharField(max_length=50, default='UTC')
    enable_inventory = models.BooleanField(default=True)
    enable_invoicing = models.BooleanField(default=True)
    enable_pos = models.BooleanField(default=True)
    enable_kitchen = models.BooleanField(default=False)
    enable_delivery = models.BooleanField(default=False)
    fuel_pumps = models.JSONField(default=list, blank=True)
    
    # ========== MRA EIS CONTROLS ==========
    enable_eis = models.BooleanField(
        default=False,
        help_text="Enable MRA EIS integration for this business"
    )
    eis_environment = models.CharField(
        max_length=20,
        choices=[('TEST', 'Test/Sandbox'), ('PROD', 'Production')],
        default='TEST',
        help_text="MRA EIS environment (sandbox or production)"
    )
    
    # Block sales if EIS is down
    block_sales_if_eis_down = models.BooleanField(
        default=True,
        help_text="Block POS sales if EIS is unavailable (MRA requirement)"
    )

    # Block sales if tax/MRA mapping is missing
    block_sales_if_tax_mapping_missing = models.BooleanField(
        default=False,
        help_text="Block POS sales if items lack approved+synced MRA mappings"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        verbose_name_plural = 'Business Settings'

    def __str__(self):
        return f"Settings for {self.business.name}"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


# ============================================================================
# CUSTOMER MODEL (Enhanced for MRA EIS)
# ============================================================================

class Customer(models.Model):
    """
    Customer with VAT tracking for B2B invoices.
    """
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='customers')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='customers')
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=32, blank=True)
    address = models.TextField(blank=True)
    
    # ========== MRA VAT TRACKING ==========
    customer_tin = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Customer TIN (for B2B invoices)"
    )
    vat_registered = models.BooleanField(
        default=False,
        help_text="Is customer VAT registered?"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        unique_together = ('business', 'email')
        indexes = [
            models.Index(fields=['business', 'vat_registered']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.name} ({self.business.name})"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


# ============================================================================
# INVOICE LINE MODEL (NEW - CRITICAL FOR MRA EIS)
# ============================================================================

class InvoiceLine(models.Model):
    """
    Individual invoice line item.
    CRITICAL: Stored relationally (not JSON) for MRA compliance.
    MRA auditors require traceable, immutable line items.
    """
    invoice = models.ForeignKey(
        'Invoice',
        on_delete=models.CASCADE,
        related_name='lines'
    )
    
    # Product info
    product_code = models.CharField(max_length=100)
    product_name = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    
    # Tax info (immutable snapshot)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    
    # MRA Mapping
    mra_product_code = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="MRA product code"
    )
    
    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['invoice']),
            models.Index(fields=['mra_product_code']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])


# ============================================================================
# INVOICE MODEL (Enhanced for MRA EIS)
# ============================================================================

class Invoice(models.Model):
    """
    Invoice with MRA EIS compliance and immutability enforcement.
    CRITICAL: Once paid or submitted to MRA, invoice is read-only.
    """
    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Sent', 'Sent'),
        ('Paid', 'Paid'),
        ('Void', 'Void'),
    ]

    APPROVAL_STATUS_CHOICES = [
        ('Pending', 'Pending Approval'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]

    MRA_STATUS_CHOICES = [
        ('PENDING', 'Pending Submission'),
        ('SUBMITTED', 'Submitted to MRA'),
        ('ACCEPTED', 'Accepted by MRA'),
        ('REJECTED', 'Rejected by MRA'),
    ]

    # Basic info
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='invoices')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    
    invoice_number = models.IntegerField()
    customer_name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    approval_status = models.CharField(
        max_length=20,
        choices=APPROVAL_STATUS_CHOICES,
        default='Pending',
        help_text="Approval status for invoice review"
    )
    
    # Amounts (calculated from lines)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Dates
    issue_date = models.DateTimeField()
    due_date = models.DateTimeField()
    notes = models.TextField(blank=True)
    
    # Link to POS Order
    related_order_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="UUID of related POS Order when invoice is marked as Paid"
    )
    
    # Approval tracking
    approved_by = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    
    # ========== MRA EIS TRACKING (CRITICAL) ==========
    mra_invoice_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Invoice number assigned by MRA"
    )
    mra_status = models.CharField(
        max_length=50,
        choices=MRA_STATUS_CHOICES,
        default='PENDING',
        help_text="MRA submission status"
    )
    mra_receipt_signature = models.TextField(
        blank=True,
        null=True,
        help_text="Cryptographic signature from MRA"
    )
    mra_qr_code = models.TextField(
        blank=True,
        null=True,
        help_text="QR code data from MRA"
    )
    mra_submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When was this invoice submitted to MRA?"
    )
    
    # Immutability flag
    is_locked = models.BooleanField(
        default=False,
        help_text="Is this invoice locked? (read-only after payment/submission)"
    )
    
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
        indexes = [
            models.Index(fields=['business', 'status']),
            models.Index(fields=['business', 'approval_status']),
            models.Index(fields=['business', 'created_at']),
            models.Index(fields=['invoice_number']),
            models.Index(fields=['mra_status']),
            models.Index(fields=['is_locked']),
            models.Index(fields=['is_dirty']),
        ]
        unique_together = ('business', 'invoice_number')

    def __str__(self):
        return f"Invoice #{self.invoice_number} - {self.customer_name}"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])

    def save(self, *args, **kwargs):
        """Enforce immutability for locked invoices"""
        if self.pk:
            existing = Invoice.objects.get(pk=self.pk)
            if existing.is_locked:
                raise ValidationError("Cannot modify a locked invoice. This invoice has been paid or submitted to MRA.")
        
        # Auto-lock when paid or submitted
        if self.status == 'Paid' or self.mra_status == 'SUBMITTED':
            self.is_locked = True
        
        super().save(*args, **kwargs)


# ============================================================================
# EXPENSE MODEL
# ============================================================================

class Expense(models.Model):
    """Expense tracking with approval workflow"""
    CATEGORY_CHOICES = [
        ('Utilities', 'Utilities'),
        ('Rent', 'Rent'),
        ('Salaries', 'Salaries'),
        ('Supplies', 'Supplies'),
        ('Marketing', 'Marketing'),
        ('Maintenance', 'Maintenance'),
        ('Other', 'Other'),
    ]

    STATUS_CHOICES = [
        ('Pending', 'Pending Approval'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]

    id = models.CharField(max_length=255, primary_key=True)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='expenses')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')
    
    title = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateTimeField()
    notes = models.TextField(blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    
    created_by = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    approved_by = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    
    # Sync tracking
    is_dirty = models.BooleanField(
        default=True,
        help_text="Marks record as dirty (needs syncing). Set to False after successful sync."
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'status']),
            models.Index(fields=['business', 'created_at']),
            models.Index(fields=['category']),
            models.Index(fields=['is_dirty']),
        ]

    def __str__(self):
        return f"{self.title} - {self.amount}"

    def mark_dirty(self):
        """Mark this record as dirty (needs syncing)"""
        self.is_dirty = True
        self.save(update_fields=['is_dirty'])

    def mark_synced(self):
        """Mark this record as synced"""
        self.is_dirty = False
        self.save(update_fields=['is_dirty'])
