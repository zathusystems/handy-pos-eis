"""
MRA EIS Integration Models
Implements full compliance with Malawi Revenue Authority Electronic Invoicing System
"""
import uuid
import hashlib
import json
from decimal import Decimal
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator
from business.models import Business, Branch
from .fields import EncryptedTextField
from .security import redact_sensitive_data, redact_sensitive_text

User = get_user_model()


# ============================================================================
# 1. TERMINAL & ONBOARDING MODULE
# ============================================================================

class Terminal(models.Model):
    """
    Represents a POS terminal registered with MRA EIS.
    One terminal = one device + OS combination.
    """
    STATUS_CHOICES = [
        ('pending_activation', 'Pending Activation'),
        ('active', 'Active'),
        ('suspended', 'Suspended'),
        ('deactivated', 'Deactivated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='mra_terminals')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='mra_terminals')
    
    # Terminal identification
    terminal_id = models.CharField(
        max_length=50,
        unique=True,
        help_text="Unique terminal identifier assigned by MRA"
    )
    device_serial = models.CharField(
        max_length=255,
        help_text="Device serial number or generated identifier"
    )
    mac_address = models.CharField(
        max_length=17,
        blank=True,
        help_text="MAC address of the device"
    )
    
    # POS Information
    pos_name = models.CharField(max_length=255, help_text="Name of POS system")
    pos_version = models.CharField(max_length=50, help_text="Version of POS system")
    os_type = models.CharField(
        max_length=50,
        help_text="Operating system type (Windows, Linux, macOS, Android, iOS, Web)"
    )
    
    # MRA Credentials (encrypted in production)
    mra_terminal_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Terminal ID from MRA"
    )
    mra_taxpayer_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Numeric taxpayer ID from MRA activation response; used in fiscal invoice number generation"
    )
    terminal_position = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Terminal position from MRA activation response; used in fiscal invoice number generation"
    )
    mra_api_key = EncryptedTextField(
        help_text="API key for MRA communication (encrypted at rest)"
    )
    mra_token = EncryptedTextField(
        blank=True,
        help_text="Current authentication token from MRA (encrypted at rest)"
    )
    token_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Token expiration timestamp"
    )
    
    # Status & Activation
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending_activation')
    is_online = models.BooleanField(default=False, help_text="Current connectivity status")
    
    # Counters (MRA compliance)
    online_invoice_counter = models.BigIntegerField(
        default=0,
        help_text="Sequential counter for online invoices"
    )
    offline_invoice_counter = models.BigIntegerField(
        default=0,
        help_text="Sequential counter for offline invoices"
    )
    
    # Audit fields
    activated_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'branch']),
            models.Index(fields=['business', 'branch', 'device_serial']),
            models.Index(fields=['status']),
            models.Index(fields=['mra_terminal_id']),
        ]

    def __str__(self):
        return f"Terminal {self.terminal_id} - {self.branch.name}"

    def is_token_expired(self):
        """Check if MRA token has expired"""
        if not self.token_expires_at:
            return True
        return timezone.now() >= self.token_expires_at

    def increment_online_counter(self):
        """Increment online invoice counter (thread-safe using F expressions)"""
        from django.db.models import F
        Terminal.objects.filter(pk=self.pk).update(online_invoice_counter=F('online_invoice_counter') + 1)
        self.refresh_from_db()
        return self.online_invoice_counter

    def increment_offline_counter(self):
        """Increment offline invoice counter (thread-safe using F expressions)"""
        from django.db.models import F
        Terminal.objects.filter(pk=self.pk).update(offline_invoice_counter=F('offline_invoice_counter') + 1)
        self.refresh_from_db()
        return self.offline_invoice_counter


