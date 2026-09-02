"""
MRA EIS-Compliant Business Serializers

Provides serialization for business models with MRA compliance:
- Taxpayer identity
- Branch tracking
- Tax immutability
- Invoice immutability
- Relational line items
"""

from rest_framework import serializers
from decimal import Decimal
from django.core.exceptions import ValidationError as DjangoValidationError

from .models import (
    Business, Branch, BusinessSettings, TaxRate, Invoice, InvoiceLine,
    Customer, Expense
)


# ============================================================================
# CUSTOMER SERIALIZERS
# ============================================================================

class CustomerSerializer(serializers.ModelSerializer):
    """Customer serializer with VAT tracking"""
    class Meta:
        model = Customer
        fields = [
            'id', 'business', 'branch', 'name', 'email', 'phone', 'address',
            'customer_tin', 'vat_registered', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'created_at', 'updated_at']


class CustomerCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating customers"""
    class Meta:
        model = Customer
        fields = [
            'name', 'email', 'phone', 'address', 'customer_tin', 'vat_registered'
        ]


# ============================================================================
# INVOICE LINE SERIALIZERS (NEW - CRITICAL)
# ============================================================================

class InvoiceLineSerializer(serializers.ModelSerializer):
    """Serializer for invoice line items (relational storage)"""
    class Meta:
        model = InvoiceLine
        fields = [
            'id', 'product_code', 'product_name', 'quantity', 'unit_price',
            'tax_rate', 'tax_amount', 'total_amount', 'mra_product_code',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class InvoiceLineCreateSerializer(serializers.Serializer):
    """Serializer for creating invoice line items"""
    product_code = serializers.CharField(max_length=100)
    product_name = serializers.CharField(max_length=255)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    mra_product_code = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be positive")
        return value

    def validate_unit_price(self, value):
        if value < 0:
            raise serializers.ValidationError("Unit price cannot be negative")
        return value

    def validate_tax_rate(self, value):
        if value < 0 or value > 100:
            raise serializers.ValidationError("Tax rate must be between 0 and 100")
        return value


# ============================================================================
# INVOICE SERIALIZERS (Enhanced for MRA EIS)
# ============================================================================

class InvoiceSerializer(serializers.ModelSerializer):
    """Invoice serializer with MRA EIS fields"""
    lines = InvoiceLineSerializer(many=True, read_only=True)
    customer_name_display = serializers.CharField(source='customer.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'business', 'branch', 'branch_name', 'customer', 'customer_name_display',
            'invoice_number', 'customer_name', 'status', 'approval_status', 'lines',
            'subtotal', 'tax', 'total', 'issue_date', 'due_date', 'notes',
            'related_order_id', 'approved_by', 'approved_at',
            # MRA EIS fields
            'mra_invoice_number', 'mra_status', 'mra_receipt_signature',
            'mra_qr_code', 'mra_submitted_at', 'is_locked',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'business', 'created_at', 'updated_at', 'is_locked',
            'mra_invoice_number', 'mra_receipt_signature', 'mra_qr_code',
            'mra_submitted_at'
        ]


class InvoiceDetailSerializer(InvoiceSerializer):
    """Detailed invoice serializer with all fields"""
    pass


class InvoiceCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating invoices with line items"""
    lines = InvoiceLineCreateSerializer(many=True, write_only=True)

    class Meta:
        model = Invoice
        fields = [
            'invoice_number', 'customer', 'customer_name', 'status',
            'lines', 'subtotal', 'tax', 'total', 'issue_date', 'due_date', 'notes'
        ]

    def validate_lines(self, value):
        """Validate line items"""
        if not value:
            raise serializers.ValidationError("Invoice must have at least one line item")
        return value

    def create(self, validated_data):
        """Create invoice with line items"""
        business = self.context['request'].user.businesses.first()
        if not business:
            raise serializers.ValidationError('User must have a business')

        # Extract lines
        lines_data = validated_data.pop('lines', [])

        # Create invoice
        validated_data['business'] = business
        invoice = super().create(validated_data)

        # Create line items
        total_tax = Decimal('0')
        total_amount = Decimal('0')

        for line_data in lines_data:
            InvoiceLine.objects.create(
                invoice=invoice,
                product_code=line_data['product_code'],
                product_name=line_data['product_name'],
                quantity=line_data['quantity'],
                unit_price=line_data['unit_price'],
                tax_rate=line_data['tax_rate'],
                tax_amount=line_data['tax_amount'],
                total_amount=line_data['total_amount'],
                mra_product_code=line_data.get('mra_product_code', ''),
            )
            total_tax += line_data['tax_amount']
            total_amount += line_data['total_amount']

        # Update invoice totals
        invoice.subtotal = total_amount - total_tax
        invoice.tax = total_tax
        invoice.total = total_amount
        invoice.save()

        return invoice


class InvoiceUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating invoices (with immutability check)"""
    class Meta:
        model = Invoice
        fields = [
            'customer', 'customer_name', 'status', 'approval_status',
            'subtotal', 'tax', 'total', 'issue_date', 'due_date', 'notes'
        ]

    def update(self, instance, validated_data):
        """Update invoice with immutability check"""
        if instance.is_locked:
            raise serializers.ValidationError(
                "Cannot modify a locked invoice. This invoice has been paid or submitted to MRA."
            )
        return super().update(instance, validated_data)


# ============================================================================
# TAX RATE SERIALIZERS (Enhanced for MRA EIS)
# ============================================================================

class TaxRateSerializer(serializers.ModelSerializer):
    """Tax rate serializer with immutability tracking"""
    created_by_name = serializers.CharField(
        source='created_by.get_full_name',
        read_only=True,
        allow_null=True
    )

    class Meta:
        model = TaxRate
        fields = [
            'id', 'business', 'name', 'rate', 'tax_type', 'is_default',
            'effective_from', 'effective_to', 'mra_tax_code', 'locked',
            'is_active', 'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'created_by', 'created_by_name', 'locked'
        ]


class TaxRateCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating tax rates"""
    class Meta:
        model = TaxRate
        fields = [
            'name', 'rate', 'tax_type', 'is_default', 'effective_from',
            'effective_to', 'mra_tax_code'
        ]

    def validate_rate(self, value):
        """Validate tax rate"""
        if value < 0 or value > 100:
            raise serializers.ValidationError("Tax rate must be between 0 and 100")
        return value


class TaxRateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating tax rates (with immutability check)"""
    class Meta:
        model = TaxRate
        fields = [
            'name', 'is_default', 'effective_to', 'is_active'
        ]

    def update(self, instance, validated_data):
        """Update tax rate with immutability check"""
        if instance.locked:
            raise serializers.ValidationError(
                "Cannot modify a locked tax rate. Create a new tax rate instead."
            )
        return super().update(instance, validated_data)


# ============================================================================
# BUSINESS SETTINGS SERIALIZERS (Enhanced for MRA EIS)
# ============================================================================

class BusinessSettingsSerializer(serializers.ModelSerializer):
    """Business settings serializer with EIS controls"""
    class Meta:
        model = BusinessSettings
        fields = [
            'id', 'currency', 'timezone', 'enable_inventory', 'enable_invoicing',
            'enable_pos', 'enable_kitchen', 'enable_delivery', 'fuel_pumps',
            # MRA EIS fields
            'enable_eis', 'eis_environment', 'block_sales_if_eis_down',
            'block_sales_if_tax_mapping_missing',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# ============================================================================
# BRANCH SERIALIZERS (Enhanced for MRA EIS)
# ============================================================================

class BranchSerializer(serializers.ModelSerializer):
    """Branch serializer with MRA tracking"""
    class Meta:
        model = Branch
        fields = [
            'id', 'business', 'name', 'slug', 'address', 'city', 'state',
            'postal_code', 'country', 'phone', 'email', 'latitude', 'longitude',
            'is_active', 'mra_branch_code', 'mra_device_location',
            'mra_site_id', 'mra_site_name', 'mra_terminal_id',
            'mra_terminal_position', 'is_eis_warehouse', 'eis_mapping_source',
            'eis_mapping_updated_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'slug', 'eis_mapping_updated_at', 'created_at', 'updated_at']


class BranchCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating branches"""
    class Meta:
        model = Branch
        fields = [
            'name', 'address', 'city', 'state', 'postal_code', 'country',
            'phone', 'email', 'latitude', 'longitude', 'mra_branch_code',
            'mra_device_location', 'mra_site_id', 'mra_site_name',
            'mra_terminal_id', 'mra_terminal_position', 'is_eis_warehouse',
            'eis_mapping_source',
        ]


