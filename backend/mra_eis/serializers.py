"""
MRA EIS Serializers - API request/response serialization
"""
from rest_framework import serializers
from .models import (
    Terminal, TerminalActivationCode, MRAConfiguration,
    MRAInvoice, OfflineInvoiceQueue, Receipt, InvoiceAuditLog,
    TerminalAuditLog, MRAAPIError, SyncRetryQueue
)


class TerminalActivationCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = TerminalActivationCode
        fields = ['id', 'code', 'status', 'expires_at', 'created_at']
        read_only_fields = ['id', 'created_at']


class TerminalSerializer(serializers.ModelSerializer):
    blocking_status = serializers.SerializerMethodField()

    class Meta:
        model = Terminal
        fields = [
            'id', 'business', 'branch', 'terminal_id', 'mra_terminal_id',
            'device_serial', 'mac_address',
            'pos_name', 'pos_version', 'os_type', 'mra_taxpayer_id', 'terminal_position',
            'status', 'is_online', 'blocking_status',
            'online_invoice_counter', 'offline_invoice_counter',
            'activated_at', 'last_sync_at', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'business', 'branch', 'terminal_id', 'mra_terminal_id',
            'blocking_status', 'online_invoice_counter', 'offline_invoice_counter',
            'activated_at', 'last_sync_at',
            'created_at', 'updated_at'
        ]

    def get_blocking_status(self, obj):
        try:
            from .services import TerminalService

            return TerminalService.get_cached_blocking_status(obj)
        except Exception:
            return None


class TerminalDetailSerializer(TerminalSerializer):
    """Detailed terminal info without TAC, token, or API secret exposure."""
    activation_result = serializers.SerializerMethodField()
    has_mra_token = serializers.SerializerMethodField()

    class Meta(TerminalSerializer.Meta):
        fields = TerminalSerializer.Meta.fields + [
            'token_expires_at', 'has_mra_token', 'activation_result'
        ]

    def get_has_mra_token(self, obj):
        return bool(getattr(obj, 'mra_token', ''))

    def get_activation_result(self, obj):
        audit = obj.audit_logs.filter(action='activated').order_by('-created_at').first()
        if not audit:
            return None

        details = audit.details if isinstance(audit.details, dict) else {}
        response = details.get('response') if isinstance(details.get('response'), dict) else {}
        dry_run = bool(details.get('dry_run', False))

        return {
            'state': details.get('state') or ('prepared' if dry_run else obj.status),
            'dry_run': dry_run,
            'dry_run_reason': details.get('dry_run_reason') or response.get('reason') or '',
            'endpoint': details.get('endpoint') or '',
            'status_code': details.get('status_code'),
            'confirmation_state': details.get('confirmation_state') or '',
            'error': details.get('error') or response.get('error') or '',
            'activation_response_shape': details.get('activation_response_shape') or {},
            'created_at': audit.created_at,
        }


class TerminalActivationSerializer(serializers.Serializer):
    """Serializer for terminal activation request"""
    tac_code = serializers.CharField(max_length=50)
    pos_name = serializers.CharField(max_length=255)
    pos_version = serializers.CharField(max_length=50)
    os_type = serializers.CharField(max_length=50)
    device_serial = serializers.CharField(max_length=255)
    mac_address = serializers.CharField(max_length=17, required=False, allow_blank=True)


class MRAConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = MRAConfiguration
        fields = [
            'id', 'config_type', 'config_version', 'config_data',
            'effective_from', 'effective_to', 'is_active',
            'fetched_from_mra_at', 'created_at'
        ]
        read_only_fields = ['id', 'fetched_from_mra_at', 'created_at']


class InvoiceItemSerializer(serializers.Serializer):
    """Serializer for invoice items"""
    mra_product_code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=255)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    tax_rate = serializers.DecimalField(max_digits=5, decimal_places=2)
    tax_category = serializers.ChoiceField(choices=['standard', 'zero', 'exempt'])


class MRAInvoiceCreateSerializer(serializers.Serializer):
    """Serializer for creating invoices"""
    seller_tin = serializers.CharField(max_length=50)
    seller_name = serializers.CharField(max_length=255)
    buyer_tin = serializers.CharField(max_length=50, required=False, allow_blank=True)
    buyer_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    items = InvoiceItemSerializer(many=True)
    is_online = serializers.BooleanField(default=True)


class MRAInvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = MRAInvoice
        fields = [
            'id', 'invoice_number', 'mra_invoice_id',
            'seller_tin', 'seller_name', 'buyer_tin', 'buyer_name',
            'items', 'net_amount', 'tax_amount', 'gross_amount',
            'tax_breakdown', 'invoice_signature',
            'status', 'is_online', 'invoice_date', 'submitted_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'invoice_number', 'mra_invoice_id', 'invoice_signature',
            'status', 'submitted_at', 'created_at', 'updated_at'
        ]


class OfflineInvoiceQueueSerializer(serializers.ModelSerializer):
    invoice = MRAInvoiceSerializer(source='mra_invoice', read_only=True)
    sync_state = serializers.CharField(source='sync_state', read_only=True)

    class Meta:
        model = OfflineInvoiceQueue
        fields = [
            'id', 'queue_position', 'status', 'sync_state', 'sync_attempts',
            'last_sync_attempt_at', 'last_sync_error',
            'created_at', 'synced_at', 'invoice'
        ]
        read_only_fields = [
            'id', 'queue_position', 'sync_attempts',
            'last_sync_attempt_at', 'last_sync_error',
            'created_at', 'synced_at'
        ]


class SyncRetryQueueSerializer(serializers.ModelSerializer):
    sync_state = serializers.CharField(source='sync_state', read_only=True)

    class Meta:
        model = SyncRetryQueue
        fields = [
            'id', 'operation_type', 'status', 'sync_state',
            'attempt_count', 'max_attempts', 'next_attempt_at',
            'last_error', 'created_at', 'completed_at'
        ]
        read_only_fields = [
            'id', 'operation_type', 'status', 'sync_state',
            'attempt_count', 'max_attempts', 'next_attempt_at',
            'last_error', 'created_at', 'completed_at'
        ]


class ReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Receipt
        fields = [
            'id', 'receipt_number', 'receipt_text',
            'qr_code_data', 'qr_code_image', 'generated_at'
        ]
        read_only_fields = ['id', 'generated_at']


class InvoiceAuditLogSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.email', read_only=True)

    class Meta:
        model = InvoiceAuditLog
        fields = [
            'id', 'action', 'user_name', 'details',
            'ip_address', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class TerminalAuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = TerminalAuditLog
        fields = ['id', 'action', 'details', 'created_at']
        read_only_fields = ['id', 'created_at']


class MRAAPIErrorSerializer(serializers.ModelSerializer):
    class Meta:
        model = MRAAPIError
        fields = [
            'id', 'error_type', 'error_message', 'error_code',
            'retry_count', 'next_retry_at', 'is_resolved',
            'created_at', 'resolved_at'
        ]
        read_only_fields = ['id', 'created_at', 'resolved_at']


class TerminalStatusSerializer(serializers.Serializer):
    """Serializer for terminal status response"""
    id = serializers.CharField(required=False)
    business = serializers.CharField(required=False, allow_blank=True)
    branch = serializers.CharField(required=False, allow_blank=True)
    terminal_id = serializers.CharField()
    mra_terminal_id = serializers.CharField(required=False, allow_blank=True)
    device_serial = serializers.CharField(required=False, allow_blank=True)
    mac_address = serializers.CharField(required=False, allow_blank=True)
    pos_name = serializers.CharField(required=False, allow_blank=True)
    pos_version = serializers.CharField(required=False, allow_blank=True)
    os_type = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField()
    is_online = serializers.BooleanField()
    has_mra_token = serializers.BooleanField(required=False)
    online_invoice_counter = serializers.IntegerField()
    offline_invoice_counter = serializers.IntegerField()
    pending_offline_invoices = serializers.IntegerField()
    activated_at = serializers.DateTimeField(required=False, allow_null=True)
    token_expires_at = serializers.DateTimeField(allow_null=True)
    last_sync_at = serializers.DateTimeField(allow_null=True)
    blocking_status = serializers.JSONField(required=False, allow_null=True)
    health_check = serializers.JSONField(required=False, allow_null=True)


class SyncStatusSerializer(serializers.Serializer):
    """Serializer for sync status response"""
    synced_count = serializers.IntegerField()
    failed_count = serializers.IntegerField()
    pending_count = serializers.IntegerField()
    last_sync_at = serializers.DateTimeField(allow_null=True)