class FiscalInvoiceSequence(models.Model):
    """
    Daily fiscal invoice sequence per MRA terminal.

    MRA fiscal invoice numbers encode taxpayer ID, terminal position, Julian
    date, and a single daily count. Online and offline receipts must draw from
    this same counter to avoid same-day duplicate invoice numbers.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terminal = models.ForeignKey(Terminal, on_delete=models.CASCADE, related_name='fiscal_sequences')
    julian_date = models.PositiveIntegerField(help_text="MRA Julian date encoded in the fiscal invoice number")
    last_sequence = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-julian_date']
        unique_together = ('terminal', 'julian_date')
        indexes = [
            models.Index(fields=['terminal', 'julian_date']),
        ]

    def __str__(self):
        return f"{self.terminal.terminal_id} / {self.julian_date}: {self.last_sequence}"


class TerminalActivationCode(models.Model):
    """
    TAC (Terminal Activation Code) - one-time use code for terminal activation.
    Prevents TAC reuse and tracks activation history.
    """
    STATUS_CHOICES = [
        ('unused', 'Unused'),
        ('used', 'Used'),
        ('expired', 'Expired'),
        ('revoked', 'Revoked'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='mra_tacs')
    
    code = models.CharField(
        max_length=50,
        unique=True,
        help_text="Activation code from MRA"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unused')
    
    # Usage tracking
    used_by_terminal = models.OneToOneField(
        Terminal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activation_code'
    )
    used_at = models.DateTimeField(null=True, blank=True)
    
    # Validity
    expires_at = models.DateTimeField(help_text="TAC expiration date")
    
    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'status']),
            models.Index(fields=['code']),
        ]

    def __str__(self):
        return f"TAC {self.code} - {self.status}"

    def is_valid(self):
        """Check if TAC is valid for activation"""
        return (
            self.status == 'unused' and
            timezone.now() < self.expires_at
        )

    def mark_as_used(self, terminal):
        """Mark TAC as used by a terminal"""
        if not self.is_valid():
            raise ValueError("TAC is not valid for use")
        self.status = 'used'
        self.used_by_terminal = terminal
        self.used_at = timezone.now()
        self.save()


# ============================================================================
# 2. CONFIGURATION MANAGEMENT
# ============================================================================

class MRAConfiguration(models.Model):
    """
    Immutable MRA configuration snapshot.
    Fetched periodically from MRA and stored locally.
    """
    CONFIG_TYPES = [
        ('tax_rules', 'Tax Rules'),
        ('receipt_format', 'Receipt Format'),
        ('product_codes', 'Product Codes'),
        ('system_settings', 'System Settings'),
        ('global_configuration', 'MRA Global Configuration'),
        ('terminal_configuration', 'MRA Terminal Configuration'),
        ('taxpayer_configuration', 'MRA Taxpayer Configuration'),
        ('terminal_site_products', 'MRA Terminal Site Products'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='mra_configs')
    
    config_type = models.CharField(max_length=50, choices=CONFIG_TYPES)
    config_version = models.CharField(
        max_length=50,
        help_text="Version identifier from MRA"
    )
    
    # Immutable configuration data
    config_data = models.JSONField(help_text="Configuration data from MRA")
    
    # Validity
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    # Audit
    fetched_from_mra_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-effective_from']
        indexes = [
            models.Index(fields=['business', 'config_type', 'is_active']),
            models.Index(fields=['config_type', 'effective_from']),
        ]
        unique_together = ('business', 'config_type', 'config_version')

    def __str__(self):
        return f"{self.config_type} v{self.config_version}"

    def is_current(self):
        """Check if this configuration is currently active"""
        now = timezone.now()
        return (
            self.is_active and
            self.effective_from <= now and
            (self.effective_to is None or now < self.effective_to)
        )


class ConfigurationSyncLog(models.Model):
    """
    Tracks configuration sync attempts with MRA.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='mra_config_sync_logs')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    config_types = models.JSONField(default=list, help_text="List of config types synced")
    
    error_message = models.TextField(blank=True)
    
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'status']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"Config Sync - {self.status}"

    def save(self, *args, **kwargs):
        self.error_message = redact_sensitive_text(self.error_message)
        super().save(*args, **kwargs)


# ============================================================================
# 3. PRODUCT & STOCK MAPPING
# ============================================================================

class MRAProductMapping(models.Model):
    """
    Legacy product mapping table retained for migration/data compatibility.

    Active POS/EIS mapping now lives in inventory.MRAProductMapping because it
    links directly to InventoryItem and branch-scoped sales.
    """
    TAX_CATEGORIES = [
        ('standard', 'Standard Rated (16.5%)'),
        ('zero', 'Zero Rated (0%)'),
        ('exempt', 'Exempt'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='mra_product_mappings')
    
    # Internal product reference
    inventory_item_id = models.CharField(
        max_length=255,
        help_text="UUID of the internal inventory item"
    )
    product_name = models.CharField(max_length=255)
    
    # MRA mapping
    mra_product_code = models.CharField(
        max_length=50,
        help_text="MRA-assigned product code"
    )
    mra_product_name = models.CharField(max_length=255)
    tax_category = models.CharField(max_length=20, choices=TAX_CATEGORIES)
    
    # Price & Tax (immutable - enforced at sale time)
    approved_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="MRA-approved price (if applicable)"
    )
    tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Tax rate percentage"
    )
    
    # Status
    is_approved = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    
    # Audit
    approved_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'is_active']),
            models.Index(fields=['mra_product_code']),
        ]
        unique_together = ('business', 'inventory_item_id')

    def __str__(self):
        return f"{self.product_name} -> {self.mra_product_code}"


