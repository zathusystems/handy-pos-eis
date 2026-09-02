from __future__ import annotations

import json
from decimal import Decimal

from ..models import InvoiceAuditLog, Receipt
from .core import ConfigurationService


class ReceiptService:
    """Receipt rendering and QR payload generation."""

    @staticmethod
    def _extract_validation_url(invoice) -> str:
        response = invoice.mra_response or {}
        response_data = response.get('response') if isinstance(response.get('response'), dict) else {}
        response_inner = response_data.get('data') if isinstance(response_data.get('data'), dict) else {}
        local_metadata = response.get('local_metadata') if isinstance(response.get('local_metadata'), dict) else {}
        return str(
            response_inner.get('validationURL')
            or response_inner.get('validationUrl')
            or response_data.get('validationURL')
            or response_data.get('validationUrl')
            or local_metadata.get('offlineValidationURL')
            or ''
        ).strip()

    @staticmethod
    def _official_invoice_number(invoice) -> str:
        response = invoice.mra_response or {}
        payload = response.get('payload') if isinstance(response.get('payload'), dict) else {}
        header = payload.get('invoiceHeader') if isinstance(payload.get('invoiceHeader'), dict) else {}
        invoice_number = str(header.get('invoiceNumber') or '').strip()
        if invoice_number:
            return invoice_number

        terminal_code = invoice.terminal.mra_terminal_id or invoice.terminal.terminal_id
        mode_code = '01' if invoice.is_online else '02'
        return f"{terminal_code}-{mode_code}-{int(invoice.invoice_number):08d}"

    @staticmethod
    def _local_receipt_reference(invoice) -> str:
        response = invoice.mra_response or {}
        payload = response.get('payload') if isinstance(response.get('payload'), dict) else {}
        metadata = payload.get('handyPosMetadata') if isinstance(payload.get('handyPosMetadata'), dict) else {}
        reference = str(metadata.get('localReceiptReference') or '').strip()
        if reference:
            return reference

        order_number = metadata.get('orderNumber')
        if order_number in (None, ''):
            order_number = invoice.invoice_number
        return f"{invoice.invoice_date.strftime('%Y%m%d-%H%M%S')}-{order_number}"

    @staticmethod
    def _format_money(value, *, include_currency: bool = False) -> str:
        try:
            amount = Decimal(str(value or 0)).quantize(Decimal('0.01'))
        except Exception:
            amount = Decimal('0.00')
        formatted = f"{amount:,.2f}"
        return f"MWK {formatted}" if include_currency else formatted

    @staticmethod
    def _format_pair(label: str, value: str, width: int = 40) -> str:
        left = str(label or '').strip()
        right = str(value or '').strip()
        if not left:
            return right
        if not right:
            return left
        gap = width - len(left) - len(right)
        return f"{left}{' ' * max(1, gap)}{right}"

    @staticmethod
    def _format_quantity(value) -> str:
        try:
            quantity = Decimal(str(value or 0))
        except Exception:
            return '0'
        if quantity == quantity.to_integral_value():
            return str(int(quantity))
        return f"{quantity.normalize():f}".rstrip('0').rstrip('.')

    @staticmethod
    def _center_line(value: str, width: int = 40) -> str:
        text = str(value or '').strip()
        if len(text) >= width:
            return text
        return text.center(width).rstrip()

    @staticmethod
    def _short_line(value: str, max_length: int = 30) -> str:
        text = ' '.join(str(value or '').strip().split()) or 'ITEM'
        if len(text) <= max_length:
            return text.upper()
        return f"{text[:max_length - 3].rstrip()}...".upper()

    @staticmethod
    def _tax_code(rate_id: str | None, rate: Decimal | int | float | str | None = None) -> str:
        normalized = str(rate_id or '').strip().upper()
        if normalized in {'A', 'B', 'E'}:
            return normalized
        try:
            parsed_rate = Decimal(str(rate or 0))
        except Exception:
            parsed_rate = Decimal('0')
        return 'A' if parsed_rate > 0 else 'B'

    @staticmethod
    def _tax_rate_label(rate_id: str | None, taxable_amount, tax_amount) -> str:
        code = ReceiptService._tax_code(rate_id)
        try:
            taxable = Decimal(str(taxable_amount or 0))
            vat = Decimal(str(tax_amount or 0))
            rate = Decimal('0') if taxable == 0 else ((vat / taxable) * Decimal('100')).quantize(Decimal('0.01'))
        except Exception:
            rate = Decimal('0')
        rate_text = f"{rate:f}".rstrip('0').rstrip('.') or '0'
        return f"{code}-{rate_text}%"

    @staticmethod
    def _is_seller_vat_registered(invoice) -> bool:
        business = getattr(invoice, 'business', None)
        if business is None:
            return False
        return ConfigurationService.is_taxpayer_vat_registered(business)

    @staticmethod
    def generate_receipt(invoice, *, force_refresh: bool = False):
        try:
            existing_receipt = invoice.receipt
            if not force_refresh:
                return existing_receipt
        except Receipt.DoesNotExist:
            existing_receipt = None

        response = invoice.mra_response or {}
        payload = response.get('payload') if isinstance(response.get('payload'), dict) else {}
        header = payload.get('invoiceHeader') if isinstance(payload.get('invoiceHeader'), dict) else {}
        summary = payload.get('invoiceSummary') if isinstance(payload.get('invoiceSummary'), dict) else {}
        line_items = payload.get('invoiceLineItems') if isinstance(payload.get('invoiceLineItems'), list) else invoice.items

        fiscal_invoice_number = ReceiptService._official_invoice_number(invoice)
        receipt_number = fiscal_invoice_number
        validation_url = ReceiptService._extract_validation_url(invoice)
        invoice_signature = invoice.invoice_signature or str(summary.get('offlineSignature') or '')
        seller_name = str(invoice.seller_name or '').strip().upper() or 'SELLER'
        seller_tin = str(invoice.seller_tin or header.get('sellerTIN') or '').strip() or 'N/A'
        buyer_name = str(invoice.buyer_name or header.get('buyerName') or '').strip() or 'Walk-in Customer'
        buyer_tin = str(invoice.buyer_tin or header.get('buyerTIN') or '').strip() or 'N/A'
        payment_method = str(header.get('paymentMethod') or '').strip()

        is_vat_registered = ReceiptService._is_seller_vat_registered(invoice)
        tax_breakdown_rows = []
        for tax_row in summary.get('taxBreakDown') or []:
            if not isinstance(tax_row, dict):
                continue
            rate_id = str(tax_row.get('rateId') or '').strip()
            taxable_amount = tax_row.get('taxableAmount') or 0
            tax_amount = tax_row.get('taxAmount') or 0
            tax_breakdown_rows.append({
                'label': ReceiptService._tax_rate_label(rate_id, taxable_amount, tax_amount),
                'taxable': taxable_amount,
                'vat': tax_amount,
            })

        if not tax_breakdown_rows:
            tax_breakdown = invoice.tax_breakdown or {}
            by_rate = tax_breakdown.get('byRate') if isinstance(tax_breakdown.get('byRate'), list) else []
            for tax_row in by_rate:
                if not isinstance(tax_row, dict):
                    continue
                taxable_amount = tax_row.get('taxableAmount') or tax_row.get('taxable_amount') or invoice.net_amount
                tax_amount = tax_row.get('taxAmount') or tax_row.get('tax_amount') or invoice.tax_amount
                tax_breakdown_rows.append({
                    'label': ReceiptService._tax_rate_label(tax_row.get('rateId') or tax_row.get('rate_id'), taxable_amount, tax_amount),
                    'taxable': taxable_amount,
                    'vat': tax_amount,
                })
        if not tax_breakdown_rows:
            tax_breakdown_rows.append({
                'label': ReceiptService._tax_rate_label('A' if is_vat_registered else 'B', invoice.net_amount, invoice.tax_amount),
                'taxable': invoice.net_amount,
                'vat': invoice.tax_amount,
            })

        receipt_lines = [
            ReceiptService._center_line('MRA'),
            ReceiptService._center_line('/|\\'),
            ReceiptService._center_line('*** START OF LEGAL RECEIPT ***'),
            ReceiptService._center_line(seller_name),
        ]

        branch = getattr(invoice, 'branch', None)
        address = str(getattr(branch, 'address', '') or getattr(getattr(invoice, 'business', None), 'address', '') or '').strip()
        for address_line in [line.strip() for line in address.replace('\r', '').split('\n') if line.strip()][:3]:
            receipt_lines.append(ReceiptService._center_line(address_line.upper()))

        business = getattr(invoice, 'business', None)
        phone = str(getattr(business, 'phone', '') or '').strip()
        email = str(getattr(business, 'email', '') or '').strip()
        if phone:
            receipt_lines.append(ReceiptService._center_line(f'CELL: {phone}'))
        if email:
            receipt_lines.append(ReceiptService._center_line(f'EMAIL: {email}'))
        receipt_lines.extend([
            ReceiptService._center_line(f'TIN: {seller_tin}'),
            ReceiptService._center_line('*VAT REGISTERED*' if is_vat_registered else '*NON VAT REGISTERED*'),
            '',
            f'Buyers Name: {buyer_name}',
            f'Buyers Tin: {buyer_tin}',
            f'Receipt Number: {receipt_number}',
            f'POS Ref: {ReceiptService._local_receipt_reference(invoice)}',
            '-' * 40,
        ])

        for item in line_items:
            if not isinstance(item, dict):
                continue
            quantity = item.get('quantity') or item.get('qty') or 0
            unit_price = item.get('unitPrice') or item.get('unit_price') or 0
            line_vat = Decimal(str(item.get('totalVAT') or item.get('total_vat') or 0))
            line_total = Decimal(str(item.get('total') or 0))
            if line_total == 0:
                line_total = Decimal(str(quantity or 0)) * Decimal(str(unit_price or 0))
            tax_code = ReceiptService._tax_code(item.get('taxRateId') or item.get('tax_rate_id'), line_vat)
            receipt_lines.append(
                ReceiptService._format_pair(
                    f"{ReceiptService._format_quantity(quantity)} X {ReceiptService._format_money(unit_price)}",
                    f"{ReceiptService._format_money(line_total)} {tax_code}",
                )
            )
            receipt_lines.append(ReceiptService._short_line(item.get('description') or item.get('name') or item.get('productCode')))
            discount_amount = Decimal(str(item.get('discount') or item.get('discountAmount') or item.get('discount_amount') or 0))
            if discount_amount > 0:
                discount_name = str(item.get('discountName') or item.get('discount_name') or 'DISCOUNT').upper()
                receipt_lines.append(
                    ReceiptService._format_pair(
                        ReceiptService._short_line(discount_name),
                        f"-{ReceiptService._format_money(discount_amount)}",
                    )
                )

        receipt_lines.append('-' * 40)
        total_vat = Decimal('0')
        for tax_row in tax_breakdown_rows:
            total_vat += Decimal(str(tax_row.get('vat') or 0))
            receipt_lines.append(ReceiptService._format_pair(f"TAXABLE {tax_row['label']}", ReceiptService._format_money(tax_row.get('taxable'))))
            receipt_lines.append(ReceiptService._format_pair(f"VAT {tax_row['label']}", ReceiptService._format_money(tax_row.get('vat'))))
        receipt_lines.append(ReceiptService._format_pair('TOTAL VAT:', ReceiptService._format_money(total_vat)))
        for levy_row in summary.get('levyBreakDown') or []:
            if not isinstance(levy_row, dict):
                continue
            levy_type = str(levy_row.get('levyTypeId') or '').strip() or 'LEVY'
            levy_rate = ReceiptService._format_money(levy_row.get('levyRate'), include_currency=False).rstrip('0').rstrip('.')
            receipt_lines.append(
                ReceiptService._format_pair(
                    f"LEVY {levy_type}-{levy_rate}%",
                    ReceiptService._format_money(levy_row.get('levyAmount')),
                )
            )
        receipt_total = summary.get('invoiceTotal') or invoice.gross_amount
        receipt_lines.extend([
            '-' * 40,
            ReceiptService._format_pair('TOTAL:', ReceiptService._format_money(receipt_total)),
            ReceiptService._format_pair('Amount Tendered:', ReceiptService._format_money(summary.get('amountTendered') or receipt_total)),
            ReceiptService._format_pair('Change:', ReceiptService._format_money('0')),
        ])
        if payment_method:
            receipt_lines.append(f'Payment: {payment_method}')
        receipt_lines.extend([
            '',
            f"DATE: {invoice.invoice_date.strftime('%Y-%m-%d')} TIME: {invoice.invoice_date.strftime('%H:%M:%S')}",
            'Scan Here For Receipt Details',
        ])
        if validation_url:
            receipt_lines.append(validation_url)
        receipt_lines.extend([
            '',
            ReceiptService._center_line('*** END OF LEGAL RECEIPT ***'),
            ReceiptService._center_line('THANK YOU!'),
        ])

        receipt_text = '\n'.join(receipt_lines)
        qr_data = {
            'invoice_id': str(invoice.id),
            'invoice_number': fiscal_invoice_number,
            'fiscal_invoice_number': fiscal_invoice_number,
            'seller_tin': invoice.seller_tin,
            'gross_amount': str(invoice.gross_amount),
            'signature': invoice_signature,
            'eis_status': invoice.status,
            'eis_uuid': invoice.mra_invoice_id,
            'is_online': invoice.is_online,
            'validation_url': validation_url,
            'date': invoice.invoice_date.isoformat(),
        }

        receipt_payload = {
            'receipt_number': receipt_number,
            'receipt_text': receipt_text,
            'qr_code_data': validation_url or json.dumps(qr_data),
        }
        if existing_receipt:
            Receipt.objects.filter(pk=existing_receipt.pk).update(**receipt_payload)
            existing_receipt.refresh_from_db()
            receipt = existing_receipt
        else:
            receipt = Receipt.objects.create(
                mra_invoice=invoice,
                **receipt_payload,
            )

        InvoiceAuditLog.objects.create(
            mra_invoice=invoice,
            action='receipt_generated',
            details={'receipt_number': receipt.receipt_number},
        )

        return receipt


__all__ = ['ReceiptService']
