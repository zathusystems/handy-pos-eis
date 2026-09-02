"""
Celery tasks for MRA EIS background processing.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q

from .models import Terminal
from .services import InvoiceService, RetryService

logger = logging.getLogger(__name__)


def _print_replay(message: str) -> None:
    print(message, flush=True)
    logger.warning(message)


@shared_task
def process_mra_retry_queue():
    """Process queued retry jobs for MRA EIS (POS orders, invoices, offline sync)."""
    _print_replay('[MRA RETRY] processing retry queue')
    RetryService.process_retry_queue()
    _print_replay('[MRA RETRY] retry queue processing complete')
    return {'status': 'ok'}


@shared_task
def sync_offline_invoices_for_online_terminals():
    """
    Sync offline invoices for active terminals with queued/failed invoices.
    Intended to be scheduled periodically (Celery beat or cron).
    """
    include_all_active = bool(getattr(settings, 'MRA_EIS_SYNC_ALL_ACTIVE_TERMINALS', True))
    terminals = Terminal.objects.filter(status='active')
    if not include_all_active:
        terminals = terminals.filter(is_online=True)

    terminals = terminals.filter(
        Q(offline_queue__status='queued') | Q(offline_queue__status='failed')
    ).distinct()

    synced_total = 0
    failed_total = 0
    terminal_count = terminals.count()

    _print_replay(
        f'[MRA REPLAY TASK] starting include_all_active={include_all_active} '
        f'terminals_with_queue={terminal_count}'
    )

    for terminal in terminals:
        try:
            pending_count = terminal.offline_queue.filter(status__in=['queued', 'failed']).count()
            _print_replay(
                f'[MRA REPLAY TASK] terminal_pk={terminal.pk} terminal_id={terminal.terminal_id} '
                f'is_online={terminal.is_online} pending={pending_count}'
            )
            result = InvoiceService.sync_offline_invoices(terminal)
            synced_total += int(result.get('synced', 0))
            failed_total += int(result.get('failed', 0))
        except Exception as exc:
            failed_total += 1
            logger.exception(
                '[MRA REPLAY TASK] terminal failed terminal_pk=%s terminal_id=%s error=%s',
                terminal.pk,
                terminal.terminal_id,
                exc,
            )

    _print_replay(
        f'[MRA REPLAY TASK] complete terminals={terminal_count} '
        f'synced={synced_total} failed={failed_total}'
    )

    return {
        'terminals': terminal_count,
        'synced': synced_total,
        'failed': failed_total,
    }
