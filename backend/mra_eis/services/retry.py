from __future__ import annotations

from django.utils import timezone

from ..models import MRAInvoice, SyncRetryQueue, Terminal


class RetryService:
    """Retry queue processing."""

    @staticmethod
    def queue_retry(terminal, operation_type, payload, max_attempts=5):
        return SyncRetryQueue.objects.create(
            terminal=terminal,
            operation_type=operation_type,
            status='pending',
            payload=payload,
            max_attempts=max_attempts,
            next_attempt_at=timezone.now(),
        )

    @staticmethod
    def process_retry_queue():
        from .core import CorrectionService, InvoiceService, POSOrderSubmissionService, StockReceivingService

        pending_retries = (
            SyncRetryQueue.objects.filter(status='pending')
            .filter(next_attempt_at__lte=timezone.now())
            .order_by('next_attempt_at')
        )

        for retry in pending_retries:
            try:
                retry.status = 'processing'
                retry.save(update_fields=['status'])

                if retry.operation_type == 'submit_invoice':
                    invoice = MRAInvoice.objects.get(id=retry.payload['invoice_id'])
                    InvoiceService.submit_invoice(invoice)
                elif retry.operation_type == 'sync_offline_invoices':
                    terminal = Terminal.objects.get(id=retry.payload['terminal_id'])
                    InvoiceService.sync_offline_invoices(terminal)
                elif retry.operation_type == 'submit_pos_order':
                    from pos_sessions.models import Order

                    order = Order.objects.get(id=retry.payload['order_id'])
                    POSOrderSubmissionService.prepare_pos_order_submission(order)
                elif retry.operation_type == 'submit_credit_note':
                    from pos_sessions.models import CreditNote

                    credit_note = CreditNote.objects.get(id=retry.payload['credit_note_id'])
                    CorrectionService.submit_credit_note(
                        credit_note,
                        queue_on_dry_run=False,
                        queue_on_failure=False,
                    )
                elif retry.operation_type == 'submit_debit_note':
                    from pos_sessions.models import DebitNote

                    debit_note = DebitNote.objects.get(id=retry.payload['debit_note_id'])
                    CorrectionService.submit_debit_note(
                        debit_note,
                        queue_on_dry_run=False,
                        queue_on_failure=False,
                    )
                elif retry.operation_type == 'submit_void_transaction':
                    from pos_sessions.models import VoidTransaction

                    void_transaction = VoidTransaction.objects.get(id=retry.payload['void_transaction_id'])
                    CorrectionService.submit_void_transaction(
                        void_transaction,
                        queue_on_dry_run=False,
                        queue_on_failure=False,
                    )
                elif retry.operation_type == 'submit_stock_payload':
                    StockReceivingService.retry_stock_payload(retry.payload)
                elif retry.operation_type == 'submit_purchase_item_receipt':
                    from inventory.models import PurchaseOrderItem

                    purchase_item = PurchaseOrderItem.objects.get(id=retry.payload['purchase_item_id'])
                    StockReceivingService.submit_purchase_item_receipt(
                        purchase_item,
                        retry.payload.get('quantity'),
                        queue_on_failure=False,
                        raise_on_error=True,
                    )

                retry.status = 'completed'
                retry.completed_at = timezone.now()
                retry.save(update_fields=['status', 'completed_at'])
            except Exception as exc:
                retry.attempt_count += 1
                retry.last_error = str(exc)

                if retry.attempt_count >= retry.max_attempts:
                    retry.status = 'failed'
                else:
                    retry.status = 'pending'
                    retry.next_attempt_at = retry.calculate_next_retry()

                retry.save(update_fields=['attempt_count', 'last_error', 'status', 'next_attempt_at'])


__all__ = ['RetryService']
