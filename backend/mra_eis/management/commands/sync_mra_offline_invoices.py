"""
Replay queued MRA EIS offline invoices.

Useful as a cron fallback when Celery beat is not deployed.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q

from mra_eis.models import OfflineInvoiceQueue, Terminal
from mra_eis.services import InvoiceService


class Command(BaseCommand):
    help = 'Replay queued MRA EIS offline invoices for active online terminals'

    def add_arguments(self, parser):
        parser.add_argument(
            '--terminal-id',
            dest='terminal_id',
            help='Replay only one terminal primary key.',
        )
        parser.add_argument(
            '--all-active',
            action='store_true',
            help='Include active terminals even if their cached online flag is false.',
        )

    def handle(self, *args, **options):
        terminal_id = options.get('terminal_id')
        include_all_active = bool(options.get('all_active')) or bool(
            getattr(settings, 'MRA_EIS_SYNC_ALL_ACTIVE_TERMINALS', True)
        )

        terminals = Terminal.objects.filter(status='active')
        if terminal_id:
            terminals = terminals.filter(id=terminal_id)
        elif not include_all_active:
            terminals = terminals.filter(is_online=True)

        terminals = terminals.filter(
            Q(offline_queue__status='queued') | Q(offline_queue__status='failed')
        ).distinct()

        synced_total = 0
        failed_total = 0
        terminal_count = terminals.count()
        self.stdout.write(
            f'MRA offline replay starting: include_all_active={include_all_active} '
            f'terminals_with_queue={terminal_count}'
        )

        for terminal in terminals:
            pending_count = OfflineInvoiceQueue.objects.filter(
                terminal=terminal,
                status__in=['queued', 'failed'],
            ).count()
            self.stdout.write(
                f'Syncing terminal {terminal.id} ({terminal.terminal_id}) with {pending_count} queued invoice(s)...'
            )
            try:
                result = InvoiceService.sync_offline_invoices(terminal)
            except Exception as exc:
                failed_total += 1
                self.stderr.write(self.style.ERROR(f'  failed: {exc}'))
                continue

            synced = int(result.get('synced', 0))
            failed = int(result.get('failed', 0))
            synced_total += synced
            failed_total += failed
            self.stdout.write(f'  synced={synced} failed={failed}')

        self.stdout.write(
            self.style.SUCCESS(
                f'MRA offline invoice replay complete: terminals={terminal_count} '
                f'synced={synced_total} failed={failed_total}'
            )
        )
