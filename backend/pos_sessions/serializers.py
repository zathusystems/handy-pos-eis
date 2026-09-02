from rest_framework import serializers
from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from .models import Session, Order, OrderItem, DiscountRule
from .tax_utils import (
    calculate_tax_snapshot,
    get_default_tax_rate,
    lock_tax_rate_on_use,
)
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _to_decimal(value, default=Decimal('0')):
    if value in (None, ''):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _money(value):
    return _to_decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = [
            'id',
            'order',
            'inventory_item_id',
            'name',
            'quantity',
            'price',
            'notes',
            'discount_rule_id',
            'discount_name',
            'discount_type',
            'discount_value',
            'discount_amount',
            # MRA PRODUCT MAPPING
            'mra_product_code',
            'vat_category',
            # TAX INFORMATION (MRA Compliance - Immutable snapshot)
            'tax_rate',
            'tax_type',
            'tax_calculation_method',
            # CALCULATED TAX AMOUNTS (Immutable snapshot for audit trail)
            'subtotal',
            'tax_amount',
            'total',
            'batch_consumption',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'order', 'batch_consumption']
    
    def to_internal_value(self, data):
        """Convert camelCase from frontend to snake_case for backend"""
        # Prefer explicit inventoryItemId when present.
        # Some clients also send `id` as an order-item UUID, which must not
        # override inventory reference and break stock decrements.
        inventory_item_id = (
            data.get('inventoryItemId')
            or data.get('inventory_item_id')
            or data.get('id')
        )

        field_mapping = {
            'createdAt': 'created_at',
            'updatedAt': 'updated_at',
            'mraProductCode': 'mra_product_code',
            'vatCategory': 'vat_category',
            'taxRate': 'tax_rate',
            'taxType': 'tax_type',
            'taxCalculationMethod': 'tax_calculation_method',
            'taxAmount': 'tax_amount',
            'discountRuleId': 'discount_rule_id',
            'discountName': 'discount_name',
            'discountType': 'discount_type',
            'discountValue': 'discount_value',
            'discountAmount': 'discount_amount',
        }

        converted_data = {}
        for key, value in data.items():
            # Avoid mapping `id` directly; we normalize inventory_item_id above.
            if key in {'id', 'inventoryItemId', 'inventory_item_id'}:
                continue
            converted_data[field_mapping.get(key, key)] = value

        converted_data['inventory_item_id'] = inventory_item_id
        return super().to_internal_value(converted_data)

    def validate(self, attrs):
        attrs = super().validate(attrs)

        quantity = _to_decimal(attrs.get('quantity'))
        price = _to_decimal(attrs.get('price'))
        line_amount = _money(price * quantity)
        if line_amount <= 0:
            line_amount = _money(
                _to_decimal(attrs.get('subtotal')) + _to_decimal(attrs.get('tax_amount'))
            )
        if line_amount <= 0:
            line_amount = _money(attrs.get('total'))

        discount_type = str(attrs.get('discount_type') or '').strip().lower()
        discount_value = _money(attrs.get('discount_value'))
        discount_amount = _money(attrs.get('discount_amount'))

        if discount_type == 'percentage' and discount_value >= Decimal('100'):
            raise serializers.ValidationError({
                'discount_value': 'Discount must be less than 100%.',
            })
        if line_amount > 0 and discount_type == 'fixed' and discount_value >= line_amount:
            raise serializers.ValidationError({
                'discount_value': 'Fixed discount must be less than the item total.',
            })
        if line_amount > 0 and discount_amount >= line_amount and discount_amount > 0:
            raise serializers.ValidationError({
                'discount_amount': 'Discount must be less than the item total.',
            })

        return attrs


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, required=False)
    eis_sync_state = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'business',
            'branch',
            'session',
            'order_number',
            'order_type',
            'status',
            'payment_method',
            'pump_name',
            'customer_name',
            'customer_phone',
            'customer_tin',
            'customer_email',
            'customer_address',
            'customer_notes',
            'buyer_name',
            'buyer_tin',
            'buyer_authorization_code',
            'is_export',
            'is_relief_supply',
            'vat5_project_number',
            'vat5_certificate_number',
            'vat5_quantity',
            'eis_validation_metadata',
            'subtotal',
            'discount_amount',
            'discount_metadata',
            'total',
            'cogs',
            # Tax snapshot fields (MRA compliance)
            'tax_rate_name',
            'tax_rate_value',
            'tax_type',
            'vat_amount',
            'net_amount',
            'gross_amount',
            # MRA EIS fields (NEW)
            'fiscal_invoice_number',
            'eis_uuid',
            'eis_status',
            'eis_sync_state',
            'eis_submitted_at',
            'qr_code_payload',
            'digital_signature',
            'is_fiscal_locked',
            'created_at',
            'updated_at',
            'items'
        ]
        read_only_fields = ['created_at', 'updated_at', 'business', 'fiscal_invoice_number', 'eis_uuid', 'eis_submitted_at', 'qr_code_payload', 'digital_signature', 'is_fiscal_locked', 'eis_validation_metadata']

    def get_eis_sync_state(self, obj):
        metadata = obj.eis_validation_metadata if isinstance(obj.eis_validation_metadata, dict) else {}
        submission = metadata.get('mra_submission') if isinstance(metadata.get('mra_submission'), dict) else {}
        submission_state = str(submission.get('state') or '').strip().lower()
        if submission_state == 'offline_queued' or bool(submission.get('queued_offline')):
            return 'OFFLINE_QUEUED'

        status = str(getattr(obj, 'eis_status', '') or '').upper()
        return {
            'PENDING': 'PENDING',
            'SUBMITTED': 'SENDING',
            'ACCEPTED': 'SUCCESS',
            'REJECTED': 'FAILED',
        }.get(status, 'PENDING')
    
    def to_internal_value(self, data):
        """Convert camelCase from frontend to snake_case for backend"""
        # Map camelCase to snake_case
        field_mapping = {
            'orderNumber': 'order_number',
            'branchId': 'branch',
            'sessionId': 'session',
            'orderType': 'order_type',
            'paymentMethod': 'payment_method',
            'pumpName': 'pump_name',
            'customerName': 'customer_name',
            'customerPhone': 'customer_phone',
            'customerTin': 'customer_tin',
            'customerEmail': 'customer_email',
            'customerAddress': 'customer_address',
            'customerNotes': 'customer_notes',
            'buyerName': 'buyer_name',
            'buyerTin': 'buyer_tin',
            'buyerAuthorizationCode': 'buyer_authorization_code',
            'isExport': 'is_export',
            'isReliefSupply': 'is_relief_supply',
            'vat5ProjectNumber': 'vat5_project_number',
            'vat5CertificateNumber': 'vat5_certificate_number',
            'vat5Quantity': 'vat5_quantity',
            'createdAt': 'created_at',
            'updatedAt': 'updated_at',
            'discountAmount': 'discount_amount',
            'discountMetadata': 'discount_metadata',
        }
        
        # Convert camelCase keys to snake_case
        converted_data = {}
        for key, value in data.items():
            new_key = field_mapping.get(key, key)
            converted_data[new_key] = value
        
        return super().to_internal_value(converted_data)
    
    def create(self, validated_data):
        """Create order with UUID from frontend and calculate tax snapshot"""
        with transaction.atomic():
            from inventory.models import InventoryItem

            # Allow frontend to provide the UUID
            order_id = validated_data.pop('id', None)

            # Extract items before creating order
            items_data = validated_data.pop('items', [])

            # Get business from branch if not provided
            branch = validated_data.get('branch')
            if branch and 'business' not in validated_data:
                validated_data['business'] = branch.business

            business = validated_data.get('business')

            # Calculate tax snapshot BEFORE creating the order.
            # This captures exact tax values at the moment of sale.
            subtotal = Decimal(str(validated_data.get('subtotal', 0)))
            applied_tax_rate = get_default_tax_rate(business) if business else None
            tax_snapshot = calculate_tax_snapshot(subtotal, business, applied_tax_rate)

            # Apply tax snapshot to validated data
            validated_data['tax_rate_name'] = tax_snapshot['tax_rate_name']
            validated_data['tax_rate_value'] = tax_snapshot['tax_rate_value']
            validated_data['tax_type'] = tax_snapshot['tax_type']
            validated_data['vat_amount'] = tax_snapshot['vat_amount']
            validated_data['net_amount'] = tax_snapshot['net_amount']
            validated_data['gross_amount'] = tax_snapshot['gross_amount']

            # Create the order with tax snapshot fields
            order = Order.objects.create(id=order_id, **validated_data)

            # Create order items
            for item_data in items_data:
                inventory_item_id = str(item_data.get('inventory_item_id') or '').strip()
                item_name = str(item_data.get('name', '') or '').strip()

                if inventory_item_id:
                    inventory_match = InventoryItem.objects.filter(
                        id=inventory_item_id,
                        business=business,
                        branch=branch,
                    ).first()
                else:
                    inventory_match = None

                if not inventory_match and item_name:
                    name_matches = list(
                        InventoryItem.objects.filter(
                            name__iexact=item_name,
                            business=business,
                            branch=branch,
                        )[:2]
                    )
                    if len(name_matches) == 1:
                        inventory_item_id = str(name_matches[0].id)

                if inventory_item_id:
                    item_data['inventory_item_id'] = inventory_item_id

                OrderItem.objects.create(order=order, **item_data)

            # Once used in a transaction, lock the tax rate for fiscal immutability.
            lock_tax_rate_on_use(applied_tax_rate)

            return order


class DiscountRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiscountRule
        fields = [
            'id',
            'business',
            'branch',
            'name',
            'discount_type',
            'value',
            'applies_to',
            'product_ids',
            'categories',
            'starts_at',
            'ends_at',
            'is_active',
            'requires_manager_approval',
            'created_by',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def to_internal_value(self, data):
        field_mapping = {
            'discountType': 'discount_type',
            'appliesTo': 'applies_to',
            'productIds': 'product_ids',
            'startsAt': 'starts_at',
            'endsAt': 'ends_at',
            'isActive': 'is_active',
            'requiresManagerApproval': 'requires_manager_approval',
        }
        converted_data = {}
        for key, value in data.items():
            converted_data[field_mapping.get(key, key)] = value
        return super().to_internal_value(converted_data)

    def validate(self, attrs):
        candidate = {}
        for field in DiscountRule._meta.fields:
            if field.name in {'id', 'created_at', 'updated_at'}:
                continue
            if self.instance is not None:
                candidate[field.name] = getattr(self.instance, field.name)
        candidate.update(attrs)
        instance = DiscountRule(**candidate)
        try:
            instance.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, 'message_dict') else exc.messages)
        return attrs


class SessionSerializer(serializers.ModelSerializer):
    orders = OrderSerializer(many=True, read_only=True)
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    total_sales = serializers.SerializerMethodField()
    total_cash_sales = serializers.SerializerMethodField()
    total_card_sales = serializers.SerializerMethodField()
    total_mobile_money_sales = serializers.SerializerMethodField()
    total_on_account_sales = serializers.SerializerMethodField()
    total_other_sales = serializers.SerializerMethodField()
    total_tips = serializers.SerializerMethodField()
    expected_cash = serializers.SerializerMethodField()

    class Meta:
        model = Session
        fields = [
            'id',
            'business',
            'branch',
            'user',
            'user_name',
            'user_email',
            'status',
            'pump_name',
            'opening_float',
            'expected_cash',
            'actual_cash',
            'closing_float',
            'difference',
            'total_sales',
            'total_cash_sales',
            'total_card_sales',
            'total_mobile_money_sales',
            'total_on_account_sales',
            'total_other_sales',
            'total_tips',
            'opening_stock',
            'closing_stock',
            'started_at',
            'closed_at',
            'created_at',
            'updated_at',
            'orders',
        ]
        read_only_fields = ['id', 'business', 'user', 'user_name', 'created_at', 'updated_at']

    def to_internal_value(self, data):
        """Accept pumpName in camelCase payloads."""
        if isinstance(data, dict) and 'pumpName' in data and 'pump_name' not in data:
            data = {**data, 'pump_name': data.get('pumpName')}
        return super().to_internal_value(data)
    
    def get_user_name(self, obj):
        """Get the user's display name"""
        if obj.user:
            # Try first_name + last_name, then first_name, then email, then phone
            if obj.user.first_name and obj.user.last_name:
                return f"{obj.user.first_name} {obj.user.last_name}"
            elif obj.user.first_name:
                return obj.user.first_name
            elif obj.user.email:
                return obj.user.email
            elif obj.user.phone:
                return obj.user.phone
        return 'Unknown User'
    
    def get_user_email(self, obj):
        """Get the user's email"""
        if obj.user:
            return obj.user.email or ''
        return ''
    
    def get_total_sales(self, obj):
        """Calculate total sales excluding voided and cancelled orders"""
        from django.db.models import Sum
        total = Order.objects.filter(
            session=obj,
            status__in=['New', 'Preparing', 'Ready', 'Completed']
        ).aggregate(Sum('subtotal'))['subtotal__sum'] or Decimal('0')
        return float(total)
    
    def get_total_cash_sales(self, obj):
        """Calculate cash sales excluding voided and cancelled orders"""
        from django.db.models import Sum
        total = Order.objects.filter(
            session=obj,
            payment_method='Cash',
            status__in=['New', 'Preparing', 'Ready', 'Completed']
        ).aggregate(Sum('total'))['total__sum'] or Decimal('0')
        return float(total)
    
    def get_total_card_sales(self, obj):
        """Calculate card sales excluding voided and cancelled orders"""
        from django.db.models import Sum
        total = Order.objects.filter(
            session=obj,
            payment_method='Card',
            status__in=['New', 'Preparing', 'Ready', 'Completed']
        ).aggregate(Sum('total'))['total__sum'] or Decimal('0')
        return float(total)
    
    def get_total_mobile_money_sales(self, obj):
        """Calculate mobile money sales excluding voided and cancelled orders"""
        from django.db.models import Sum
        total = Order.objects.filter(
            session=obj,
            payment_method='Mobile Money',
            status__in=['New', 'Preparing', 'Ready', 'Completed']
        ).aggregate(Sum('total'))['total__sum'] or Decimal('0')
        return float(total)
    
    def get_total_on_account_sales(self, obj):
        """Calculate on account sales excluding voided and cancelled orders"""
        from django.db.models import Sum
        total = Order.objects.filter(
            session=obj,
            payment_method='On Account',
            status__in=['New', 'Preparing', 'Ready', 'Completed']
        ).aggregate(Sum('total'))['total__sum'] or Decimal('0')
        return float(total)
    
    def get_total_other_sales(self, obj):
        """Calculate other sales excluding voided and cancelled orders"""
        from django.db.models import Sum
        total = Order.objects.filter(
            session=obj,
            payment_method='Other',
            status__in=['New', 'Preparing', 'Ready', 'Completed']
        ).aggregate(Sum('total'))['total__sum'] or Decimal('0')
        return float(total)
    
    def get_total_tips(self, obj):
        """Return the session's synced tip total.

        Tips are tracked on the session aggregate because the backend Order model
        no longer stores a per-order tip field.
        """
        return float(obj.total_tips or 0)
    
    def get_expected_cash(self, obj):
        """Calculate expected cash (opening float + cash sales) excluding voided orders"""
        from django.db.models import Sum
        cash_sales = Order.objects.filter(
            session=obj,
            payment_method='Cash',
            status__in=['New', 'Preparing', 'Ready', 'Completed']
        ).aggregate(Sum('total'))['total__sum'] or Decimal('0')
        expected = (obj.opening_float or 0) + cash_sales
        return float(expected)
    
    def to_internal_value(self, data):
        """Convert camelCase from frontend to snake_case for backend"""
        print('[SessionSerializer] Raw data received:', data)
        
        # Map camelCase to snake_case
        field_mapping = {
            'openingFloat': 'opening_float',
            'expectedCash': 'expected_cash',
            'actualCash': 'actual_cash',
            'closingFloat': 'closing_float',
            'totalSales': 'total_sales',
            'totalCashSales': 'total_cash_sales',
            'totalCardSales': 'total_card_sales',
            'totalMobileMoneySales': 'total_mobile_money_sales',
            'totalOnAccountSales': 'total_on_account_sales',
            'totalOtherSales': 'total_other_sales',
            'totalTips': 'total_tips',
            'openingStock': 'opening_stock',
            'closingStock': 'closing_stock',
            'startedAt': 'started_at',
            'closedAt': 'closed_at',
            'createdAt': 'created_at',
            'updatedAt': 'updated_at',
        }
        
        # Convert camelCase keys to snake_case
        converted_data = {}
        for key, value in data.items():
            new_key = field_mapping.get(key, key)
            converted_data[new_key] = value
        
        print('[SessionSerializer] Converted data:', converted_data)
        print('[SessionSerializer] Closing stock in converted data:', converted_data.get('closing_stock'))
        
        try:
            result = super().to_internal_value(converted_data)
            print('[SessionSerializer] Validation result:', result)
            print('[SessionSerializer] Closing stock in validation result:', result.get('closing_stock'))
            return result
        except serializers.ValidationError as e:
            print('[SessionSerializer] Validation error:', e.detail)
            raise
    
    def create(self, validated_data):
        """Create session with UUID from frontend"""
        print('[SessionSerializer] Creating session with validated_data:', validated_data)
        
        # Allow frontend to provide the UUID
        session_id = validated_data.pop('id', None)
        print('[SessionSerializer] Session ID:', session_id)
        
        # expected_cash is a required DB column but exposed as a computed field in the API.
        # Ensure it is always populated on create to prevent NOT NULL integrity failures.
        opening_float = Decimal(str(validated_data.get('opening_float') or 0))
        expected_cash = validated_data.get('expected_cash')
        if expected_cash is None:
            validated_data['expected_cash'] = opening_float
        else:
            validated_data['expected_cash'] = Decimal(str(expected_cash))
        
        # Get user from request context
        request = self.context.get('request')
        if request and request.user:
            validated_data['user'] = request.user
            print('[SessionSerializer] User from request:', request.user)
        else:
            print('[SessionSerializer] WARNING: No user in request context')
        
        print('[SessionSerializer] Remaining validated_data:', validated_data)
        
        # Create the session with the provided UUID
        try:
            if session_id:
                session = Session.objects.create(id=session_id, **validated_data)
            else:
                session = Session.objects.create(**validated_data)
            print('[SessionSerializer] Session created successfully:', session.id)
            return session
        except Exception as e:
            print('[SessionSerializer] Error creating session:', str(e))
            raise