# ============================================================================
# BUSINESS SERIALIZERS (Enhanced for MRA EIS)
# ============================================================================

class BusinessTinAliasSerializerMixin:
    """
    Compatibility mixin for legacy payloads that send business TIN as
    `tax_pin` or `taxPin` instead of `tin`.
    """
    tax_pin = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        write_only=True
    )
    taxPin = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        write_only=True
    )

    @staticmethod
    def _normalize_tin(raw_value):
        if raw_value is None:
            return None
        normalized = str(raw_value).strip()
        return normalized or None

    def validate(self, attrs):
        attrs = super().validate(attrs)
        legacy_tax_pin = attrs.pop('tax_pin', serializers.empty)
        legacy_tax_pin_camel = attrs.pop('taxPin', serializers.empty)

        if 'tin' in attrs:
            attrs['tin'] = self._normalize_tin(attrs.get('tin'))
            return attrs

        for candidate in (legacy_tax_pin, legacy_tax_pin_camel):
            if candidate is serializers.empty:
                continue
            attrs['tin'] = self._normalize_tin(candidate)
            break

        return attrs

class BusinessSerializer(serializers.ModelSerializer):
    """Business serializer with MRA identity"""
    branches = BranchSerializer(many=True, read_only=True)
    settings = BusinessSettingsSerializer(read_only=True)
    tax_rates = TaxRateSerializer(many=True, read_only=True)
    tax_pin = serializers.SerializerMethodField()
    taxPin = serializers.SerializerMethodField()
    # EIS settings fields for easy access
    enable_eis = serializers.SerializerMethodField()
    eis_environment = serializers.SerializerMethodField()
    block_sales_if_eis_down = serializers.SerializerMethodField()
    block_sales_if_tax_mapping_missing = serializers.SerializerMethodField()

    class Meta:
        model = Business
        fields = [
            'id', 'owner', 'name', 'slug', 'business_type', 'description',
            'email', 'phone', 'address', 'country', 'website', 'logo',
            'is_active',
            # MRA EIS identity
            'tin', 'tax_pin', 'taxPin', 'vat_registration_number', 'vat_registered',
            'mra_taxpayer_type', 'mra_enrolled', 'mra_enrolled_at',
            # EIS settings
            'enable_eis', 'eis_environment', 'block_sales_if_eis_down',
            'block_sales_if_tax_mapping_missing',
            # Relations
            'branches', 'settings', 'tax_rates',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'owner', 'slug', 'created_at', 'updated_at', 'mra_enrolled_at'
        ]

    def get_enable_eis(self, obj):
        """Get enable_eis from related BusinessSettings"""
        return obj.settings.enable_eis if hasattr(obj, 'settings') else False

    def get_eis_environment(self, obj):
        """Get eis_environment from related BusinessSettings"""
        return obj.settings.eis_environment if hasattr(obj, 'settings') else 'TEST'

    def get_block_sales_if_eis_down(self, obj):
        """Get block_sales_if_eis_down from related BusinessSettings"""
        return obj.settings.block_sales_if_eis_down if hasattr(obj, 'settings') else True

    def get_block_sales_if_tax_mapping_missing(self, obj):
        """Get block_sales_if_tax_mapping_missing from related BusinessSettings"""
        return obj.settings.block_sales_if_tax_mapping_missing if hasattr(obj, 'settings') else False

    def get_tax_pin(self, obj):
        return obj.tin

    def get_taxPin(self, obj):
        return obj.tin