# ============================================================================
# 4. SALES & INVOICE SUBMISSION
# ============================================================================

class MRAInvoice(models.Model):
    """
    MRA-compliant invoice record.
    Immutable once created - represents a submitted sale.
    """
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('submitted', 'Submitted to MRA'),
        ('accepted', 'Accepted by MRA'),
        ('rejected', 'Rejected by MRA'),
        ('offline_queued', 'Queued for Offline Sync'),
        ('offline_synced', 'Synced from Offline Queue'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name='mra_invoices')
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='mra_invoices')
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, related_name='mra_invoices')
    
    # Invoice identification (immutable)
    invoice_number = models.BigIntegerField(
        help_text="Sequential invoice number per terminal fiscal day"
    )
    fiscal_julian_date = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="MRA Julian date used with invoice_number for daily fiscal sequencing"
    )
    mra_invoice_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Invoice ID assigned by MRA"
    )
    
    # Seller information (client TIN)
    seller_tin = models.CharField(
        max_length=50,
        help_text="Client TIN (always used as seller)"
    )
    seller_name = models.CharField(max_length=255)
    
    # Buyer information
    buyer_tin = models.CharField(
        max_length=50,
        blank=True,
        help_text="Buyer TIN (if available)"
    )
    buyer_name = models.CharField(max_length=255, blank=True)
    
    # Invoice data (immutable)
    items = models.JSONField(
        help_text="Invoice items with MRA product codes and tax info"
    )
    
    # Amounts (immutable)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    
    # Tax breakdown (for audit)
    tax_breakdown = models.JSONField(
        default=dict,
        help_text="Tax by category: {standard: amount, zero: amount, exempt: amount}"
    )
    
    # Invoice signature (for QR code)
    invoice_signature = models.CharField(
        max_length=500,
        blank=True,
        help_text="Cryptographic signature of invoice"
    )
    
    # Status & Submission
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    is_online = models.BooleanField(
        default=True,
        help_text="True if submitted online, False if offline"
    )
    
    # Timestamps (immutable)
    invoice_date = models.DateTimeField(help_text="Invoice date/time")
    submitted_at = models.DateTimeField(null=True, blank=True)
    mra_response = models.JSONField(default=dict, blank=True)
    
    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-invoice_date']
        indexes = [
            models.Index(fields=['business', 'branch']),
            models.Index(fields=['terminal', 'invoice_number']),
            models.Index(fields=['terminal', 'invoice_number', 'is_online']),
            models.Index(fields=['terminal', 'fiscal_julian_date', 'invoice_number']),
            models.Index(fields=['status']),
            models.Index(fields=['invoice_date']),
            models.Index(fields=['seller_tin']),
        ]
        unique_together = ('terminal', 'fiscal_julian_date', 'invoice_number')

    def __str__(self):
        return f"Invoice #{self.invoice_number} - {self.status}"

    def can_edit(self):
        """Check if invoice can be edited (only drafts)"""
        return self.status == 'draft'

    def can_delete(self):
        """Invoices cannot be deleted once submitted"""
        return self.status == 'draft'

    def generate_signature(self):
        """Generate cryptographic signature for invoice"""
        gross_amount = Decimal(str(self.gross_amount or 0)).quantize(Decimal('0.01'))
        signature_data = {
            'invoice_number': self.invoice_number,
            'seller_tin': self.seller_tin,
            'invoice_date': self.invoice_date.isoformat(),
            'gross_amount': format(gross_amount, 'f'),
            'items': self.items,
        }
        signature_string = json.dumps(signature_data, sort_keys=True)
        return hashlib.sha256(signature_string.encode()).hexdigest()

    def save(self, *args, **kwargs):
        self.mra_response = redact_sensitive_data(self.mra_response)
        super().save(*args, **kwargs)


# ============================================================================
# 5. OFFLINE SALES ENGINE
# ============================================================================

