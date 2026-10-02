"""
MRA EIS Admin Configuration
"""
from django.contrib import admin
from django.utils.html import format_html
from .models import (
    Terminal, TerminalActivationCode, MRAConfiguration,
    MRAInvoice, OfflineInvoiceQueue, OfflineAuditLog, Receipt,
    InvoiceAuditLog, TerminalAuditLog, MRAAPIError, SyncRetryQueue,
    ConfigurationSyncLog
)


@admin.register(Terminal)
class TerminalAdmin(admin.ModelAdmin):
    list_display = [
        'terminal_id', 'business', 'branch', 'status_badge',
        'is_online_badge', 'mra_taxpayer_id', 'terminal_position',
        'online_invoice_counter', 'offline_invoice_counter',
        'activated_at', 'last_sync_at'
    ]
    list_filter = ['status', 'is_online', 'os_type', 'created_at']
    search_fields = ['terminal_id', 'mra_terminal_id', 'device_serial']
    readonly_fields = [
        'id', 'terminal_id', 'online_invoice_counter', 'offline_invoice_counter',
        'credential_status', 'created_at', 'updated_at'
    ]
    fieldsets = (
        ('Identification', {
            'fields': ('id', 'terminal_id', 'device_serial', 'mac_address')
        }),
        ('Business', {
            'fields': ('business', 'branch')
        }),
        ('POS Information', {
            'fields': ('pos_name', 'pos_version', 'os_type')
        }),
        ('MRA Connection', {
            'fields': (
                'mra_terminal_id', 'mra_taxpayer_id', 'terminal_position',
                'credential_status', 'token_expires_at'
            ),
            'classes': ('collapse',)
        }),
        ('Status', {
            'fields': ('status', 'is_online')
        }),
        ('Counters', {
            'fields': ('online_invoice_counter', 'offline_invoice_counter')
        }),
        ('Audit', {
            'fields': ('activated_at', 'last_sync_at', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def status_badge(self, obj):
        colors = {
            'pending_activation': 'orange',
            'active': 'green',
            'suspended': 'red',
            'deactivated': 'gray'
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 3px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'

    def is_online_badge(self, obj):
        color = 'green' if obj.is_online else 'red'
        text = 'Online' if obj.is_online else 'Offline'
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 3px;">{}</span>',
            color, text
        )
    is_online_badge.short_description = 'Connectivity'

    def credential_status(self, obj):
        return 'Stored securely' if obj.mra_api_key or obj.mra_token else 'Not available'
    credential_status.short_description = 'Credentials'


@admin.register(TerminalActivationCode)
class TerminalActivationCodeAdmin(admin.ModelAdmin):
    list_display = ['code', 'business', 'status', 'expires_at', 'used_by_terminal', 'used_at']
    list_filter = ['status', 'created_at', 'expires_at']
    search_fields = ['code', 'business__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    fieldsets = (
        ('Code Information', {
            'fields': ('id', 'code', 'business', 'status')
        }),
        ('Usage', {
            'fields': ('used_by_terminal', 'used_at')
        }),
        ('Validity', {
            'fields': ('expires_at',)
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(MRAConfiguration)
class MRAConfigurationAdmin(admin.ModelAdmin):
    list_display = ['config_type', 'config_version', 'business', 'is_active', 'effective_from', 'effective_to']
    list_filter = ['config_type', 'is_active', 'effective_from']
    search_fields = ['business__name', 'config_version']
    readonly_fields = ['id', 'created_at']
    fieldsets = (
        ('Configuration', {
            'fields': ('id', 'business', 'config_type', 'config_version')
        }),
        ('Data', {
            'fields': ('config_data',)
        }),
        ('Validity', {
            'fields': ('effective_from', 'effective_to', 'is_active')
        }),
        ('Audit', {
            'fields': ('fetched_from_mra_at', 'created_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(MRAInvoice)
class MRAInvoiceAdmin(admin.ModelAdmin):
    list_display = [
        'invoice_number', 'terminal', 'seller_tin', 'status_badge',
        'gross_amount', 'is_online', 'invoice_date', 'submitted_at'
    ]
    list_filter = ['status', 'is_online', 'invoice_date', 'created_at']
    search_fields = ['invoice_number', 'seller_tin', 'mra_invoice_id']
    readonly_fields = [
        'id', 'invoice_number', 'invoice_signature', 'created_at', 'updated_at'
    ]
    fieldsets = (
        ('Invoice Identification', {
            'fields': ('id', 'invoice_number', 'mra_invoice_id', 'terminal')
        }),
        ('Seller Information', {
            'fields': ('seller_tin', 'seller_name')
        }),
        ('Buyer Information', {
            'fields': ('buyer_tin', 'buyer_name')
        }),
        ('Items', {
            'fields': ('items',)
        }),
        ('Amounts', {
            'fields': ('net_amount', 'tax_amount', 'gross_amount', 'tax_breakdown')
        }),
        ('Signature', {
            'fields': ('invoice_signature',),
            'classes': ('collapse',)
        }),
        ('Status', {
            'fields': ('status', 'is_online', 'invoice_date', 'submitted_at')
        }),
        ('MRA Response', {
            'fields': ('mra_response',),
            'classes': ('collapse',)
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def status_badge(self, obj):
        colors = {
            'draft': 'gray',
            'submitted': 'blue',
            'accepted': 'green',
            'rejected': 'red',
            'offline_queued': 'orange',
            'offline_synced': 'green',
            'expired': 'darkred',
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 3px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'


@admin.register(OfflineInvoiceQueue)
class OfflineInvoiceQueueAdmin(admin.ModelAdmin):
    list_display = [
        'queue_position', 'terminal', 'mra_invoice', 'status_badge',
        'sync_attempts', 'created_at', 'synced_at'
    ]
    list_filter = ['status', 'created_at', 'synced_at']
    search_fields = ['terminal__terminal_id', 'mra_invoice__invoice_number']
    readonly_fields = ['id', 'created_at']
    fieldsets = (
        ('Queue Entry', {
            'fields': ('id', 'terminal', 'mra_invoice', 'queue_position')
        }),
        ('Status', {
            'fields': ('status', 'sync_attempts', 'last_sync_attempt_at')
        }),
        ('Error', {
            'fields': ('last_sync_error',),
            'classes': ('collapse',)
        }),
        ('Audit', {
            'fields': ('created_at', 'synced_at'),
            'classes': ('collapse',)
        }),
    )

    def status_badge(self, obj):
        colors = {
            'queued': 'orange',
            'syncing': 'blue',
            'synced': 'green',
            'failed': 'red',
            'expired': 'darkred',
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 3px;">{}</span>',
            color, obj.get_status_display()
        )
    status_badge.short_description = 'Status'


@admin.register(OfflineAuditLog)
class OfflineAuditLogAdmin(admin.ModelAdmin):
    list_display = ['event_type', 'terminal', 'created_at']
    list_filter = ['event_type', 'created_at']
    search_fields = ['terminal__terminal_id']
    readonly_fields = ['id', 'created_at']


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ['receipt_number', 'mra_invoice', 'generated_at']
    list_filter = ['generated_at']
    search_fields = ['receipt_number', 'mra_invoice__invoice_number']
    readonly_fields = ['id', 'generated_at']


@admin.register(InvoiceAuditLog)
class InvoiceAuditLogAdmin(admin.ModelAdmin):
    list_display = ['action', 'mra_invoice', 'user', 'created_at']
    list_filter = ['action', 'created_at']
    search_fields = ['mra_invoice__invoice_number', 'user__email']
    readonly_fields = ['id', 'created_at']


@admin.register(TerminalAuditLog)
class TerminalAuditLogAdmin(admin.ModelAdmin):
    list_display = ['action', 'terminal', 'created_at']
    list_filter = ['action', 'created_at']
    search_fields = ['terminal__terminal_id']
    readonly_fields = ['id', 'created_at']


@admin.register(MRAAPIError)
class MRAAPIErrorAdmin(admin.ModelAdmin):
    list_display = [
        'error_type', 'terminal', 'error_code', 'retry_count',
        'is_resolved', 'created_at'
    ]
    list_filter = ['error_type', 'is_resolved', 'created_at']
    search_fields = ['terminal__terminal_id', 'error_message']
    readonly_fields = ['id', 'created_at', 'resolved_at']


@admin.register(SyncRetryQueue)
class SyncRetryQueueAdmin(admin.ModelAdmin):
    list_display = [
        'operation_type', 'terminal', 'status', 'attempt_count',
        'next_attempt_at', 'created_at'
    ]
    list_filter = ['operation_type', 'status', 'created_at']
    search_fields = ['terminal__terminal_id']
    readonly_fields = ['id', 'created_at', 'completed_at']


@admin.register(ConfigurationSyncLog)
class ConfigurationSyncLogAdmin(admin.ModelAdmin):
    list_display = ['business', 'status', 'config_types', 'started_at', 'completed_at']
    list_filter = ['status', 'started_at']
    search_fields = ['business__name']
    readonly_fields = ['id', 'created_at']