class BusinessDetailSerializer(BusinessSerializer):
    """Detailed business serializer"""
    pass


class BusinessCreateSerializer(BusinessTinAliasSerializerMixin, serializers.ModelSerializer):
    """Serializer for creating businesses"""
    # Explicitly declare legacy alias inputs so DRF treats them as serializer
    # fields (not model fields) when present in Meta.fields.
    tax_pin = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        write_only=True
    )
    taxPin = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        write_only=True
    )
    referral_code = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True
    )
    currency = serializers.CharField(
        required=False,
        allow_blank=False,
        write_only=True
    )
    referral_status = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Business
        fields = [
            'id', 'name', 'business_type', 'description', 'email', 'phone',
            'address', 'country', 'website', 'logo',
            # MRA EIS identity
            'tin', 'tax_pin', 'taxPin', 'vat_registration_number', 'vat_registered',
            'mra_taxpayer_type',
            # Initial business settings
            'currency',
            # Referral
            'referral_code', 'referral_status'
        ]
        read_only_fields = ['id']

    def get_referral_status(self, obj):
        return getattr(self, '_referral_status', None)

    def _process_referral_code(self, business, referral_code):
        """Helper method to process referral code"""
        if referral_code:
            from affiliate.models import Affiliate, BusinessReferral
            try:
                affiliate = Affiliate.objects.get(affiliate_code=referral_code)

                # Check if referral already exists
                existing_referral = BusinessReferral.objects.filter(business=business).first()
                if existing_referral:
                    self._referral_status = {
                        'valid': False,
                        'message': 'Business is already associated with an affiliate'
                    }
                    return

                # Create new referral
                BusinessReferral.objects.create(
                    affiliate=affiliate,
                    business=business,
                    referral_code=f"{affiliate.affiliate_code}-{business.id}",
                    status='active'
                )

                # Update affiliate stats
                affiliate.total_referred_businesses += 1
                affiliate.total_active_referrals += 1
                affiliate.save()

                full_name = f"{affiliate.user.first_name} {affiliate.user.last_name}".strip()
                affiliate_name = full_name or affiliate.user.email

                self._referral_status = {
                    'valid': True,
                    'message': f'Referral code applied successfully. You will earn commissions through {affiliate.user.email}',
                    'affiliate_name': affiliate_name
                }
            except Affiliate.DoesNotExist:
                self._referral_status = {
                    'valid': False,
                    'message': f'Referral code "{referral_code}" is invalid or does not exist'
                }
        else:
            self._referral_status = {
                'valid': True,
                'message': None
            }

    def create(self, validated_data):
        requested_currency = validated_data.pop('currency', None)
        self._requested_currency = requested_currency
        referral_code = validated_data.pop('referral_code', None)
        business = super().create(validated_data)
        self._process_referral_code(business, referral_code)
        return business

    def update(self, instance, validated_data):
        validated_data.pop('currency', None)
        referral_code = validated_data.pop('referral_code', None)
        business = super().update(instance, validated_data)
        if referral_code:
            self._process_referral_code(business, referral_code)
        return business

    def to_representation(self, instance):
        """Override to include referral_status in response"""
        data = super().to_representation(instance)
        if hasattr(self, '_referral_status'):
            data['referral_status'] = self._referral_status
        return data