class OfflineInvoiceQueue(models.Model):
    """
    Queue for invoices generated while offline.
    Maintains order for sequential sync when online.
    """
    STATUS_CHOICES = [
        ('queued', 'Queued'),
        ('syncing', 'Syncing'),
        ('synced', 'Synced'),
        ('failed', 'Failed'),
        ('expired', 'Expired - MRA time limit exceeded'),
    ]
    SYNC_STATE_MAP = {
        'queued': 'PENDING',
        'syncing': 'SENDING',
        'synced': 'SUCCESS',
        'failed': 'FAILED',
        'expired': 'EXPIRED',
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terminal = models.ForeignKey(Terminal, on_delete=models.CASCADE, related_name='offline_queue')
    mra_invoice = models.OneToOneField(
        MRAInvoice,
        on_delete=models.CASCADE,
        related_name='offline_queue_entry'
    )
    
    # Queue position (for ordering)
    queue_position = models.BigIntegerField(
        help_text="Position in offline queue (for sequential sync)"
    )
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    
    # Sync attempts
    sync_attempts = models.IntegerField(default=0)
    last_sync_attempt_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.TextField(blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['queue_position']
        indexes = [
            models.Index(fields=['terminal', 'status']),
            models.Index(fields=['queue_position']),
        ]
        unique_together = ('terminal', 'queue_position')

    def __str__(self):
        return f"Offline Queue #{self.queue_position} - {self.status}"

    @property
    def sync_state(self):
        """Expose MRA-required sync state naming without altering stored status values."""
        return self.SYNC_STATE_MAP.get(self.status, 'PENDING')

    def save(self, *args, **kwargs):
        self.last_sync_error = redact_sensitive_text(self.last_sync_error)
        super().save(*args, **kwargs)


class OfflineAuditLog(models.Model):
    """
    Audit log for offline operations.
    Tracks all offline sales and sync events.
    """
    EVENT_TYPES = [
        ('invoice_created', 'Invoice Created Offline'),
        ('invoice_queued', 'Invoice Queued'),
        ('sync_started', 'Sync Started'),
        ('sync_completed', 'Sync Completed'),
        ('sync_failed', 'Sync Failed'),
        ('online_detected', 'Online Detected'),
        ('offline_detected', 'Offline Detected'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terminal = models.ForeignKey(Terminal, on_delete=models.CASCADE, related_name='offline_audit_logs')
    
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    details = models.JSONField(default=dict)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['terminal', 'event_type']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.event_type} - {self.terminal.terminal_id}"

    def save(self, *args, **kwargs):
        self.details = redact_sensitive_data(self.details)
        super().save(*args, **kwargs)


# ============================================================================
# 6. RECEIPT & QR CODE GENERATION
# ============================================================================

class Receipt(models.Model):
    """
    Receipt record with QR code data.
    Immutable once generated.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mra_invoice = models.OneToOneField(
        MRAInvoice,
        on_delete=models.CASCADE,
        related_name='receipt'
    )
    
    # Receipt content
    receipt_number = models.CharField(max_length=50)
    receipt_text = models.TextField(help_text="Formatted receipt text")
    
    # QR Code data
    qr_code_data = models.TextField(
        help_text="Data encoded in QR code (invoice signature + metadata)"
    )
    qr_code_image = models.TextField(
        blank=True,
        help_text="Base64 encoded QR code image"
    )
    
    # Timestamps
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-generated_at']
        indexes = [
            models.Index(fields=['mra_invoice']),
        ]

    def __str__(self):
        return f"Receipt {self.receipt_number}"


# ============================================================================
# 7. SECURITY & AUDIT CONTROLS
# ============================================================================

class InvoiceAuditLog(models.Model):
    """
    Write-once audit log for all invoice operations.
    Tracks who, when, and what for compliance.
    """
    ACTION_TYPES = [
        ('created', 'Invoice Created'),
        ('submitted', 'Invoice Submitted'),
        ('accepted', 'Invoice Accepted by MRA'),
        ('rejected', 'Invoice Rejected by MRA'),
        ('queued_offline', 'Queued for Offline Sync'),
        ('synced_from_offline', 'Synced from Offline Queue'),
        ('viewed', 'Invoice Viewed'),
        ('receipt_generated', 'Receipt Generated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mra_invoice = models.ForeignKey(
        MRAInvoice,
        on_delete=models.CASCADE,
        related_name='audit_logs'
    )
    
    action = models.CharField(max_length=50, choices=ACTION_TYPES)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    details = models.JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['mra_invoice', 'action']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.action} - Invoice {self.mra_invoice.invoice_number}"

    def save(self, *args, **kwargs):
        self.details = redact_sensitive_data(self.details)
        super().save(*args, **kwargs)


class TerminalAuditLog(models.Model):
    """
    Audit log for terminal operations.
    """
    ACTION_TYPES = [
        ('activated', 'Terminal Activated'),
        ('token_refreshed', 'Token Refreshed'),
        ('online_status_changed', 'Online Status Changed'),
        ('configuration_updated', 'Configuration Updated'),
        ('mra_request_signed', 'MRA Request Signed'),
        ('suspended', 'Terminal Suspended'),
        ('deactivated', 'Terminal Deactivated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terminal = models.ForeignKey(
        Terminal,
        on_delete=models.CASCADE,
        related_name='audit_logs'
    )
    
    action = models.CharField(max_length=50, choices=ACTION_TYPES)
    details = models.JSONField(default=dict)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['terminal', 'action']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.action} - {self.terminal.terminal_id}"

    def save(self, *args, **kwargs):
        self.details = redact_sensitive_data(self.details)
        super().save(*args, **kwargs)


# ============================================================================
# 8. ERROR HANDLING & RESILIENCE
# ============================================================================

class MRAAPIError(models.Model):
    """
    Tracks API errors and failures for resilience and debugging.
    """
    ERROR_TYPES = [
        ('connection_error', 'Connection Error'),
        ('timeout', 'Timeout'),
        ('token_expired', 'Token Expired'),
        ('invalid_request', 'Invalid Request'),
        ('server_error', 'Server Error'),
        ('rate_limit', 'Rate Limit'),
        ('unknown', 'Unknown Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terminal = models.ForeignKey(
        Terminal,
        on_delete=models.CASCADE,
        related_name='api_errors'
    )
    
    error_type = models.CharField(max_length=50, choices=ERROR_TYPES)
    error_message = models.TextField()
    error_code = models.CharField(max_length=50, blank=True)
    
    # Retry tracking
    retry_count = models.IntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    is_resolved = models.BooleanField(default=False)
    
    # Context
    related_invoice = models.ForeignKey(
        MRAInvoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['terminal', 'is_resolved']),
            models.Index(fields=['error_type']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.error_type} - {self.error_message[:50]}"

    def save(self, *args, **kwargs):
        self.error_message = redact_sensitive_text(self.error_message)
        super().save(*args, **kwargs)

    def should_retry(self):
        """Check if error should be retried"""
        return (
            not self.is_resolved and
            self.retry_count < 5 and
            (self.next_retry_at is None or timezone.now() >= self.next_retry_at)
        )


class SyncRetryQueue(models.Model):
    """
    Database-backed queue for retrying failed operations.
    Implements exponential backoff.
    """
    OPERATION_TYPES = [
        ('submit_invoice', 'Submit Invoice'),
        ('submit_pos_order', 'Submit POS Order'),
        ('sync_offline_invoices', 'Sync Offline Invoices'),
        ('refresh_token', 'Refresh Token'),
        ('fetch_configuration', 'Fetch Configuration'),
        ('submit_credit_note', 'Submit Credit Note'),
        ('submit_debit_note', 'Submit Debit Note'),
        ('submit_void_transaction', 'Submit Void Transaction'),
        ('submit_stock_payload', 'Submit Stock Payload'),
        ('submit_purchase_item_receipt', 'Submit Purchase Item Receipt'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    SYNC_STATE_MAP = {
        'pending': 'PENDING',
        'processing': 'SENDING',
        'completed': 'SUCCESS',
        'failed': 'FAILED',
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    terminal = models.ForeignKey(
        Terminal,
        on_delete=models.CASCADE,
        related_name='sync_retry_queue'
    )
    
    operation_type = models.CharField(max_length=50, choices=OPERATION_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Payload
    payload = models.JSONField()
    
    # Retry tracking
    attempt_count = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    next_attempt_at = models.DateTimeField()
    
    # Error tracking
    last_error = models.TextField(blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['next_attempt_at']
        indexes = [
            models.Index(fields=['terminal', 'status']),
            models.Index(fields=['next_attempt_at']),
        ]

    def __str__(self):
        return f"{self.operation_type} - {self.status}"

    def should_retry(self):
        """Check if operation should be retried"""
        return (
            self.status == 'pending' and
            self.attempt_count < self.max_attempts and
            timezone.now() >= self.next_attempt_at
        )

    def calculate_next_retry(self):
        """Calculate next retry time with exponential backoff"""
        backoff_seconds = min(300, 2 ** self.attempt_count * 10)  # Max 5 minutes
        return timezone.now() + timezone.timedelta(seconds=backoff_seconds)

    @property
    def sync_state(self):
        """Expose MRA-required sync state naming without altering stored status values."""
        return self.SYNC_STATE_MAP.get(self.status, 'PENDING')
