"""
MRA EIS Signals - Background tasks and event handlers
"""
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.utils import timezone
from django.conf import settings
from .models import Terminal, MRAInvoice, OfflineInvoiceQueue, TerminalAuditLog
from .services import RetryService


@receiver(post_save, sender=Terminal)
def on_terminal_created(sender, instance, created, **kwargs):
    """Handle terminal creation"""
    if created:
        # Initialize counters deterministically without triggering nested saves.
        Terminal.objects.filter(pk=instance.pk).update(
            online_invoice_counter=0,
            offline_invoice_counter=0,
        )

        # Record an activation audit when a terminal is created already active.
        if instance.status == 'active':
            TerminalAuditLog.objects.create(
                terminal=instance,
                action='activated',
                details={'source': 'signal_on_create'},
            )


@receiver(post_save, sender=MRAInvoice)
def on_invoice_status_changed(sender, instance, created, **kwargs):
    """Handle invoice status changes"""
    if not created and instance.status == 'offline_queued':
        if bool(getattr(settings, 'MRA_EIS_ALWAYS_OFFLINE_B2C', False)):
            return
        # Auto-sync if terminal is online
        if instance.terminal.is_online:
            try:
                from .services import InvoiceService
                InvoiceService.sync_offline_invoices(instance.terminal)
            except Exception:
                pass  # Silently fail - will retry later


@receiver(post_save, sender=OfflineInvoiceQueue)
def on_offline_queue_entry_created(sender, instance, created, **kwargs):
    """Handle offline queue entry creation"""
    if created:
        # Log queue entry
        from .models import OfflineAuditLog
        OfflineAuditLog.objects.create(
            terminal=instance.terminal,
            event_type='invoice_queued',
            details={
                'invoice_number': instance.mra_invoice.invoice_number,
                'queue_position': instance.queue_position
            }
        )