class BusinessUpdateSerializer(BusinessTinAliasSerializerMixin, serializers.ModelSerializer):
    """Serializer for updating businesses"""
    tax_pin = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        write_only=True
    )
    taxPin = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        write_only=True
    )
    # EIS settings fields (from BusinessSettings model) - writable
    enable_eis = serializers.BooleanField(required=False, allow_null=True)
    eis_environment = serializers.CharField(required=False, allow_blank=True)
    block_sales_if_eis_down = serializers.BooleanField(required=False, allow_null=True)
    block_sales_if_tax_mapping_missing = serializers.BooleanField(required=False, allow_null=True)
    fuel_pumps = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True
    )

    class Meta:
        model = Business
        fields = [
            'id', 'name', 'business_type', 'description', 'email', 'phone',
            'address', 'country', 'website', 'logo', 'is_active',
            'tin', 'tax_pin', 'taxPin', 'vat_registration_number', 'vat_registered',
            'mra_taxpayer_type', 'mra_enrolled',
            # EIS settings
            'enable_eis', 'eis_environment', 'block_sales_if_eis_down',
            'block_sales_if_tax_mapping_missing', 'fuel_pumps',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def to_representation(self, instance):
        """Override to include EIS settings in response"""
        data = super().to_representation(instance)
        # Always include EIS settings from BusinessSettings
        if hasattr(instance, 'settings'):
            data['enable_eis'] = instance.settings.enable_eis
            data['eis_environment'] = instance.settings.eis_environment
            data['block_sales_if_eis_down'] = instance.settings.block_sales_if_eis_down
            data['block_sales_if_tax_mapping_missing'] = instance.settings.block_sales_if_tax_mapping_missing
            data['fuel_pumps'] = instance.settings.fuel_pumps
        else:
            data['enable_eis'] = False
            data['eis_environment'] = 'TEST'
            data['block_sales_if_eis_down'] = True
            data['block_sales_if_tax_mapping_missing'] = False
            data['fuel_pumps'] = []
        return data

    def update(self, instance, validated_data):
        """Update business and related settings"""
        # Extract EIS settings before calling super
        enable_eis = validated_data.pop('enable_eis', None)
        eis_environment = validated_data.pop('eis_environment', None)
        block_sales_if_eis_down = validated_data.pop('block_sales_if_eis_down', None)
        block_sales_if_tax_mapping_missing = validated_data.pop('block_sales_if_tax_mapping_missing', None)
        fuel_pumps = validated_data.pop('fuel_pumps', None)

        # Update business fields
        instance = super().update(instance, validated_data)

        # Update BusinessSettings if provided
        if any(v is not None for v in [enable_eis, eis_environment, block_sales_if_eis_down, block_sales_if_tax_mapping_missing, fuel_pumps]):
            settings = instance.settings
            if enable_eis is not None:
                settings.enable_eis = enable_eis
            if eis_environment is not None:
                settings.eis_environment = eis_environment
            if block_sales_if_eis_down is not None:
                settings.block_sales_if_eis_down = block_sales_if_eis_down
            if block_sales_if_tax_mapping_missing is not None:
                settings.block_sales_if_tax_mapping_missing = block_sales_if_tax_mapping_missing
            if fuel_pumps is not None:
                normalized_pumps = []
                for pump in fuel_pumps or []:
                    pump_value = str(pump or '').strip()
                    if not pump_value or pump_value in normalized_pumps:
                        continue
                    normalized_pumps.append(pump_value)
                settings.fuel_pumps = normalized_pumps
            settings.save()

        return instance


# ============================================================================
# EXPENSE SERIALIZERS
# ============================================================================

class ExpenseSerializer(serializers.ModelSerializer):
    """Expense serializer"""
    class Meta:
        model = Expense
        fields = [
            'id', 'business', 'branch', 'title', 'category', 'amount', 'date',
            'notes', 'status', 'created_by', 'created_at', 'updated_at',
            'approved_by', 'approved_at'
        ]
        read_only_fields = ['id', 'business', 'created_at', 'updated_at']


class ExpenseCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating expenses"""
    class Meta:
        model = Expense
        fields = [
            'title', 'category', 'amount', 'date', 'notes', 'branch'
        ]
    
    def create(self, validated_data):
        """Create expense with auto-generated ID"""
        import uuid
        # Generate unique ID for expense
        expense_id = f"EXP-{uuid.uuid4().hex[:12]}"
        validated_data['id'] = expense_id
        return super().create(validated_data)
