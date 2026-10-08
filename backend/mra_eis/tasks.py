"""
Celery tasks for MRA EIS background processing.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.db.models import Q

from .models import Terminal
from .services import InvoiceService, RetryService, TerminalService

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
    expired_total = 0
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
            expired_total += int(result.get('expired', 0))
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
        f'synced={synced_total} failed={failed_total} expired={expired_total}'
    )

    return {
        'terminals': terminal_count,
        'synced': synced_total,
        'failed': failed_total,
        'expired': expired_total,
    }


@shared_task
def check_suspended_terminal_unblock_status():
    """Ask MRA whether locally suspended terminals have been unblocked.

    This is deliberately one sequential Celery task. It checks only terminals
    already marked suspended locally, so normal active terminals do not create
    MRA traffic and each terminal does not get its own worker process.
    """
    live_submission = (
        bool(getattr(settings, 'MRA_EIS_ENABLE_HTTP_CALLS', False))
        and not bool(getattr(settings, 'MRA_EIS_DRY_RUN', True))
        and bool(getattr(settings, 'MRA_EIS_ALLOW_LIVE_SUBMISSION', False))
    )
    if not live_submission:
        _print_replay('[MRA UNBLOCK TASK] skipped because live MRA submission is disabled')
        return {
            'terminals': 0,
            'unblocked': 0,
            'still_blocked': 0,
            'failed': 0,
            'skipped': True,
        }

    terminals = (
        Terminal.objects
        .filter(status='suspended')
        .exclude(mra_token='')
        .order_by('updated_at')
    )
    terminal_count = terminals.count()
    unblocked_total = 0
    still_blocked_total = 0
    failed_total = 0

    _print_replay(
        f'[MRA UNBLOCK TASK] starting suspended_terminals={terminal_count}'
    )

    for terminal in terminals.iterator(chunk_size=50):
        try:
            result = TerminalService.check_terminal_unblock_status(terminal)
            if result.get('is_unblocked') is True:
                unblocked_total += 1
                _print_replay(
                    f'[MRA UNBLOCK TASK] terminal_id={terminal.terminal_id} unblocked'
                )
            else:
                still_blocked_total += 1
        except Exception as exc:
            failed_total += 1
            logger.exception(
                '[MRA UNBLOCK TASK] terminal failed terminal_pk=%s terminal_id=%s error=%s',
                terminal.pk,
                terminal.terminal_id,
                exc,
            )

    _print_replay(
        f'[MRA UNBLOCK TASK] complete terminals={terminal_count} '
        f'unblocked={unblocked_total} still_blocked={still_blocked_total} failed={failed_total}'
    )
    return {
        'terminals': terminal_count,
        'unblocked': unblocked_total,
        'still_blocked': still_blocked_total,
        'failed': failed_total,
        'skipped': False,
    }
