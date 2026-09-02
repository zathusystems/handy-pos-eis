from django.contrib import admin
from .models import Session, Order, OrderItem, DiscountRule


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ('created_at', 'updated_at', 'batch_consumption')
    fields = (
        'inventory_item_id',
        'name',
        'quantity',
        'price',
        'discount_name',
        'discount_amount',
        'tax_rate',
        'tax_type',
        'tax_calculation_method',
        'subtotal',
        'tax_amount',
        'total',
        'notes',
        'batch_consumption',
    )


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = [
        'order_number',
        'buyer_name_display',
        'buyer_phone_display',
        'buyer_tin_display',
        'branch',
        'order_type',
        'status',
        'payment_method',
        'total',
        'created_at',
    ]
    list_filter = ['business', 'branch', 'order_type', 'status', 'payment_method', 'created_at']
    search_fields = [
        'order_number',
        'session__id',
        'customer_name',
        'customer_phone',
        'customer_tin',
        'customer_email',
        'buyer_name',
        'buyer_tin',
    ]
    readonly_fields = ['created_at', 'updated_at']
    inlines = [OrderItemInline]
    fieldsets = (
        ('Business & Branch', {
            'fields': ('business', 'branch', 'session')
        }),
        ('Order Information', {
            'fields': ('order_number', 'order_type', 'status', 'payment_method')
        }),
        ('Buyer Details', {
            'fields': (
                'customer_name',
                'customer_phone',
                'customer_tin',
                'customer_email',
                'customer_address',
                'customer_notes',
                'buyer_name',
                'buyer_tin',
            )
        }),
        ('Pricing', {
            'fields': ('subtotal', 'discount_amount', 'discount_metadata', 'total', 'cogs')
        }),
        ('Tax Information', {
            'fields': ('tax_rate_name', 'tax_rate_value', 'tax_type', 'vat_amount', 'net_amount', 'gross_amount'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    @admin.display(description='Buyer')
    def buyer_name_display(self, obj):
        return obj.customer_name or obj.buyer_name or '-'

    @admin.display(description='Phone')
    def buyer_phone_display(self, obj):
        return obj.customer_phone or '-'

    @admin.display(description='TIN')
    def buyer_tin_display(self, obj):
        return obj.customer_tin or obj.buyer_tin or '-'


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ['order', 'name', 'quantity', 'price', 'discount_amount', 'tax_rate', 'tax_type', 'total', 'created_at']
    list_filter = ['order__business', 'order__branch', 'tax_type', 'tax_calculation_method', 'created_at']
    search_fields = ['name', 'order__order_number', 'inventory_item_id']
    readonly_fields = ['created_at', 'updated_at', 'batch_consumption']
    
    fieldsets = (
        ('Order & Item Info', {
            'fields': ('order', 'inventory_item_id', 'name', 'quantity', 'notes')
        }),
        ('Pricing', {
            'fields': ('price', 'discount_rule_id', 'discount_name', 'discount_type', 'discount_value', 'discount_amount', 'subtotal', 'total')
        }),
        ('Tax Information (MRA Compliance - Immutable Snapshot)', {
            'fields': ('tax_rate', 'tax_type', 'tax_calculation_method', 'tax_amount'),
            'description': 'These fields are immutable snapshots captured at the time of sale for MRA audit trail.'
        }),
        ('MRA Product Mapping', {
            'fields': ('mra_product_code', 'vat_category'),
            'classes': ('collapse',)
        }),
        ('Batch Consumption Trace', {
            'fields': ('batch_consumption',),
            'classes': ('collapse',),
        }),
        ('Metadata', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'branch', 'status', 'started_at', 'closed_at']
    list_filter = ['status', 'started_at', 'branch']


@admin.register(DiscountRule)
class DiscountRuleAdmin(admin.ModelAdmin):
    list_display = ['name', 'business', 'branch', 'discount_type', 'value', 'applies_to', 'is_active', 'starts_at', 'ends_at']
    list_filter = ['business', 'branch', 'discount_type', 'applies_to', 'is_active']
    search_fields = ['name', 'business__name', 'branch__name']
    readonly_fields = ['created_at', 'updated_at']
    search_fields = ['user__email', 'branch__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    
    fieldsets = (
        ('Session Info', {
            'fields': ('id', 'business', 'branch', 'user', 'status')
        }),
        ('Cash Tracking', {
            'fields': ('opening_float', 'expected_cash', 'actual_cash', 'closing_float', 'difference')
        }),
        ('Sales Summary', {
            'fields': (
                'total_sales',
                'total_cash_sales',
                'total_card_sales',
                'total_mobile_money_sales',
                'total_on_account_sales',
                'total_other_sales',
                'total_tips'
            )
        }),
        ('Stock', {
            'fields': ('opening_stock', 'closing_stock')
        }),
        ('Timestamps', {
            'fields': ('started_at', 'closed_at', 'created_at', 'updated_at')
        }),
    )
