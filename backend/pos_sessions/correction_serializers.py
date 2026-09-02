import json
from decimal import Decimal

from rest_framework import serializers
from .models import CreditNote, DebitNote, VoidTransaction, Order


def _resolve_eis_sync_state(eis_status: str | None) -> str:
    status = str(eis_status or '').upper()
    return {
        'PENDING': 'PENDING',
        'SUBMITTED': 'SENDING',
        'ACCEPTED': 'SUCCESS',
        'REJECTED': 'FAILED',
    }.get(status, 'PENDING')


class CreditNoteSerializer(serializers.ModelSerializer):
    """Serializer for Credit Notes"""
    original_order_number = serializers.CharField(source='original_order.order_number', read_only=True)
    original_fiscal_invoice_number = serializers.CharField(source='original_order.fiscal_invoice_number', read_only=True)
    original_eis_uuid = serializers.CharField(source='original_order.eis_uuid', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    eis_sync_state = serializers.SerializerMethodField()
    
    class Meta:
        model = CreditNote
        fields = [
            'id',
            'credit_note_number',
            'original_order',
            'original_order_number',
            'original_fiscal_invoice_number',
            'original_eis_uuid',
            'reason',
            'description',
            'credit_amount',
            'vat_amount',
            'total_credit',
            'fiscal_credit_number',
            'eis_uuid',
            'eis_status',
            'eis_sync_state',
            'eis_submitted_at',
            'qr_code_payload',
            'digital_signature',
            'is_fiscal_locked',
            'created_by',
            'created_by_name',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'credit_note_number',
            'fiscal_credit_number',
            'eis_uuid',
            'eis_status',
            'eis_submitted_at',
            'qr_code_payload',
            'digital_signature',
            'is_fiscal_locked',
            'created_by',
            'created_at',
            'updated_at',
        ]

    def get_eis_sync_state(self, obj):
        return _resolve_eis_sync_state(getattr(obj, 'eis_status', None))


class DebitNoteSerializer(serializers.ModelSerializer):
    """Serializer for Debit Notes"""
    original_order_number = serializers.CharField(source='original_order.order_number', read_only=True)
    original_fiscal_invoice_number = serializers.CharField(source='original_order.fiscal_invoice_number', read_only=True)
    original_eis_uuid = serializers.CharField(source='original_order.eis_uuid', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    eis_sync_state = serializers.SerializerMethodField()
    
    class Meta:
        model = DebitNote
        fields = [
            'id',
            'debit_note_number',
            'original_order',
            'original_order_number',
            'original_fiscal_invoice_number',
            'original_eis_uuid',
            'description',
            'additional_amount',
            'vat_amount',
            'total_debit',
            'fiscal_debit_number',
            'eis_uuid',
            'eis_status',
            'eis_sync_state',
            'eis_submitted_at',
            'qr_code_payload',
            'digital_signature',
            'is_fiscal_locked',
            'created_by',
            'created_by_name',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'debit_note_number',
            'fiscal_debit_number',
            'eis_uuid',
            'eis_status',
            'eis_submitted_at',
            'qr_code_payload',
            'digital_signature',
            'is_fiscal_locked',
            'created_by',
            'created_at',
            'updated_at',
        ]

    def get_eis_sync_state(self, obj):
        return _resolve_eis_sync_state(getattr(obj, 'eis_status', None))


class VoidTransactionSerializer(serializers.ModelSerializer):
    """Serializer for Void Transactions"""
    original_order_number = serializers.CharField(source='original_order.order_number', read_only=True)
    original_fiscal_invoice_number = serializers.CharField(source='original_order.fiscal_invoice_number', read_only=True)
    original_eis_uuid = serializers.CharField(source='original_order.eis_uuid', read_only=True)
    original_order_total = serializers.DecimalField(source='original_order.total', read_only=True, max_digits=12, decimal_places=2)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    eis_sync_state = serializers.SerializerMethodField()
    
    class Meta:
        model = VoidTransaction
        fields = [
            'id',
            'void_number',
            'original_order',
            'original_order_number',
            'original_fiscal_invoice_number',
            'original_eis_uuid',
            'original_order_total',
            'void_reason',
            'reason_description',
            'supporting_documents',
            'voided_amount',
            'voided_vat',
            'refund_method',
            'refund_amount',
            'refund_processed',
            'refund_processed_at',
            'fiscal_void_number',
            'eis_uuid',
            'eis_status',
            'eis_sync_state',
            'eis_submitted_at',
            'qr_code_payload',
            'digital_signature',
            'is_fiscal_locked',
            'created_by',
            'created_by_name',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'void_number',
            'fiscal_void_number',
            'eis_uuid',
            'eis_status',
            'eis_submitted_at',
            'refund_processed',
            'refund_processed_at',
            'qr_code_payload',
            'digital_signature',
            'is_fiscal_locked',
            'created_by',
            'created_at',
            'updated_at',
        ]

    def get_eis_sync_state(self, obj):
        return _resolve_eis_sync_state(getattr(obj, 'eis_status', None))


class CreateCreditNoteSerializer(serializers.Serializer):
    """Serializer for creating a Credit Note"""
    original_order_id = serializers.UUIDField()
    reason = serializers.ChoiceField(choices=CreditNote.REASON_CHOICES)
    description = serializers.CharField(max_length=1000)
    credit_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    vat_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    
    def validate_original_order_id(self, value):
        try:
            order = Order.objects.get(id=value)
            return value
        except Order.DoesNotExist:
            raise serializers.ValidationError("Order not found.")
    
    def validate_credit_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Credit amount must be greater than 0.")
        return value

    def validate_vat_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("VAT amount cannot be negative.")
        return value

    def validate(self, attrs):
        order = Order.objects.get(id=attrs['original_order_id'])
        original_net = Decimal(str(order.net_amount or order.subtotal or 0))
        original_vat = Decimal(str(order.vat_amount or 0))

        if attrs['credit_amount'] > original_net:
            raise serializers.ValidationError(
                {'credit_amount': 'Credit amount cannot exceed the original taxable/net amount.'}
            )
        if attrs['vat_amount'] > original_vat:
            raise serializers.ValidationError(
                {'vat_amount': 'VAT credit cannot exceed the original VAT amount.'}
            )
        if attrs['credit_amount'] + attrs['vat_amount'] > Decimal(str(order.gross_amount or order.total or 0)):
            raise serializers.ValidationError('Total credit cannot exceed the original sale total.')
        return attrs


class CreateDebitNoteSerializer(serializers.Serializer):
    """Serializer for creating a Debit Note"""
    original_order_id = serializers.UUIDField()
    description = serializers.CharField(max_length=1000)
    additional_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    vat_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    
    def validate_original_order_id(self, value):
        try:
            order = Order.objects.get(id=value)
            return value
        except Order.DoesNotExist:
            raise serializers.ValidationError("Order not found.")
    
    def validate_additional_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Additional amount must be greater than 0.")
        return value

    def validate_vat_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("VAT amount cannot be negative.")
        return value


class CreateVoidTransactionSerializer(serializers.Serializer):
    """Serializer for creating a Void Transaction"""
    original_order_id = serializers.UUIDField()
    void_reason = serializers.ChoiceField(choices=VoidTransaction.VOID_REASON_CHOICES)
    reason_description = serializers.CharField(max_length=1000)
    supporting_documents = serializers.JSONField(required=False, default='')
    supportingDocuments = serializers.JSONField(required=False, write_only=True)
    refund_method = serializers.ChoiceField(
        choices=[
            ('cash', 'Cash Refund'),
            ('card', 'Card Refund'),
            ('credit', 'Store Credit'),
            ('none', 'No Refund'),
        ],
        required=False,
        default='none',
    )
    refund_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        default=Decimal('0.00'),
    )
    
    def validate_original_order_id(self, value):
        try:
            order = Order.objects.get(id=value)
            return value
        except Order.DoesNotExist:
            raise serializers.ValidationError("Order not found.")

    def validate_refund_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Refund amount cannot be negative.")
        return value

    @staticmethod
    def _normalize_supporting_documents(value):
        if value in (None, '', []):
            return ''
        if isinstance(value, str):
            text = value.replace('\r\n', '\n').replace('\r', '\n').strip()
            if len(text) > 10000:
                raise serializers.ValidationError('Supporting document text must be 10000 characters or fewer.')
            return text
        elif isinstance(value, dict):
            nested = value.get('supportingDocuments') or value.get('supporting_documents')
            if nested not in (None, '', []):
                return CreateVoidTransactionSerializer._normalize_supporting_documents(nested)
            cleaned = {
                str(key): item_value
                for key, item_value in value.items()
                if item_value not in (None, '', [])
            }
            if not cleaned:
                return ''
            text = json.dumps(cleaned, sort_keys=True, separators=(',', ':'), default=str)
            if len(text) > 10000:
                raise serializers.ValidationError('Supporting document data must be 10000 characters or fewer.')
            return text
        elif isinstance(value, list):
            lines = []
            structured_items = []
            has_structured = False
            for index, item in enumerate(value):
                if item in (None, ''):
                    continue
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        lines.append(text)
                    continue
                if isinstance(item, dict):
                    cleaned = {
                        str(key): item_value
                        for key, item_value in item.items()
                        if item_value not in (None, '', [])
                    }
                    if cleaned:
                        structured_items.append(cleaned)
                        has_structured = True
                    continue
                raise serializers.ValidationError(
                    f'Supporting document #{index + 1} must be text or structured JSON.'
                )
            if has_structured:
                if lines:
                    structured_items.insert(0, {'references': lines})
                text = json.dumps(structured_items, sort_keys=True, separators=(',', ':'), default=str)
            else:
                text = '\n'.join(lines)
            if len(text) > 10000:
                raise serializers.ValidationError('Supporting document data must be 10000 characters or fewer.')
            return text
        else:
            raise serializers.ValidationError('Supporting document must be text or structured JSON.')

    def validate(self, attrs):
        order = Order.objects.get(id=attrs['original_order_id'])
        supporting_documents = attrs.pop(
            'supportingDocuments',
            attrs.get('supporting_documents', []),
        )
        attrs['supporting_documents'] = self._normalize_supporting_documents(supporting_documents)
        refund_method = attrs.get('refund_method') or 'none'
        refund_amount = attrs.get('refund_amount') or Decimal('0.00')
        original_total = Decimal(str(order.gross_amount or order.total or 0))

        if refund_method == 'none' and refund_amount > 0:
            raise serializers.ValidationError(
                {'refund_amount': 'Refund amount must be zero when refund method is No Refund.'}
            )
        if refund_method != 'none' and refund_amount <= 0:
            raise serializers.ValidationError(
                {'refund_amount': 'Refund amount is required when a refund method is selected.'}
            )
        if refund_amount > original_total:
            raise serializers.ValidationError(
                {'refund_amount': 'Refund amount cannot exceed the original sale total.'}
            )
        return attrs
