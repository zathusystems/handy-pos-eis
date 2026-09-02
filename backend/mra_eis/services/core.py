"""
MRA EIS service implementations.

This module keeps the app fully integrated with the official MRA EIS contract
while supporting a safe dry-run mode for rollout.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import base64
import re
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Sum
from django.utils import timezone

from ..models import (
    ConfigurationSyncLog,
    FiscalInvoiceSequence,
    InvoiceAuditLog,
    MRAAPIError,
    MRAConfiguration,
    MRAInvoice,
    OfflineAuditLog,
    OfflineInvoiceQueue,
    SyncRetryQueue,
    Terminal,
    TerminalActivationCode,
    TerminalAuditLog,
)
from .client import MRAEISClient
from .common import (
    MRACallResult,
    MRAIntegrationError,
    OfflineLimitPolicy,
    _extract_mra_response_errors,
)
from .retry import RetryService

logger = logging.getLogger(__name__)


def _is_mra_network_failure(
    exc: Exception,
    *,
    status_code: int | None = None,
    response_data: dict[str, Any] | None = None,
) -> bool:
    """True when MRA is unreachable or its gateway/server is unavailable."""
    if status_code and int(status_code) in {500, 501, 502, 503, 504}:
        return True

    if status_code:
        return False

    data = response_data if isinstance(response_data, dict) else {}
    if data:
        if data.get('httpStatusCode') or data.get('statusCode'):
            return False
        if data.get('remark') or data.get('errors'):
            return False

    error_text = str(exc or '').lower()
    network_markers = [
        'network',
        'connection',
        'connect timeout',
        'read timed out',
        'timed out',
        'temporary failure',
        'failed to establish',
        'name or service not known',
        'max retries exceeded',
        'connection aborted',
        'connection refused',
        'connection reset',
        'failed to resolve',
        'nodename nor servname provided',
    ]
    return any(marker in error_text for marker in network_markers)


class TerminalService:
    """Terminal management and onboarding."""

    @staticmethod
    def normalize_device_serial(value: Any) -> str:
        return str(value or '').strip()

    @staticmethod
    def device_serials_match(left: Any, right: Any) -> bool:
        left_serial = TerminalService.normalize_device_serial(left)
        right_serial = TerminalService.normalize_device_serial(right)
        return bool(left_serial and right_serial and left_serial.lower() == right_serial.lower())

    @staticmethod
    def extract_request_device_serial(request) -> str:
        if not request:
            return ''
        try:
            return TerminalService.normalize_device_serial(
                request.headers.get('X-HandyPOS-Device-Serial')
                or request.headers.get('X-Handypos-Device-Serial')
            )
        except Exception:
            return TerminalService.normalize_device_serial(
                getattr(request, 'META', {}).get('HTTP_X_HANDYPOS_DEVICE_SERIAL')
                or getattr(request, 'META', {}).get('HTTP_X_HANDY_POS_DEVICE_SERIAL')
            )

    @staticmethod
    def enforce_terminal_device_binding(
        terminal: Terminal,
        request_device_serial: Any,
        *,
        operation: str = 'sale',
    ) -> None:
        if not bool(getattr(settings, 'MRA_EIS_ENFORCE_TERMINAL_DEVICE_BINDING', True)):
            return

        incoming_serial = TerminalService.normalize_device_serial(request_device_serial)
        if not incoming_serial:
            raise MRAIntegrationError(
                'This device is not identified as an activated MRA EIS terminal. '
                'Open EIS Settings on this device and activate it before making fiscal sales.'
            )

        terminal_serial = TerminalService.normalize_device_serial(getattr(terminal, 'device_serial', ''))
        if not terminal_serial:
            # Legacy active terminals may predate the stricter device binding rule.
            terminal.device_serial = incoming_serial
            terminal.save(update_fields=['device_serial', 'updated_at'])
            return

        if not TerminalService.device_serials_match(terminal_serial, incoming_serial):
            raise MRAIntegrationError(
                f'This device is not the activated MRA EIS terminal for this branch. '
                f'The active terminal is bound to device serial {terminal_serial}. '
                f'Activate this device with its own TAC or transfer/deactivate the old terminal before {operation}.'
            )

    @staticmethod
    def _build_activation_payload(
        *,
        tac_code: str,
        pos_version: str,
        os_type: str,
        mac_address: str,
    ) -> dict[str, Any]:
        product_id = str(getattr(settings, 'MRA_EIS_PRODUCT_ID', '') or 'HandyPOS')[:50]
        os_name = str(os_type or 'Unknown')[:50]
        return {
            'terminalActivationCode': str(tac_code or '').strip(),
            'environment': {
                'platform': {
                    'osName': os_name,
                    'osVersion': os_name,
                    'osBuild': '',
                    'macAddress': (mac_address or '00-00-00-00-00-00')[:17],
                },
                'pos': {
                    'productID': product_id,
                    'productVersion': str(pos_version or '1.0.0')[:50],
                },
            },
        }

    @staticmethod
    def _dict_get_any(mapping: dict[str, Any] | None, *keys: str) -> Any:
        if not isinstance(mapping, dict):
            return None

        for key in keys:
            if key in mapping:
                return mapping[key]

        lowered_keys = {str(key).lower(): value for key, value in mapping.items()}
        for key in keys:
            if key.lower() in lowered_keys:
                return lowered_keys[key.lower()]

        return None

    @staticmethod
    def _find_nested_value(value: Any, *keys: str) -> Any:
        if isinstance(value, dict):
            direct = TerminalService._dict_get_any(value, *keys)
            if direct not in (None, ''):
                return direct
            for nested_value in value.values():
                found = TerminalService._find_nested_value(nested_value, *keys)
                if found not in (None, ''):
                    return found
        elif isinstance(value, list):
            for nested_value in value:
                found = TerminalService._find_nested_value(nested_value, *keys)
                if found not in (None, ''):
                    return found
        return None

    @staticmethod
    def _to_positive_int(value: Any) -> int | None:
        if value in (None, ''):
            return None
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _response_inner(response_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response_data, dict):
            return {}
        data = response_data.get('data')
        return data if isinstance(data, dict) else response_data

    @staticmethod
    def _optional_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if value in (None, ''):
            return None
        normalized = str(value).strip().lower()
        if normalized in {'true', '1', 'yes', 'y', 'on'}:
            return True
        if normalized in {'false', '0', 'no', 'n', 'off'}:
            return False
        return None

    @staticmethod
    def _terminal_id_payload(terminal: Terminal) -> dict[str, str]:
        terminal_id = str(terminal.mra_terminal_id or terminal.terminal_id or '').strip()
        if not terminal_id:
            raise MRAIntegrationError('MRA terminal identifier is missing.')
        return {'terminalId': terminal_id}

    @staticmethod
    def _extract_blocking_status(response_data: dict[str, Any]) -> dict[str, Any]:
        inner = TerminalService._response_inner(response_data)
        return {
            'is_blocked': TerminalService._optional_bool(
                inner.get('isBlocked')
                if 'isBlocked' in inner
                else inner.get('is_blocked')
            ),
            'blocking_reason': str(
                inner.get('blockingReason')
                or inner.get('blocking_reason')
                or inner.get('message')
                or inner.get('remark')
                or response_data.get('remark')
                or ''
            ).strip(),
            'blocked_at': inner.get('blockedAt') or inner.get('blocked_at') or None,
        }

    @staticmethod
    def _extract_unblock_status(response_data: dict[str, Any]) -> dict[str, Any]:
        inner = TerminalService._response_inner(response_data)
        return {
            'is_unblocked': TerminalService._optional_bool(
                inner.get('isUnblocked')
                if 'isUnblocked' in inner
                else inner.get('is_unblocked')
            ),
            'remark': str(inner.get('remark') or response_data.get('remark') or '').strip(),
        }

    @staticmethod
    def _is_ping_response_ok(response_data: dict[str, Any], status_code: int | None = None) -> bool:
        if _extract_mra_response_errors(response_data):
            return False

        status_value = response_data.get('statusCode', response_data.get('status_code'))
        if status_value not in (None, ''):
            try:
                return int(status_value) > 0
            except (TypeError, ValueError):
                pass

        values = [
            response_data.get('raw'),
            response_data.get('data'),
            response_data.get('remark'),
            response_data.get('message'),
            response_data.get('status'),
        ]
        for value in values:
            if isinstance(value, dict):
                nested = str(value.get('message') or value.get('status') or value.get('result') or '').strip().lower()
            else:
                nested = str(value or '').strip().lower()
            if nested in {'pong', 'ok', 'success', 'successful', 'online'}:
                return True

        return bool(status_code and 200 <= int(status_code) < 300)

    @staticmethod
    def _normalize_ping_server_time(value: Any) -> str:
        raw = str(value or '').strip()
        if not raw:
            return ''

        try:
            if ',' in raw and ('GMT' in raw.upper() or raw[:3].isalpha()):
                parsed = parsedate_to_datetime(raw)
            else:
                parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))

            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, datetime_timezone.utc)
            return parsed.isoformat()
        except Exception:
            return raw

    @staticmethod
    def _extract_ping_server_time(response_data: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        server_time_keys = (
            'serverTime',
            'server_time',
            'serverDateTime',
            'server_date_time',
            'serverDate',
            'server_date',
            'dateTime',
            'date_time',
            'datetime',
            'timestamp',
            'time',
            'date',
        )

        found_value = TerminalService._find_nested_value(response_data, *server_time_keys)
        if found_value not in (None, ''):
            return {
                'server_time': TerminalService._normalize_ping_server_time(found_value),
                'server_time_raw': str(found_value),
                'server_time_source': 'response',
            }

        headers = headers or {}
        header_value = (
            TerminalService._dict_get_any(headers, 'Date')
            or TerminalService._dict_get_any(headers, 'date')
        )
        if header_value not in (None, ''):
            return {
                'server_time': TerminalService._normalize_ping_server_time(header_value),
                'server_time_raw': str(header_value),
                'server_time_source': 'http_date_header',
            }

        return {
            'server_time': None,
            'server_time_raw': None,
            'server_time_source': None,
        }

    @staticmethod
    def _parse_server_time(value: Any) -> datetime | None:
        normalized = TerminalService._normalize_ping_server_time(value)
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(str(normalized).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, datetime_timezone.utc)
        return parsed

    @staticmethod
    def record_server_time_sync(
        terminal: Terminal,
        *,
        server_time: Any,
        checked_at: datetime | None = None,
        source: str = 'mra_ping',
        response_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        parsed_server_time = TerminalService._parse_server_time(server_time)
        if parsed_server_time is None:
            return None

        checked_at = checked_at or timezone.now()
        details = {
            'source': 'mra_server_time_sync',
            'ping_source': source,
            'server_time': parsed_server_time.isoformat(),
            'checked_at': checked_at.isoformat(),
            'local_checked_at': checked_at.isoformat(),
            'response': response_data or {},
        }
        TerminalAuditLog.objects.create(
            terminal=terminal,
            action='online_status_changed',
            details=details,
        )
        return details

    @staticmethod
    def get_latest_server_time_sync(terminal: Terminal) -> dict[str, Any] | None:
        try:
            audits = terminal.audit_logs.filter(action='online_status_changed').order_by('-created_at')[:50]
        except Exception:
            return None

        for audit in audits:
            details = audit.details if isinstance(audit.details, dict) else {}
            if details.get('source') != 'mra_server_time_sync':
                continue
            server_time = TerminalService._parse_server_time(details.get('server_time'))
            checked_at = TerminalService._parse_server_time(details.get('checked_at')) or audit.created_at
            if server_time is None:
                continue
            return {
                'server_time': server_time,
                'checked_at': checked_at,
                'source': details.get('ping_source') or 'mra_ping',
                'audit_id': str(audit.id),
            }
        return None

    @staticmethod
    def resolve_mra_transaction_time(
        terminal: Terminal,
        *,
        require_live_ping: bool,
        allow_cached: bool = True,
    ) -> tuple[datetime, dict[str, Any]]:
        max_age_hours = float(getattr(settings, 'MRA_EIS_SERVER_TIME_MAX_AGE_HOURS', 24) or 24)
        now = timezone.now()

        sync = TerminalService.get_latest_server_time_sync(terminal) if allow_cached else None
        if sync:
            checked_at = sync['checked_at']
            age_hours = max((now - checked_at).total_seconds() / 3600, 0)
            if age_hours <= max_age_hours:
                adjusted_time = sync['server_time'] + (now - checked_at)
                return adjusted_time, {
                    'source': 'cached_mra_server_time',
                    'checked_at': checked_at.isoformat(),
                    'server_time': sync['server_time'].isoformat(),
                    'adjusted_time': adjusted_time.isoformat(),
                    'age_hours': round(age_hours, 4),
                    'max_age_hours': max_age_hours,
                    'live_ping': False,
                }

        if require_live_ping:
            health = TerminalService.check_terminal_health(terminal)
            if health.get('is_online') is not True:
                raise MRAIntegrationError('MRA server time unavailable. Connect to internet and retry.')
            server_time = TerminalService._parse_server_time(
                health.get('server_time') or health.get('server_time_raw')
            )
            if server_time is None:
                raise MRAIntegrationError('MRA ping did not return server time. Retry server ping.')
            return server_time, {
                'source': health.get('server_time_source') or 'mra_ping',
                'checked_at': health.get('checked_at'),
                'server_time': server_time.isoformat(),
                'live_ping': True,
            }

        if sync:
            raise MRAIntegrationError('MRA server time sync expired. Connect to internet and ping MRA first.')

        raise MRAIntegrationError('MRA server time not synced. Connect to internet and ping MRA first.')

    @staticmethod
    def check_terminal_health(terminal: Terminal) -> dict[str, Any]:
        """Use the official MRA utilities ping as the source of terminal online status."""
        checked_at = timezone.now()
        client = MRAEISClient(terminal=terminal)
        endpoint = client._resolve_endpoint('ping')

        try:
            result = None
            ping_method = 'POST'
            last_method_error = None
            for method in ('POST', 'GET'):
                try:
                    result = client.call(
                        'ping',
                        payload=None,
                        method=method,
                        mutating=False,
                        send_json=False,
                        record_connectivity=False,
                    )
                    ping_method = method
                    break
                except MRAIntegrationError as exc:
                    last_method_error = exc
                    if getattr(exc, 'status_code', None) not in (404, 405):
                        raise

            if result is None:
                raise last_method_error or MRAIntegrationError(
                    'MRA request failed (ping): no ping method succeeded',
                    endpoint=endpoint,
                    endpoint_key='ping',
                )

            response_data = MRAEISClient._normalize_response_data(result.data)
            server_time_details = TerminalService._extract_ping_server_time(
                response_data,
                getattr(result, 'headers', None),
            )

            if result.dry_run:
                return {
                    'checked': False,
                    'dry_run': True,
                    'is_online': bool(terminal.is_online),
                    'endpoint': result.endpoint,
                    'method': ping_method,
                    'status_code': result.status_code,
                    'response': response_data,
                    'checked_at': checked_at.isoformat(),
                    **server_time_details,
                }

            is_online = TerminalService._is_ping_response_ok(response_data, result.status_code)
            previous_online = bool(terminal.is_online)
            if previous_online != is_online:
                TerminalService.update_online_status(
                    terminal,
                    is_online,
                    source='mra_ping',
                    run_reconnect_tasks=False,
                )
                terminal.refresh_from_db()

            if is_online:
                terminal.last_sync_at = checked_at
                terminal.save(update_fields=['last_sync_at', 'updated_at'])
                if server_time_details.get('server_time'):
                    TerminalService.record_server_time_sync(
                        terminal,
                        server_time=server_time_details.get('server_time'),
                        checked_at=checked_at,
                        source=server_time_details.get('server_time_source') or 'mra_ping',
                        response_data=response_data,
                    )

            return {
                'checked': True,
                'dry_run': False,
                'is_online': is_online,
                'previous_online': previous_online,
                'endpoint': result.endpoint,
                'method': ping_method,
                'status_code': result.status_code,
                'response': response_data,
                'checked_at': checked_at.isoformat(),
                'errors': _extract_mra_response_errors(response_data),
                **server_time_details,
            }
        except MRAIntegrationError as exc:
            previous_online = bool(terminal.is_online)
            if previous_online:
                TerminalService.update_online_status(
                    terminal,
                    False,
                    source='mra_ping',
                    run_reconnect_tasks=False,
                )
                terminal.refresh_from_db()

            response_data = getattr(exc, 'response_data', None)
            response_data = response_data if isinstance(response_data, dict) else {}
            return {
                'checked': True,
                'dry_run': False,
                'is_online': False,
                'previous_online': previous_online,
                'endpoint': getattr(exc, 'endpoint', None) or endpoint,
                'endpoint_key': getattr(exc, 'endpoint_key', None) or 'ping',
                'status_code': getattr(exc, 'status_code', None),
                'response': response_data,
                'error': str(exc),
                'checked_at': checked_at.isoformat(),
            }

    @staticmethod
    def get_cached_blocking_status(terminal: Terminal) -> dict[str, Any] | None:
        for audit in terminal.audit_logs.order_by('-created_at')[:25]:
            details = audit.details if isinstance(audit.details, dict) else {}
            source = str(details.get('source') or '')
            if source not in {
                'mra_terminal_blocking_message',
                'mra_terminal_unblock_status',
                'mra_sale_response_terminal_block',
            }:
                continue

            blocking_status = details.get('blocking_status') if isinstance(details.get('blocking_status'), dict) else {}
            if blocking_status:
                return {
                    **blocking_status,
                    'source': source,
                    'checked_at': audit.created_at.isoformat(),
                }

            if 'is_unblocked' in details:
                return {
                    'is_blocked': not bool(details.get('is_unblocked')),
                    'is_unblocked': bool(details.get('is_unblocked')),
                    'blocking_reason': details.get('remark') or '',
                    'source': source,
                    'checked_at': audit.created_at.isoformat(),
                }

        if terminal.status == 'suspended':
            return {
                'is_blocked': True,
                'blocking_reason': 'Terminal is suspended locally. Check MRA block status for the official reason.',
                'source': 'local_terminal_status',
                'checked_at': None,
            }
        return None

    @staticmethod
    def ensure_terminal_not_blocked_for_sale(terminal: Terminal) -> dict[str, Any]:
        try:
            blocking = TerminalService.get_terminal_blocking_message(terminal)
        except MRAIntegrationError as exc:
            response_data = getattr(exc, 'response_data', None)
            status_code = int(getattr(exc, 'status_code', None) or 0)
            if _is_mra_network_failure(exc, status_code=status_code, response_data=response_data):
                cached = TerminalService.get_cached_blocking_status(terminal) or {}
                if terminal.status == 'suspended' or cached.get('is_blocked') is True:
                    reason = (
                        cached.get('blocking_reason')
                        or 'Terminal is suspended locally. Check MRA block status when online.'
                    )
                    raise MRAIntegrationError(f'MRA terminal is blocked: {reason}') from exc

                return {
                    'checked': False,
                    'is_blocked': False,
                    'source': 'mra_unreachable_cached_terminal_state',
                    'reason': 'mra_network_unreachable',
                    'error': str(exc),
                    'cached_blocking_status': cached,
                }

            raise MRAIntegrationError(
                'Could not verify MRA terminal block status. Connect to internet and retry.',
                status_code=getattr(exc, 'status_code', None),
                endpoint=getattr(exc, 'endpoint', None),
                endpoint_key=getattr(exc, 'endpoint_key', None) or 'get_terminal_blocking_message',
                response_data=getattr(exc, 'response_data', None),
            ) from exc

        terminal.refresh_from_db()
        errors = _extract_mra_response_errors(blocking.get('response') if isinstance(blocking, dict) else {})
        errors.extend(str(error) for error in (blocking.get('errors') or []) if error)
        if errors:
            raise MRAIntegrationError(
                'Could not verify MRA terminal block status: ' + '; '.join(dict.fromkeys(errors))
            )

        if blocking.get('is_blocked') is True or terminal.status == 'suspended':
            cached = TerminalService.get_cached_blocking_status(terminal) or {}
            reason = (
                blocking.get('blocking_reason')
                or cached.get('blocking_reason')
                or 'MRA reports this terminal is blocked.'
            )
            raise MRAIntegrationError(f'MRA terminal is blocked: {reason}')

        return blocking

    @staticmethod
    def record_terminal_blocked(
        terminal: Terminal,
        *,
        reason: str,
        source: str,
        response_data: dict[str, Any] | None = None,
        blocking_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status_payload = {
            'is_blocked': True,
            'blocking_reason': str(reason or 'MRA requested terminal block.').strip(),
            'blocked_at': None,
            **(blocking_status or {}),
        }
        terminal.status = 'suspended'
        terminal.save(update_fields=['status', 'updated_at'])
        TerminalAuditLog.objects.create(
            terminal=terminal,
            action='suspended',
            details={
                'source': source,
                'blocking_status': status_payload,
                'response': response_data or {},
            },
        )
        return status_payload

    @staticmethod
    def get_terminal_blocking_message(terminal: Terminal) -> dict[str, Any]:
        payload = TerminalService._terminal_id_payload(terminal)
        result = MRAEISClient(terminal=terminal).call(
            'get_terminal_blocking_message',
            payload=payload,
            method='POST',
            mutating=False,
        )
        response_data = MRAEISClient._normalize_response_data(result.data)
        response_errors = _extract_mra_response_errors(response_data)
        parsed_status = TerminalService._extract_blocking_status(response_data)
        is_blocked = parsed_status.get('is_blocked')

        if result.dry_run:
            return {
                'checked': False,
                'dry_run': True,
                'terminal_id': payload['terminalId'],
                **parsed_status,
                'response': response_data,
            }

        if not response_errors and is_blocked is True:
            TerminalService.record_terminal_blocked(
                terminal,
                reason=parsed_status.get('blocking_reason') or 'MRA reports this terminal is blocked.',
                source='mra_terminal_blocking_message',
                response_data=response_data,
                blocking_status=parsed_status,
            )
        elif not response_errors:
            TerminalAuditLog.objects.create(
                terminal=terminal,
                action='configuration_updated',
                details={
                    'source': 'mra_terminal_blocking_message',
                    'blocking_status': parsed_status,
                    'response': response_data,
                },
            )

        return {
            'checked': True,
            'dry_run': False,
            'terminal_id': payload['terminalId'],
            **parsed_status,
            'response': response_data,
            'errors': response_errors,
        }

    @staticmethod
    def check_terminal_unblock_status(terminal: Terminal) -> dict[str, Any]:
        payload = TerminalService._terminal_id_payload(terminal)
        previous_status = terminal.status
        result = MRAEISClient(terminal=terminal).call(
            'check_terminal_unblock_status',
            payload=payload,
            method='POST',
            mutating=False,
        )
        response_data = MRAEISClient._normalize_response_data(result.data)
        response_errors = _extract_mra_response_errors(response_data)
        parsed_status = TerminalService._extract_unblock_status(response_data)
        is_unblocked = parsed_status.get('is_unblocked')

        if result.dry_run:
            return {
                'checked': False,
                'dry_run': True,
                'terminal_id': payload['terminalId'],
                **parsed_status,
                'response': response_data,
            }

        if not response_errors and is_unblocked is True and terminal.status == 'suspended':
            terminal.status = 'active'
            terminal.save(update_fields=['status', 'updated_at'])

        TerminalAuditLog.objects.create(
            terminal=terminal,
            action='online_status_changed' if is_unblocked else 'suspended',
            details={
                'source': 'mra_terminal_unblock_status',
                'previous_status': previous_status,
                'current_status': terminal.status,
                **parsed_status,
                'blocking_status': {
                    'is_blocked': False if is_unblocked is True else terminal.status == 'suspended',
                    'is_unblocked': is_unblocked,
                    'blocking_reason': parsed_status.get('remark') or '',
                },
                'response': response_data,
            },
        )

        return {
            'checked': True,
            'dry_run': False,
            'terminal_id': payload['terminalId'],
            **parsed_status,
            'previous_status': previous_status,
            'current_status': terminal.status,
            'response': response_data,
            'errors': response_errors,
        }

    @staticmethod
    def sync_terminal_blocking_status(terminal: Terminal) -> dict[str, Any]:
        was_suspended = terminal.status == 'suspended'
        blocking = TerminalService.get_terminal_blocking_message(terminal)
        terminal.refresh_from_db()

        should_check_unblock = was_suspended or blocking.get('is_blocked') is True
        unblock = None
        if should_check_unblock:
            unblock = TerminalService.check_terminal_unblock_status(terminal)
            terminal.refresh_from_db()

        cached = TerminalService.get_cached_blocking_status(terminal)
        return {
            'terminal_id': terminal.terminal_id,
            'mra_terminal_id': terminal.mra_terminal_id,
            'status': terminal.status,
            'is_blocked': terminal.status == 'suspended',
            'blocking_status': cached,
            'blocking_message': blocking,
            'unblock_status': unblock,
            'checked_at': timezone.now().isoformat(),
        }

    @staticmethod
    def _response_data(response: dict[str, Any]) -> dict[str, Any]:
        data = TerminalService._dict_get_any(response, 'data')
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_activation_terminal(response: dict[str, Any]) -> dict[str, Any]:
        data = TerminalService._response_data(response)
        activated_terminal = TerminalService._dict_get_any(
            data,
            'activatedTerminal',
            'ActivatedTerminal',
            'activated_terminal',
        )
        if isinstance(activated_terminal, dict):
            return activated_terminal
        return data if data else response

    @staticmethod
    def _extract_activation_configuration(response: dict[str, Any]) -> dict[str, Any]:
        data = TerminalService._response_data(response)
        configuration = TerminalService._dict_get_any(data, 'configuration', 'Configuration')
        return configuration if isinstance(configuration, dict) else {}

    @staticmethod
    def _safe_activation_response_shape(response: dict[str, Any]) -> dict[str, Any]:
        data = TerminalService._response_data(response)
        activated_terminal = TerminalService._extract_activation_terminal(response)
        terminal_credentials = TerminalService._dict_get_any(
            activated_terminal,
            'terminalCredentials',
            'TerminalCredentials',
            'terminal_credentials',
        )
        if not isinstance(terminal_credentials, dict):
            terminal_credentials = {}

        def keys(value: Any) -> list[str]:
            return sorted(str(key) for key in value.keys()) if isinstance(value, dict) else []

        return {
            'response_keys': keys(response),
            'data_keys': keys(data),
            'activated_terminal_keys': keys(activated_terminal),
            'terminal_credentials_keys': keys(terminal_credentials),
        }

    @staticmethod
    def _extract_header_token(headers: dict[str, Any] | None) -> str:
        header_value = TerminalService._dict_get_any(
            headers,
            'Authorization',
            'X-Authorization',
            'X-Access-Token',
            'X-JWT-Token',
            'jwtToken',
            'accessToken',
        )
        if not header_value:
            return ''

        header_value = str(header_value).strip()
        if header_value.lower().startswith('bearer '):
            return header_value.split(' ', 1)[1].strip()
        return header_value

    @staticmethod
    def _upsert_terminal(
        *,
        business,
        branch,
        pos_name: str,
        pos_version: str,
        os_type: str,
        device_serial: str,
        mac_address: str,
    ) -> Terminal:
        incoming_serial = TerminalService.normalize_device_serial(device_serial)
        terminal_qs = Terminal.objects.select_for_update().filter(business=business, branch=branch)
        if incoming_serial:
            terminal = terminal_qs.filter(device_serial__iexact=incoming_serial).order_by('-updated_at').first()
        else:
            terminal = terminal_qs.order_by('-updated_at').first()

        if terminal:
            existing_serial = TerminalService.normalize_device_serial(terminal.device_serial)
            if incoming_serial:
                terminal.device_serial = incoming_serial
            elif not existing_serial:
                terminal.device_serial = device_serial
            terminal.mac_address = mac_address
            terminal.pos_name = pos_name
            terminal.pos_version = pos_version
            terminal.os_type = os_type
            terminal.save(
                update_fields=['device_serial', 'mac_address', 'pos_name', 'pos_version', 'os_type', 'updated_at']
            )
            return terminal

        local_terminal_id = f"TRM-{branch.id}-{uuid.uuid4().hex[:8].upper()}"
        return Terminal.objects.create(
            business=business,
            branch=branch,
            terminal_id=local_terminal_id,
            device_serial=incoming_serial or device_serial,
            mac_address=mac_address,
            pos_name=pos_name,
            pos_version=pos_version,
            os_type=os_type,
            mra_terminal_id=local_terminal_id,
            mra_api_key='',
            status='pending_activation',
        )

    @staticmethod
    @transaction.atomic
    def activate_terminal(
        business,
        branch,
        tac_code,
        pos_name,
        pos_version,
        os_type,
        device_serial,
        mac_address=None,
    ):
        """
        Activate terminal in an onboarding-ready way.

        - Keeps local TAC compatibility.
        - Calls official onboarding endpoint when live submission is enabled.
        - In dry-run mode, payload is prepared and stored but not sent.
        """
        local_tac = TerminalActivationCode.objects.filter(code=tac_code, business=business).first()
        if local_tac and not local_tac.is_valid():
            raise ValueError('TAC is invalid or expired')

        if getattr(settings, 'MRA_EIS_REQUIRE_LOCAL_TAC', False) and not local_tac:
            raise ValueError('TAC is not registered locally. Create/import TAC first.')

        terminal = TerminalService._upsert_terminal(
            business=business,
            branch=branch,
            pos_name=pos_name,
            pos_version=pos_version,
            os_type=os_type,
            device_serial=device_serial,
            mac_address=mac_address or '',
        )

        payload = TerminalService._build_activation_payload(
            tac_code=tac_code,
            pos_version=pos_version,
            os_type=os_type,
            mac_address=mac_address or '',
        )

        client = MRAEISClient(terminal=terminal)
        activation_endpoint = client._resolve_endpoint('activate_terminal')
        try:
            result = client.call('activate_terminal', payload=payload, method='POST', mutating=True)
        except Exception as exc:
            logger.warning('Terminal activation call failed: %s', exc)
            failure_details = {
                'state': 'failed',
                'dry_run': False,
                'endpoint': activation_endpoint,
                'request_payload': payload,
                'error': str(exc),
            }
            if client.http_enabled and not client.dry_run and client.allow_live_submission:
                MRAAPIError.objects.create(
                    terminal=terminal,
                    error_type='activation_failed',
                    error_message=str(exc),
                )
                TerminalAuditLog.objects.create(
                    terminal=terminal,
                    action='activated',
                    details=failure_details,
                )
                raise MRAIntegrationError(f'MRA terminal activation failed: {exc}') from exc

            result = MRACallResult(
                ok=False,
                dry_run=True,
                status_code=0,
                endpoint=activation_endpoint,
                data={'status': 'pending_activation', 'reason': 'activation_call_failed', 'error': str(exc)},
            )
        response_data = MRAEISClient._normalize_response_data(result.data)
        activation_response_shape = TerminalService._safe_activation_response_shape(response_data)
        response_errors = _extract_mra_response_errors(response_data)
        if response_errors and not result.dry_run:
            error_message = '; '.join(response_errors)
            MRAAPIError.objects.create(
                terminal=terminal,
                error_type='activation_rejected',
                error_message=error_message,
                error_code=str(response_data.get('statusCode') or response_data.get('status_code') or ''),
            )
            TerminalAuditLog.objects.create(
                terminal=terminal,
                action='activated',
                details={
                    'state': 'failed',
                    'dry_run': False,
                    'endpoint': result.endpoint,
                    'status_code': result.status_code,
                    'request_payload': payload,
                    'response': response_data,
                    'error': error_message,
                },
            )
            raise MRAIntegrationError(f'MRA rejected terminal activation: {error_message}')

        activated_terminal = TerminalService._extract_activation_terminal(response_data)
        terminal_credentials = TerminalService._dict_get_any(
            activated_terminal,
            'terminalCredentials',
            'TerminalCredentials',
            'terminal_credentials',
        ) or {}
        if not isinstance(terminal_credentials, dict):
            terminal_credentials = {}

        mra_terminal_id = (
            TerminalService._dict_get_any(
                activated_terminal,
                'terminalId',
                'terminalID',
                'terminal_id',
                'mra_terminal_id',
                'deviceId',
                'deviceID',
            )
            or TerminalService._find_nested_value(
                response_data,
                'terminalId',
                'terminalID',
                'terminal_id',
                'mra_terminal_id',
                'deviceId',
                'deviceID',
            )
            or terminal.mra_terminal_id
            or tac_code
        )

        token = (
            TerminalService._dict_get_any(
                terminal_credentials,
                'jwtToken',
                'JWTToken',
                'jwt_token',
                'token',
                'accessToken',
                'access_token',
            )
            or TerminalService._dict_get_any(
                activated_terminal,
                'jwtToken',
                'JWTToken',
                'jwt_token',
                'token',
                'accessToken',
                'access_token',
            )
            or TerminalService._find_nested_value(
                response_data,
                'jwtToken',
                'JWTToken',
                'jwt_token',
                'token',
                'accessToken',
                'access_token',
            )
            or TerminalService._extract_header_token(getattr(result, 'headers', {}))
            or ''
        )
        secret_key = (
            TerminalService._dict_get_any(
                terminal_credentials,
                'secretKey',
                'SecretKey',
                'secret_key',
                'accessKey',
                'access_key',
                'apiKey',
                'api_key',
            )
            or TerminalService._dict_get_any(
                activated_terminal,
                'secretKey',
                'SecretKey',
                'secret_key',
                'accessKey',
                'access_key',
                'apiKey',
                'api_key',
            )
            or TerminalService._find_nested_value(
                response_data,
                'secretKey',
                'SecretKey',
                'secret_key',
                'accessKey',
                'access_key',
                'apiKey',
                'api_key',
            )
            or terminal.mra_api_key
            or ''
        )
        taxpayer_id = TerminalService._to_positive_int(
            TerminalService._dict_get_any(
                activated_terminal,
                'taxpayerId',
                'TaxpayerId',
                'taxpayerID',
                'TaxpayerID',
                'taxpayer_id',
                'businessId',
                'BusinessId',
            )
            or TerminalService._find_nested_value(
                response_data,
                'taxpayerId',
                'TaxpayerId',
                'taxpayerID',
                'TaxpayerID',
                'taxpayer_id',
                'businessId',
                'BusinessId',
            )
        )
        terminal_position = TerminalService._to_positive_int(
            TerminalService._dict_get_any(
                activated_terminal,
                'terminalPosition',
                'TerminalPosition',
                'terminal_position',
                'position',
                'Position',
            )
            or TerminalService._find_nested_value(
                response_data,
                'terminalPosition',
                'TerminalPosition',
                'terminal_position',
                'position',
                'Position',
            )
        )

        terminal.mra_terminal_id = mra_terminal_id
        if taxpayer_id:
            terminal.mra_taxpayer_id = taxpayer_id
        if terminal_position:
            terminal.terminal_position = terminal_position
        terminal.mra_api_key = secret_key
        terminal.mra_token = token
        terminal.token_expires_at = timezone.now() + timedelta(hours=24) if token else None
        terminal.status = 'pending_activation'
        terminal.save()
        if branch is not None:
            branch_update_fields = ['mra_terminal_id', 'mra_terminal_position', 'eis_mapping_source', 'eis_mapping_updated_at', 'updated_at']
            branch.mra_terminal_id = str(terminal.mra_terminal_id or '')
            branch.mra_terminal_position = terminal.terminal_position
            branch.eis_mapping_source = 'activation'
            branch.eis_mapping_updated_at = timezone.now()
            branch.save(update_fields=branch_update_fields)

        activation_configuration = TerminalService._extract_activation_configuration(response_data)
        configuration_received_before_confirmation = False
        pre_confirmation_configuration_result: dict[str, Any] | None = None
        if activation_configuration:
            try:
                ConfigurationService.store_configuration_response(
                    business=business,
                    response_data={'data': activation_configuration},
                    source='activation',
                    branch=branch,
                )
                configuration_received_before_confirmation = True
            except Exception as exc:
                logger.warning('Activation configuration storage failed for terminal %s: %s', terminal.id, exc)
                MRAAPIError.objects.create(
                    terminal=terminal,
                    error_type='configuration_sync_failed',
                    error_message=str(exc),
                )

        confirmation_result: dict[str, Any] | None = None
        confirmation_state = 'not_required' if result.dry_run else 'not_attempted'
        activation_error = ''
        if not result.dry_run and terminal.mra_terminal_id and terminal.mra_api_key and terminal.mra_token:
            client.terminal = terminal
            if not configuration_received_before_confirmation:
                try:
                    pre_confirmation_configuration_result = ConfigurationService.ensure_fresh_configuration(
                        business,
                        terminal=terminal,
                        require_success=False,
                    )
                    configuration_received_before_confirmation = bool(
                        pre_confirmation_configuration_result.get('fresh')
                    )
                except Exception as exc:
                    pre_confirmation_configuration_result = {'fresh': False, 'error': str(exc)}
                    logger.warning('Pre-confirmation configuration refresh failed for terminal %s: %s', terminal.id, exc)

            if not configuration_received_before_confirmation:
                confirmation_state = 'configuration_missing'
                activation_error = (
                    'MRA terminal activation was not confirmed because configuration was not received/stored first.'
                )
                MRAAPIError.objects.create(
                    terminal=terminal,
                    error_type='activation_configuration_missing',
                    error_message=activation_error,
                )
            else:
                confirm_payload = {'terminalId': terminal.mra_terminal_id}
                try:
                    confirm_result = client.call(
                        'confirm_terminal',
                        payload=confirm_payload,
                        method='POST',
                        mutating=True,
                        x_signature_text=str(tac_code),
                    )
                    confirmation_result = confirm_result.data
                    confirm_errors = _extract_mra_response_errors(confirmation_result)
                    confirm_data = confirm_result.data.get('data') if isinstance(confirm_result.data, dict) else None
                    if confirm_data is True or str(confirm_result.data.get('statusCode', '')).strip() == '1':
                        confirmation_state = 'confirmed'
                        terminal.status = 'active'
                        terminal.activated_at = timezone.now()
                        terminal.save(update_fields=['status', 'activated_at', 'updated_at'])
                        if terminal.branch_id:
                            terminal.branch.mra_terminal_id = str(terminal.mra_terminal_id or '')
                            terminal.branch.mra_terminal_position = terminal.terminal_position
                            terminal.branch.eis_mapping_source = 'activation-confirmed'
                            terminal.branch.eis_mapping_updated_at = timezone.now()
                            terminal.branch.save(
                                update_fields=[
                                    'mra_terminal_id',
                                    'mra_terminal_position',
                                    'eis_mapping_source',
                                    'eis_mapping_updated_at',
                                    'updated_at',
                                ]
                            )
                        try:
                            ConfigurationService.ensure_fresh_configuration(
                                business,
                                terminal=terminal,
                                require_success=False,
                            )
                        except Exception as exc:
                            logger.warning('Post-activation configuration refresh failed for terminal %s: %s', terminal.id, exc)
                    elif confirm_errors:
                        confirmation_state = 'failed'
                        activation_error = '; '.join(confirm_errors)
                        MRAAPIError.objects.create(
                            terminal=terminal,
                            error_type='activation_confirmation_rejected',
                            error_message=activation_error,
                            error_code=str(confirm_result.data.get('statusCode') or confirm_result.data.get('status_code') or ''),
                        )
                    else:
                        confirmation_state = 'pending'
                except Exception as exc:
                    confirmation_state = 'failed'
                    activation_error = f'MRA terminal activation confirmation failed: {exc}'
                    confirmation_result = {'error': str(exc)}
                    MRAAPIError.objects.create(
                        terminal=terminal,
                        error_type='activation_confirmation_failed',
                        error_message=str(exc),
                    )
        elif not result.dry_run:
            confirmation_state = 'missing_terminal_credentials'
            missing_credentials = []
            if not terminal.mra_terminal_id:
                missing_credentials.append('terminalId')
            if not terminal.mra_api_key:
                missing_credentials.append('secretKey')
            if not terminal.mra_token:
                missing_credentials.append('jwtToken')
            activation_error = (
                'MRA activation response did not include '
                f"{', '.join(missing_credentials) or 'terminal credentials'} required for confirmation."
            )

        if local_tac:
            if local_tac.status == 'unused':
                local_tac.mark_as_used(terminal)
            elif local_tac.used_by_terminal_id != terminal.id:
                raise ValueError('TAC has already been used by another terminal')

        dry_run_reason = response_data.get('reason') if result.dry_run and isinstance(response_data, dict) else ''
        activation_state = 'prepared' if result.dry_run else (
            'active' if terminal.status == 'active' else 'pending_confirmation'
        )
        TerminalAuditLog.objects.create(
            terminal=terminal,
            action='activated',
            details={
                'state': activation_state,
                'dry_run': result.dry_run,
                'dry_run_reason': dry_run_reason,
                'endpoint': result.endpoint,
                'status_code': result.status_code,
                'request_payload': payload,
                'response': response_data,
                'activation_response_shape': activation_response_shape,
                'configuration_received_before_confirmation': configuration_received_before_confirmation,
                'pre_confirmation_configuration': pre_confirmation_configuration_result,
                'confirmation_response': confirmation_result,
                'confirmation_state': confirmation_state,
                'error': activation_error,
            },
        )

        return terminal

    @staticmethod
    @transaction.atomic
    def reset_failed_activation(terminal: Terminal) -> dict[str, Any]:
        """Remove a local failed activation record so the branch can retry onboarding."""
        invoice_count = MRAInvoice.objects.filter(terminal=terminal).count()
        queue_count = OfflineInvoiceQueue.objects.filter(terminal=terminal).count()
        has_counters = bool(terminal.online_invoice_counter or terminal.offline_invoice_counter)
        has_token = bool(terminal.mra_token)

        if terminal.status == 'active' and has_token:
            raise ValueError('Active terminals cannot be reset. Deactivate through MRA support if needed.')

        if invoice_count or queue_count or has_counters:
            raise ValueError('Cannot reset a terminal that already has fiscal invoices, queued invoices, or invoice counters.')

        terminal_id = str(terminal.id)
        branch_id = str(terminal.branch_id)
        terminal_label = terminal.terminal_id

        TerminalActivationCode.objects.filter(used_by_terminal=terminal).update(
            status='unused',
            used_by_terminal=None,
            used_at=None,
        )
        terminal.delete()

        return {
            'status': 'reset',
            'terminal_id': terminal_id,
            'branch_id': branch_id,
            'terminal_label': terminal_label,
            'message': 'Local failed activation was removed. Use a fresh TAC if MRA already consumed the previous one.',
        }

    @staticmethod
    def refresh_token(terminal):
        """Request a fresh terminal token using the official MRA endpoint."""
        if not terminal.mra_token:
            raise MRAIntegrationError(
                'Terminal JWT token is missing. MRA cannot refresh a token before activation '
                'returns and stores jwtToken; use a fresh TAC or ask MRA to reset this test terminal.'
            )

        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'request_new_terminal_token',
            payload=None,
            method='POST',
            mutating=False,
        )

        response_data = result.data if isinstance(result.data, dict) else {}
        inner_data = response_data.get('data')
        inner_data = inner_data if isinstance(inner_data, dict) else {}

        token = (
            inner_data.get('jwtToken')
            or inner_data.get('token')
            or inner_data.get('accessToken')
            or inner_data.get('access_token')
            or response_data.get('jwtToken')
            or response_data.get('token')
            or response_data.get('accessToken')
            or response_data.get('access_token')
            or ''
        )

        if token:
            terminal.mra_token = token
            terminal.token_expires_at = timezone.now() + timedelta(hours=24)
            terminal.save(update_fields=['mra_token', 'token_expires_at', 'updated_at'])

        TerminalAuditLog.objects.create(
            terminal=terminal,
            action='token_refreshed',
            details={
                'dry_run': result.dry_run,
                'response': response_data,
            },
        )

        return terminal

    @staticmethod
    def update_online_status(terminal, is_online, *, source: str = 'manual', run_reconnect_tasks: bool = True):
        """Update online/offline status with audit trail."""
        if terminal.is_online != is_online:
            terminal.is_online = is_online
            terminal.save(update_fields=['is_online', 'updated_at'])

            event_type = 'online_detected' if is_online else 'offline_detected'
            OfflineAuditLog.objects.create(
                terminal=terminal,
                event_type=event_type,
                details={'timestamp': timezone.now().isoformat(), 'source': source},
            )

            TerminalAuditLog.objects.create(
                terminal=terminal,
                action='online_status_changed',
                details={'is_online': is_online, 'source': source},
            )

            # Best effort: when connectivity is restored, immediately try
            # syncing queued offline invoices in sequence.
            if is_online and run_reconnect_tasks:
                try:
                    ConfigurationService.ensure_fresh_configuration(
                        terminal.business,
                        terminal=terminal,
                        require_success=False,
                    )
                    TransactionReconciliationService.reconcile_terminal(terminal)
                    InvoiceService.sync_offline_invoices(terminal)
                    RetryService.process_retry_queue()
                except Exception as exc:
                    logger.warning(
                        'Automatic offline sync on reconnect failed for terminal %s: %s',
                        terminal.terminal_id,
                        exc,
                    )


class ConfigurationService:
    """MRA configuration sync and retrieval."""

    @staticmethod
    def _unwrap_response_data(response_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response_data, dict):
            return {}
        data = response_data.get('data')
        return data if isinstance(data, dict) else response_data

    @staticmethod
    def _extract_config_data(data: dict[str, Any], config_type: str) -> dict[str, Any]:
        data = ConfigurationService._unwrap_response_data(data)
        if not data:
            return {'source': 'dry_run', 'config_type': config_type}

        if config_type in data and isinstance(data[config_type], dict):
            return data[config_type]

        official_map = {
            'global_configuration': 'globalConfiguration',
            'terminal_configuration': 'terminalConfiguration',
            'taxpayer_configuration': 'taxpayerConfiguration',
            'tax_rules': 'globalConfiguration',
            'receipt_format': 'terminalConfiguration',
            'system_settings': None,
            'product_codes': 'terminalSiteProducts',
            'terminal_site_products': 'terminalSiteProducts',
        }
        official_key = official_map.get(config_type)
        if official_key and isinstance(data.get(official_key), (dict, list)):
            found = data[official_key]
            if isinstance(found, dict):
                return found
            return {'items': found}
        if config_type == 'system_settings':
            return data

        if 'configurations' in data and isinstance(data['configurations'], dict):
            found = data['configurations'].get(config_type)
            if isinstance(found, dict):
                return found

        return {'raw': data, 'config_type': config_type}

    @staticmethod
    def _config_version(config_data: dict[str, Any], fallback_prefix: str) -> str:
        version = (
            config_data.get('versionNo')
            or config_data.get('version')
            or config_data.get('configVersion')
        )
        if version not in (None, ''):
            return str(version)
        return f"{fallback_prefix}-{timezone.now().strftime('%Y%m%d%H%M%S%f')}"

    @staticmethod
    def _replace_active_config(business, config_type: str, config_data: dict[str, Any], *, source: str = 'sync'):
        config_version = ConfigurationService._config_version(config_data, f'{source}-{config_type}')
        now = timezone.now()

        with transaction.atomic():
            existing = (
                MRAConfiguration.objects.select_for_update()
                .filter(
                    business=business,
                    config_type=config_type,
                    config_version=config_version,
                )
                .first()
            )

            active_configs = MRAConfiguration.objects.filter(
                business=business,
                config_type=config_type,
                is_active=True,
            )
            if existing:
                active_configs = active_configs.exclude(pk=existing.pk)
            active_configs.update(is_active=False, effective_to=now)

            if existing:
                existing.config_data = config_data
                existing.effective_to = None
                existing.fetched_from_mra_at = now
                existing.is_active = True
                existing.save(update_fields=['config_data', 'effective_to', 'fetched_from_mra_at', 'is_active'])
                return existing

            try:
                with transaction.atomic():
                    return MRAConfiguration.objects.create(
                        business=business,
                        config_type=config_type,
                        config_version=config_version,
                        config_data=config_data,
                        effective_from=now,
                        fetched_from_mra_at=now,
                        is_active=True,
                    )
            except IntegrityError:
                existing = MRAConfiguration.objects.get(
                    business=business,
                    config_type=config_type,
                    config_version=config_version,
                )
                existing.config_data = config_data
                existing.effective_to = None
                existing.fetched_from_mra_at = now
                existing.is_active = True
                existing.save(update_fields=['config_data', 'effective_to', 'fetched_from_mra_at', 'is_active'])
                return existing

    @staticmethod
    def store_configuration_response(
        business,
        response_data: dict[str, Any],
        *,
        source: str = 'sync',
        config_types: list[str] | None = None,
        branch=None,
    ) -> list[MRAConfiguration]:
        config_types = config_types or [
            'global_configuration',
            'terminal_configuration',
            'taxpayer_configuration',
            'tax_rules',
            'system_settings',
        ]
        stored: list[MRAConfiguration] = []
        for config_type in config_types:
            config_data = ConfigurationService._extract_config_data(response_data, config_type)
            stored.append(
                ConfigurationService._replace_active_config(
                    business,
                    config_type,
                    config_data,
                    source=source,
                )
            )
        try:
            EISBranchSyncService.sync_sites_from_payload(
                business,
                response_data,
                source=source,
                preferred_branch=branch,
            )
        except Exception as exc:
            logger.warning('Unable to mirror EIS sites from %s configuration: %s', source, exc)
        return stored

    @staticmethod
    def _normalize_config_key(key: Any) -> str:
        return ''.join(ch for ch in str(key or '').lower() if ch.isalnum())

    @staticmethod
    def _truthy_mra_flag(value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value == 1
        if isinstance(value, str):
            return value.strip().lower() in {'true', '1', 'yes', 'y'}
        return False

    @staticmethod
    def response_requests_latest_config(response_data: Any) -> bool:
        """
        MRA may return shouldDownloadLatestConfig at different nesting levels
        and some gateways serialize booleans as strings/numbers.
        """
        flag_keys = {
            'shoulddownloadlatestconfig',
            'downloadlatestconfig',
            'shouldsynclatestconfig',
            'synclatestconfig',
        }
        queue: list[Any] = [response_data]
        seen: set[int] = set()

        while queue:
            current = queue.pop(0)
            if current is None:
                continue

            if isinstance(current, str):
                stripped = current.strip()
                if stripped.startswith('{') or stripped.startswith('['):
                    try:
                        queue.append(json.loads(stripped))
                    except Exception:
                        pass
                continue

            if isinstance(current, list):
                queue.extend(current)
                continue

            if not isinstance(current, dict):
                continue

            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)

            for key, value in current.items():
                normalized_key = ConfigurationService._normalize_config_key(key)
                if normalized_key in flag_keys and ConfigurationService._truthy_mra_flag(value):
                    return True
                if isinstance(value, (dict, list, str)):
                    queue.append(value)

        return False

    @staticmethod
    def _extract_offline_limit_node(config_data: Any) -> dict[str, Any] | None:
        if not config_data:
            return None

        queue: list[Any] = [config_data]
        while queue:
            current = queue.pop(0)

            if isinstance(current, list):
                queue.extend(current)
                continue

            if not isinstance(current, dict):
                continue

            normalized_map = {
                ConfigurationService._normalize_config_key(key): value
                for key, value in current.items()
            }

            offline_node = normalized_map.get('offlinelimit') or normalized_map.get('offlinelimits')
            if isinstance(offline_node, dict):
                return offline_node

            age_keys = {
                'maxtransactionageinhours',
                'maxofflinetransactionageinhours',
                'maxtransactionage',
            }
            cumulative_keys = {
                'maxcummulativeamount',  # MRA docs spelling
                'maxcumulativeamount',
                'maxofflinecummulativeamount',
                'maxofflinecumulativeamount',
            }
            if age_keys.intersection(normalized_map.keys()) or cumulative_keys.intersection(normalized_map.keys()):
                return current

            for value in current.values():
                if isinstance(value, (dict, list)):
                    queue.append(value)

        return None

    @staticmethod
    def _to_positive_int(value: Any) -> int | None:
        if value in (None, ''):
            return None
        try:
            parsed = int(str(value).strip())
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_positive_decimal(value: Any) -> Decimal | None:
        if value in (None, ''):
            return None
        try:
            parsed = Decimal(str(value).strip())
            if not parsed.is_finite() or parsed <= 0:
                return None
            return parsed
        except (TypeError, ValueError, InvalidOperation):
            return None

    @staticmethod
    def get_offline_limits(business) -> OfflineLimitPolicy:
        """
        Resolve offline policy from the latest active MRA configuration.

        Supports the MRA naming variants observed in documentation:
        - maxTransactionAgeInHours
        - maxCummulativeAmount (doc spelling)
        - maxCumulativeAmount
        """
        if not business:
            return OfflineLimitPolicy()

        active_configs = list(
            MRAConfiguration.objects.filter(
                business=business,
                is_active=True,
            ).order_by('-effective_from')
        )
        if not active_configs:
            return OfflineLimitPolicy()

        # Prefer system settings when available, then scan the rest.
        prioritized = sorted(
            active_configs,
            key=lambda cfg: (0 if cfg.config_type == 'system_settings' else 1, -cfg.effective_from.timestamp()),
        )

        for config in prioritized:
            node = ConfigurationService._extract_offline_limit_node(config.config_data)
            if not isinstance(node, dict):
                continue

            normalized_map = {
                ConfigurationService._normalize_config_key(key): value
                for key, value in node.items()
            }
            max_age_hours = (
                ConfigurationService._to_positive_int(normalized_map.get('maxtransactionageinhours'))
                or ConfigurationService._to_positive_int(normalized_map.get('maxofflinetransactionageinhours'))
                or ConfigurationService._to_positive_int(normalized_map.get('maxtransactionage'))
            )
            max_cumulative_amount = (
                ConfigurationService._to_positive_decimal(normalized_map.get('maxcummulativeamount'))
                or ConfigurationService._to_positive_decimal(normalized_map.get('maxcumulativeamount'))
                or ConfigurationService._to_positive_decimal(normalized_map.get('maxofflinecummulativeamount'))
                or ConfigurationService._to_positive_decimal(normalized_map.get('maxofflinecumulativeamount'))
            )

            if max_age_hours is None and max_cumulative_amount is None:
                continue

            return OfflineLimitPolicy(
                max_transaction_age_hours=max_age_hours,
                max_cumulative_amount=max_cumulative_amount,
                source=f"{config.config_type}:{config.config_version}",
            )

        return OfflineLimitPolicy()

    @staticmethod
    def fetch_and_store_configuration(business, config_types=None, terminal: Terminal | None = None):
        if config_types is None:
            config_types = [
                'global_configuration',
                'terminal_configuration',
                'taxpayer_configuration',
                'tax_rules',
                'system_settings',
            ]

        sync_log = ConfigurationSyncLog.objects.create(
            business=business,
            status='pending',
            config_types=config_types,
            started_at=timezone.now(),
        )

        try:
            if terminal is None:
                terminal = (
                    Terminal.objects.filter(business=business)
                    .exclude(mra_token='')
                    .order_by('-updated_at')
                    .first()
                )
            client = MRAEISClient(terminal=terminal)
            result = client.call('get_latest_config', payload=None, method='POST', mutating=False)
            response_data = result.data or {}

            ConfigurationService.store_configuration_response(
                business=business,
                response_data=response_data,
                source='latest-config',
                config_types=config_types,
                branch=terminal.branch if terminal is not None else None,
            )

            sync_log.status = 'success'
            sync_log.completed_at = timezone.now()
            sync_log.save(update_fields=['status', 'completed_at'])
            return sync_log
        except Exception as exc:
            sync_log.status = 'failed'
            sync_log.error_message = str(exc)
            sync_log.completed_at = timezone.now()
            sync_log.save(update_fields=['status', 'error_message', 'completed_at'])
            raise

    SALES_CONFIGURATION_TYPES = {
        'global_configuration': 'globalConfiguration',
        'terminal_configuration': 'terminalConfiguration',
        'taxpayer_configuration': 'taxpayerConfiguration',
    }

    @staticmethod
    def _configuration_fetched_at(config) -> datetime | None:
        return config.fetched_from_mra_at or config.effective_from

    @staticmethod
    def _required_sales_configuration_status(business, cutoff) -> dict[str, Any]:
        required_types = ConfigurationService.SALES_CONFIGURATION_TYPES
        required_official_keys = set(required_types.values())
        latest_fetch = None
        fresh_types: set[str] = set()
        stale_types: set[str] = set()
        active_count = 0

        configs = MRAConfiguration.objects.filter(
            business=business,
            is_active=True,
            config_type__in=[*required_types.keys(), 'system_settings'],
        )
        for config in configs:
            if not config.is_current():
                continue
            active_count += 1
            fetched_at = ConfigurationService._configuration_fetched_at(config)
            if fetched_at and (latest_fetch is None or fetched_at > latest_fetch):
                latest_fetch = fetched_at
            is_fresh = bool(fetched_at and fetched_at >= cutoff)

            if config.config_type == 'system_settings':
                data = ConfigurationService._unwrap_response_data(config.config_data if isinstance(config.config_data, dict) else {})
                has_complete_bundle = all(isinstance(data.get(key), dict) for key in required_official_keys)
                if has_complete_bundle and is_fresh:
                    return {
                        'fresh': True,
                        'source': 'system_settings',
                        'latest_fetch_at': latest_fetch,
                        'missing_types': [],
                        'stale_types': [],
                        'active_count': active_count,
                    }
                if has_complete_bundle:
                    stale_types.update(required_types.keys())
                continue

            if config.config_type in required_types:
                if is_fresh:
                    fresh_types.add(config.config_type)
                else:
                    stale_types.add(config.config_type)

        missing_types = [config_type for config_type in required_types if config_type not in fresh_types]
        stale_missing_types = [config_type for config_type in missing_types if config_type in stale_types]
        return {
            'fresh': not missing_types,
            'source': 'split_configuration' if not missing_types else '',
            'latest_fetch_at': latest_fetch,
            'missing_types': missing_types,
            'stale_types': stale_missing_types,
            'active_count': active_count,
        }

    @staticmethod
    def _fresh_configuration_error_message(status: dict[str, Any]) -> str:
        missing_types = status.get('missing_types') or []
        stale_types = status.get('stale_types') or []
        details = []
        if missing_types:
            details.append(f"missing/stale required configs: {', '.join(missing_types)}")
        if stale_types:
            details.append(f"stale configs: {', '.join(stale_types)}")
        if not status.get('active_count'):
            details.append('no active MRA sales configuration found')
        suffix = f" ({'; '.join(details)})" if details else ''
        return f'MRA sales configuration is not fresh{suffix}'

    @staticmethod
    def ensure_fresh_configuration(
        business,
        *,
        terminal: Terminal | None = None,
        max_age_hours: int | None = None,
        require_success: bool | None = None,
    ) -> dict[str, Any]:
        """Refresh and verify the complete MRA sales config bundle before EIS operations."""
        max_age_hours = max_age_hours or int(getattr(settings, 'MRA_EIS_CONFIG_MAX_AGE_HOURS', 24) or 24)
        require_success = (
            bool(getattr(settings, 'MRA_EIS_REQUIRE_FRESH_CONFIG_FOR_SALES', True))
            if require_success is None
            else bool(require_success)
        )
        cutoff = timezone.now() - timedelta(hours=max_age_hours)
        current_status = ConfigurationService._required_sales_configuration_status(business, cutoff)
        latest_fetch = current_status.get('latest_fetch_at')
        if current_status.get('fresh'):
            return {
                'fresh': True,
                'refreshed': False,
                'source': current_status.get('source'),
                'latest_fetch_at': latest_fetch.isoformat() if latest_fetch else None,
                'max_age_hours': max_age_hours,
            }

        try:
            sync_log = ConfigurationService.fetch_and_store_configuration(
                business,
                terminal=terminal,
            )
            refreshed_status = ConfigurationService._required_sales_configuration_status(business, cutoff)
            refreshed_latest = refreshed_status.get('latest_fetch_at')
            if refreshed_status.get('fresh'):
                return {
                    'fresh': True,
                    'refreshed': True,
                    'source': refreshed_status.get('source'),
                    'status': sync_log.status,
                    'sync_log_id': str(sync_log.id),
                    'completed_at': sync_log.completed_at.isoformat() if sync_log.completed_at else None,
                    'latest_fetch_at': refreshed_latest.isoformat() if refreshed_latest else None,
                    'max_age_hours': max_age_hours,
                }

            error_message = ConfigurationService._fresh_configuration_error_message(refreshed_status)
            if require_success:
                raise MRAIntegrationError(error_message)
            logger.warning('MRA configuration refresh did not produce a fresh sales config for business %s: %s', business.id, error_message)
            return {
                'fresh': False,
                'refreshed': True,
                'status': sync_log.status,
                'sync_log_id': str(sync_log.id),
                'completed_at': sync_log.completed_at.isoformat() if sync_log.completed_at else None,
                'error': error_message,
                'missing_types': refreshed_status.get('missing_types') or [],
                'stale_types': refreshed_status.get('stale_types') or [],
                'max_age_hours': max_age_hours,
            }
        except Exception as exc:
            if require_success:
                raise MRAIntegrationError(f'MRA configuration refresh failed: {exc}') from exc
            logger.warning('MRA configuration refresh skipped/failed for business %s: %s', business.id, exc)
            return {
                'fresh': False,
                'refreshed': False,
                'error': str(exc),
                'missing_types': current_status.get('missing_types') or [],
                'stale_types': current_status.get('stale_types') or [],
                'max_age_hours': max_age_hours,
            }

    @staticmethod
    def get_active_configuration(business, config_type):
        configs = (
            MRAConfiguration.objects.filter(
                business=business,
                config_type=config_type,
                is_active=True,
            )
            .order_by('-effective_from')
        )

        for config in configs:
            if config.is_current():
                return config
        return None

    @staticmethod
    def get_official_configuration_bundle(business) -> dict[str, Any]:
        system_settings = ConfigurationService.get_active_configuration(business, 'system_settings')
        if system_settings and isinstance(system_settings.config_data, dict):
            data = system_settings.config_data
            return ConfigurationService._unwrap_response_data(data)

        bundle: dict[str, Any] = {}
        for config_type, official_key in [
            ('global_configuration', 'globalConfiguration'),
            ('terminal_configuration', 'terminalConfiguration'),
            ('taxpayer_configuration', 'taxpayerConfiguration'),
        ]:
            config = ConfigurationService.get_active_configuration(business, config_type)
            if config:
                bundle[official_key] = config.config_data
        return bundle

    @staticmethod
    def get_config_versions(business) -> dict[str, int]:
        bundle = ConfigurationService.get_official_configuration_bundle(business)

        def version_for(official_key: str) -> int:
            node = bundle.get(official_key)
            if not isinstance(node, dict):
                return 0
            try:
                return int(node.get('versionNo') or node.get('version') or 0)
            except (TypeError, ValueError):
                return 0

        return {
            'global': version_for('globalConfiguration'),
            'terminal': version_for('terminalConfiguration'),
            'taxpayer': version_for('taxpayerConfiguration'),
        }

    @staticmethod
    def is_taxpayer_vat_registered(business) -> bool:
        bundle = ConfigurationService.get_official_configuration_bundle(business)
        taxpayer_config = bundle.get('taxpayerConfiguration')
        if isinstance(taxpayer_config, dict) and 'isVATRegistered' in taxpayer_config:
            value = taxpayer_config.get('isVATRegistered')
            if isinstance(value, bool):
                return value
            return str(value or '').strip().lower() in {'true', '1', 'yes', 'y'}

        return bool(getattr(business, 'vat_registered', False)) or str(
            getattr(business, 'mra_taxpayer_type', '') or ''
        ).strip().upper() == 'VAT'

    @staticmethod
    def is_taxpayer_explicitly_non_vat(business) -> bool:
        bundle = ConfigurationService.get_official_configuration_bundle(business)
        taxpayer_config = bundle.get('taxpayerConfiguration')
        if isinstance(taxpayer_config, dict) and 'isVATRegistered' in taxpayer_config:
            value = taxpayer_config.get('isVATRegistered')
            if isinstance(value, bool):
                return not value
            return str(value or '').strip().lower() not in {'true', '1', 'yes', 'y'}

        taxpayer_type = str(getattr(business, 'mra_taxpayer_type', '') or '').strip().upper().replace('-', '_')
        return taxpayer_type in {'NON_VAT', 'NONVAT', 'NON_VAT_REGISTERED'}

    @staticmethod
    def get_taxpayer_tin(business) -> str:
        bundle = ConfigurationService.get_official_configuration_bundle(business)
        taxpayer_config = bundle.get('taxpayerConfiguration')
        if isinstance(taxpayer_config, dict):
            tin = str(taxpayer_config.get('tin') or '').strip()
            if tin:
                return tin

        return str(getattr(business, 'tin', '') or '').strip()

    @staticmethod
    def get_taxpayer_configuration(business) -> dict[str, Any]:
        bundle = ConfigurationService.get_official_configuration_bundle(business)
        taxpayer_config = bundle.get('taxpayerConfiguration')
        return taxpayer_config if isinstance(taxpayer_config, dict) else {}

    @staticmethod
    def get_activated_tax_rate_ids(business) -> list[str]:
        taxpayer_config = ConfigurationService.get_taxpayer_configuration(business)
        raw_ids = (
            taxpayer_config.get('activatedTaxRateIds')
            or taxpayer_config.get('activatedTaxrateIds')
            or taxpayer_config.get('activated_tax_rate_ids')
            or []
        )
        if not isinstance(raw_ids, list):
            return []
        return [str(rate_id).strip() for rate_id in raw_ids if str(rate_id or '').strip()]

    @staticmethod
    def _normalize_tax_rate_id(value: Any) -> str:
        return re.sub(r'[^A-Z0-9]', '', str(value or '').strip().upper())

    @staticmethod
    def _prefer_activated_tax_rate_id(business, preferred_ids: list[str]) -> str:
        activated_ids = ConfigurationService.get_activated_tax_rate_ids(business)
        if not activated_ids:
            return ''

        normalized_lookup = {
            ConfigurationService._normalize_tax_rate_id(rate_id): rate_id
            for rate_id in activated_ids
        }
        for preferred_id in preferred_ids:
            normalized = ConfigurationService._normalize_tax_rate_id(preferred_id)
            if normalized in normalized_lookup:
                return normalized_lookup[normalized]
        return ''

    @staticmethod
    def get_terminal_site_id(business, branch=None) -> str:
        if branch is not None:
            site_id = getattr(branch, 'mra_site_id', None)
            if site_id:
                return str(site_id)
            branch_code = getattr(branch, 'mra_branch_code', None)
            if branch_code:
                return str(branch_code)
        bundle = ConfigurationService.get_official_configuration_bundle(business)
        terminal_config = bundle.get('terminalConfiguration')
        if isinstance(terminal_config, dict):
            terminal_site = terminal_config.get('terminalSite')
            if isinstance(terminal_site, dict):
                site_id = terminal_site.get('siteId')
                if site_id not in (None, ''):
                    return str(site_id)
        return ''

    @staticmethod
    def resolve_tax_rate_id(business, tax_rate: Any, tax_category: str | None = None) -> str:
        try:
            normalized_rate = Decimal(str(tax_rate or 0)).quantize(Decimal('0.001'))
        except (InvalidOperation, TypeError, ValueError):
            normalized_rate = Decimal('0.000')

        category = str(tax_category or '').lower()
        is_zero_or_exempt = normalized_rate == Decimal('0.000') or category in {
            'zero',
            'vat_zero',
            'zero_rated',
            'exempt',
            'vat_exempt',
        }

        if is_zero_or_exempt and not ConfigurationService.is_taxpayer_vat_registered(business):
            taxpayer_rate_id = ConfigurationService._prefer_activated_tax_rate_id(
                business,
                ['NRT', 'NON_RATED', 'NONRATED', 'NON_VAT', 'NONVAT', 'ZERO', 'B', 'EXEMPT', 'E'],
            )
            if taxpayer_rate_id:
                return taxpayer_rate_id

        bundle = ConfigurationService.get_official_configuration_bundle(business)
        global_config = bundle.get('globalConfiguration')
        tax_rates = global_config.get('taxrates') if isinstance(global_config, dict) else []
        if isinstance(tax_rates, list):
            if category in {'zero', 'vat_zero', 'zero_rated', 'exempt', 'vat_exempt'}:
                preferred_ids = ['B'] if category in {'zero', 'vat_zero', 'zero_rated'} else ['E']
                preferred_words = (
                    ['zero', 'zero rated', 'zero-rated']
                    if category in {'zero', 'vat_zero', 'zero_rated'}
                    else ['exempt']
                )
                for tax_rate_node in tax_rates:
                    if not isinstance(tax_rate_node, dict):
                        continue
                    candidate_id = str(tax_rate_node.get('id') or '').strip()
                    candidate_name = str(tax_rate_node.get('name') or '').strip().lower()
                    try:
                        candidate_rate = Decimal(str(tax_rate_node.get('rate') or 0)).quantize(Decimal('0.001'))
                    except (InvalidOperation, TypeError, ValueError):
                        continue
                    if candidate_rate != Decimal('0.000'):
                        continue
                    if (
                        ConfigurationService._normalize_tax_rate_id(candidate_id)
                        in {ConfigurationService._normalize_tax_rate_id(rate_id) for rate_id in preferred_ids}
                    ) or any(word in candidate_name for word in preferred_words):
                        return candidate_id or preferred_ids[0]

            for tax_rate_node in tax_rates:
                if not isinstance(tax_rate_node, dict):
                    continue
                try:
                    candidate_rate = Decimal(str(tax_rate_node.get('rate') or 0)).quantize(Decimal('0.001'))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if candidate_rate == normalized_rate:
                    candidate_id = tax_rate_node.get('id')
                    if candidate_id not in (None, ''):
                        return str(candidate_id)

        if category in {'zero', 'vat_zero', 'zero_rated'}:
            return 'B'
        if category in {'exempt', 'vat_exempt'}:
            return 'E'
        return 'A'


class EISBranchSyncService:
    """Mirror MRA EIS sites into local Branch records for management workflows."""

    SITE_ID_KEYS = (
        'siteId',
        'siteID',
        'siteCode',
        'site_code',
        'siteIdentifier',
        'site_identifier',
        'site_id',
        'branchSiteId',
        'branch_site_id',
    )
    GENERIC_CONTEXT_SITE_ID_KEYS = ('id', 'identifier')
    SITE_CONTAINER_KEYS = ('terminalSite', 'terminal_site', 'site', 'branchSite')
    BRANCH_CONTEXT_KEYS = (
        'branch',
        'branches',
        'site',
        'sites',
        'terminalsite',
        'terminalsites',
        'terminal_site',
        'terminal_sites',
        'activebranches',
        'active_branches',
    )
    SITE_NAME_KEYS = (
        'siteName',
        'site_name',
        'branchName',
        'branch_name',
        'name',
        'locationName',
        'location_name',
    )
    ADDRESS_KEYS = (
        'siteAddress',
        'site_address',
        'branchAddress',
        'branch_address',
        'address',
        'physicalAddress',
        'physical_address',
        'location',
    )
    CITY_KEYS = (
        'city',
        'cityName',
        'city_name',
        'placeOfBusiness',
        'place_of_business',
        'district',
    )
    STATE_KEYS = (
        'state',
        'region',
        'regionName',
        'region_name',
        'province',
    )
    COUNTRY_KEYS = ('country', 'countryName', 'country_name')
    SITE_HINT_KEYS = SITE_ID_KEYS + (
        'siteName',
        'site_name',
        'siteAddress',
        'site_address',
        'branchName',
        'branch_name',
        'branchAddress',
        'branch_address',
        'locationName',
        'location_name',
        'physicalAddress',
        'physical_address',
    )
    TAX_RATE_FALSE_BRANCH_NAMES = {
        'exempt',
        'exempt rated',
        'standard',
        'standard rated',
        'zero',
        'zero rated',
        'zero-rated',
        'non rated',
        'non-rated',
        'not rated',
        'no rate',
    }

    @staticmethod
    def _first_text(node: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = node.get(key)
            if value not in (None, ''):
                return str(value).strip()
        return ''

    @staticmethod
    def _is_branch_context(key: str) -> bool:
        normalized = str(key or '').strip().replace('-', '').replace('_', '').lower()
        return normalized in {
            context.replace('-', '').replace('_', '').lower()
            for context in EISBranchSyncService.BRANCH_CONTEXT_KEYS
        }

    @staticmethod
    def _site_from_node(node: dict[str, Any], *, parent_key: str = '') -> dict[str, str] | None:
        if not isinstance(node, dict):
            return None

        site_node = None
        for key in EISBranchSyncService.SITE_CONTAINER_KEYS:
            candidate = node.get(key)
            if isinstance(candidate, dict):
                site_node = candidate
                break

        source = site_node if site_node is not None else node
        allow_generic_site_shape = site_node is not None or EISBranchSyncService._is_branch_context(parent_key)
        if site_node is None and not any(
            source.get(key) not in (None, '') for key in EISBranchSyncService.SITE_HINT_KEYS
        ) and not allow_generic_site_shape:
            return None

        site_id = EISBranchSyncService._first_text(source, EISBranchSyncService.SITE_ID_KEYS)
        if not site_id and allow_generic_site_shape:
            site_id = EISBranchSyncService._first_text(
                source,
                EISBranchSyncService.GENERIC_CONTEXT_SITE_ID_KEYS,
            )
        if not site_id:
            return None

        site_name = EISBranchSyncService._first_text(source, EISBranchSyncService.SITE_NAME_KEYS)
        address = EISBranchSyncService._first_text(source, EISBranchSyncService.ADDRESS_KEYS)
        city = EISBranchSyncService._first_text(source, EISBranchSyncService.CITY_KEYS)
        state = EISBranchSyncService._first_text(source, EISBranchSyncService.STATE_KEYS)
        country = EISBranchSyncService._first_text(source, EISBranchSyncService.COUNTRY_KEYS)

        if site_node is None and not any([site_name, address, city, state, country]):
            return None

        return {
            'site_id': site_id,
            'name': site_name or f'EIS Site {site_id[-8:]}',
            'address': address or city or site_name or f'EIS site {site_id}',
            'city': city or 'N/A',
            'state': state,
            'country': country or 'Malawi',
        }

    @staticmethod
    def _normalized_text(value: Any) -> str:
        return ' '.join(str(value or '').strip().lower().replace('_', ' ').replace('-', ' ').split())

    @staticmethod
    def _cleanup_tax_rate_false_branches(business, valid_site_ids: set[str]) -> int:
        from business.models import Branch

        cleaned = 0
        candidates = (
            Branch.objects.filter(business=business, is_active=True, is_dirty=False)
            .exclude(mra_branch_code__isnull=True)
            .exclude(mra_branch_code='')
        )

        for branch in candidates:
            branch_code = str(branch.mra_branch_code or '').strip()
            if branch_code in valid_site_ids:
                continue

            name = EISBranchSyncService._normalized_text(branch.name)
            address = EISBranchSyncService._normalized_text(branch.address)
            code = EISBranchSyncService._normalized_text(branch_code)

            looks_like_tax_rate = (
                name in EISBranchSyncService.TAX_RATE_FALSE_BRANCH_NAMES
                and (address in {'', name} or code in EISBranchSyncService.TAX_RATE_FALSE_BRANCH_NAMES)
            )
            if not looks_like_tax_rate:
                continue

            branch.is_active = False
            branch.save(update_fields=['is_active', 'updated_at'])
            cleaned += 1

        return cleaned

    @staticmethod
    def _collect_sites(value: Any) -> list[dict[str, str]]:
        found: dict[str, dict[str, str]] = {}
        queue: list[tuple[Any, str]] = [(value, '')]
        seen: set[int] = set()

        while queue:
            current, parent_key = queue.pop(0)
            if isinstance(current, str):
                stripped = current.strip()
                if stripped.startswith('{') or stripped.startswith('['):
                    try:
                        queue.append((json.loads(stripped), parent_key))
                    except Exception:
                        pass
                continue

            if isinstance(current, list):
                queue.extend((item, parent_key) for item in current)
                continue

            if not isinstance(current, dict):
                continue

            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)

            site = EISBranchSyncService._site_from_node(current, parent_key=parent_key)
            if site:
                existing = found.get(site['site_id'])
                if not existing:
                    found[site['site_id']] = site
                else:
                    found[site['site_id']] = {
                        'site_id': site['site_id'],
                        'name': existing.get('name') or site.get('name') or f"EIS Site {site['site_id'][-8:]}",
                        'address': existing.get('address') or site.get('address') or f"EIS site {site['site_id']}",
                        'city': existing.get('city') or site.get('city') or 'N/A',
                        'state': existing.get('state') or site.get('state') or '',
                        'country': existing.get('country') or site.get('country') or 'Malawi',
                    }

            queue.extend(
                (item, str(key))
                for key, item in current.items()
                if isinstance(item, (dict, list, str))
            )

        return list(found.values())

    @staticmethod
    @transaction.atomic
    def sync_sites_from_payload(
        business,
        payload: Any,
        *,
        source: str = 'mra',
        preferred_branch=None,
    ) -> dict[str, Any]:
        from business.models import Branch

        sites = EISBranchSyncService._collect_sites(payload)
        valid_site_ids = {site['site_id'] for site in sites}
        cleaned_false_branches = EISBranchSyncService._cleanup_tax_rate_false_branches(
            business,
            valid_site_ids,
        )
        created = 0
        updated = 0
        unchanged = 0
        branches: list[dict[str, Any]] = []

        for site in sites:
            site_id = site['site_id']
            branch = None
            if (
                preferred_branch is not None
                and len(sites) == 1
                and getattr(preferred_branch, 'business_id', None) == business.id
                and str(getattr(preferred_branch, 'mra_branch_code', '') or '').strip() in {'', site_id}
            ):
                branch = preferred_branch

            if branch is None:
                branch = Branch.objects.filter(business=business, mra_branch_code=site_id).first()
            if branch is None:
                branch = Branch.objects.filter(
                    business=business,
                    name__iexact=site['name'],
                ).filter(mra_branch_code__in=[None, '']).first()

            defaults = {
                'name': site['name'],
                'address': site['address'],
                'city': site['city'],
                'state': site['state'],
                'country': site['country'],
                'mra_branch_code': site_id,
                'mra_site_id': site_id,
                'mra_site_name': site['name'],
                'mra_device_location': site['address'],
                'eis_mapping_source': source,
                'eis_mapping_updated_at': timezone.now(),
                'is_active': True,
                'is_dirty': False,
            }

            if branch is None:
                branch = Branch.objects.create(business=business, **defaults)
                created += 1
            else:
                changed_fields: list[str] = []
                for field, value in defaults.items():
                    current_value = getattr(branch, field)
                    if str(current_value or '') != str(value or ''):
                        setattr(branch, field, value)
                        changed_fields.append(field)
                if changed_fields:
                    if 'name' in changed_fields:
                        changed_fields.append('slug')
                    branch.save(update_fields=changed_fields + ['updated_at'])
                    updated += 1
                else:
                    unchanged += 1

            branches.append(
                {
                    'id': str(branch.id),
                    'name': branch.name,
                    'address': branch.address,
                    'city': branch.city,
                    'state': branch.state,
                    'country': branch.country,
                    'mra_branch_code': branch.mra_branch_code,
                    'mra_site_id': getattr(branch, 'mra_site_id', '') or '',
                    'mra_site_name': getattr(branch, 'mra_site_name', '') or '',
                    'is_eis_warehouse': getattr(branch, 'is_eis_warehouse', False),
                    'source': source,
                }
            )

        return {
            'synced': True,
            'source': source,
            'created': created,
            'updated': updated,
            'unchanged': unchanged,
            'cleaned_false_branches': cleaned_false_branches,
            'count': len(branches),
            'branches': branches,
        }

    @staticmethod
    def sync_for_business(business, terminal: Terminal | None = None) -> dict[str, Any]:
        payloads: list[Any] = []
        if terminal is None:
            terminal = (
                Terminal.objects.filter(business=business)
                .exclude(mra_token='')
                .order_by('-updated_at')
                .first()
            )

        for config in MRAConfiguration.objects.filter(business=business, is_active=True):
            payloads.append(config.config_data)

        if terminal is not None:
            payloads.append(
                {
                    'terminalSite': {
                        'siteId': getattr(terminal.branch, 'mra_branch_code', '') or '',
                        'site_id': getattr(terminal.branch, 'mra_site_id', '') or getattr(terminal.branch, 'mra_branch_code', '') or '',
                        'siteName': getattr(terminal.branch, 'name', '') or '',
                        'site_name': getattr(terminal.branch, 'mra_site_name', '') or getattr(terminal.branch, 'name', '') or '',
                        'siteAddress': getattr(terminal.branch, 'address', '') or '',
                        'city': getattr(terminal.branch, 'city', '') or '',
                        'state': getattr(terminal.branch, 'state', '') or '',
                        'country': getattr(terminal.branch, 'country', '') or '',
                    }
                }
            )

        return EISBranchSyncService.sync_sites_from_payload(
            business,
            payloads,
            source='eis-config',
            preferred_branch=terminal.branch if terminal is not None else None,
        )


class ProductMappingService:
    """MRA product mapping helpers."""

    INITIAL_INVENTORY_FIELD_ALIASES = {
        'barCode': ('barCode', 'BarCode', 'barcode', 'Barcode'),
        'productName': ('productName', 'ProductName', 'product_name'),
        'productDescription': ('productDescription', 'ProductDescription', 'product_description'),
        'quantityInStock': ('quantityInStock', 'QuantityInStock', 'quantity_in_stock'),
        'unitPrice': ('unitPrice', 'UnitPrice', 'unit_price'),
        'costPrice': ('costPrice', 'CostPrice', 'cost_price'),
        'sellingPrice': ('sellingPrice', 'SellingPrice', 'selling_price'),
        'reorderLevel': ('reorderLevel', 'ReorderLevel', 'reorder_level'),
        'overQuantityStockLevel': (
            'overQuantityStockLevel',
            'OverQuantityStockLevel',
            'over_quantity_stock_level',
        ),
    }
    INITIAL_INVENTORY_IMPORT_FIELD_ALIASES = {
        **INITIAL_INVENTORY_FIELD_ALIASES,
        'inventoryItemId': ('inventoryItemId', 'InventoryItemId', 'inventory_item_id', 'inventory_item'),
        'category': ('category', 'Category', 'productCategory', 'ProductCategory', 'ProductDescription'),
        'sku': ('sku', 'SKU', 'Sku'),
        'unitMeasure': ('unitMeasure', 'UnitMeasure', 'mraUnitMeasure', 'MRAUnitMeasure', 'unit_type', 'unitType'),
        'mraProductCode': (
            'mraProductCode',
            'MRAProductCode',
            'mra_product_code',
            'ProductCode',
            'productCode',
            'productId',
            'ProductId',
            'barCode',
            'BarCode',
            'barcode',
        ),
        'mraProductName': (
            'mraProductName',
            'MRAProductName',
            'mra_product_name',
            'ProductName',
            'productName',
            'product_name',
        ),
        'mraTaxType': ('mraTaxType', 'MRATaxType', 'mra_tax_type', 'taxType', 'TaxType', 'tax_category'),
        'mraTaxRate': ('mraTaxRate', 'MRATaxRate', 'mra_tax_rate', 'taxRate', 'TaxRate', 'vatRate', 'VatRate'),
        'taxCalculationMethod': (
            'taxCalculationMethod',
            'TaxCalculationMethod',
            'tax_calculation_method',
            'calculationMethod',
            'CalculationMethod',
        ),
        'mraLevies': ('mraLevies', 'MRALevies', 'mra_levies', 'levies', 'Levies', 'productLevies'),
        'levies': ('levies', 'Levies', 'mraLevies', 'MRALevies', 'mra_levies', 'productLevies'),
        'isApproved': ('isApproved', 'IsApproved', 'is_approved', 'approved', 'approvalStatus', 'status'),
        'mraSynced': ('mraSynced', 'MRASynced', 'mra_synced', 'synced', 'isSynced', 'is_active', 'isActive'),
    }
    INITIAL_INVENTORY_REQUIRED_FIELDS = (
        'barCode',
        'productName',
        'productDescription',
        'quantityInStock',
        'unitPrice',
        'costPrice',
        'sellingPrice',
    )

    @staticmethod
    def create_product_mapping(
        business,
        inventory_item_id,
        product_name,
        mra_product_code,
        mra_product_name,
        tax_category,
        approved_price,
        tax_rate,
    ):
        """
        Compatibility wrapper for older callers.

        The active product mapping model now lives in inventory.MRAProductMapping
        because POS orders need a real InventoryItem relation and branch scope.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError
        from inventory.models import InventoryItem, MRAProductMapping as InventoryMRAProductMapping

        try:
            inventory_item = (
                InventoryItem.objects.filter(id=inventory_item_id, business=business).first()
            )
        except (DjangoValidationError, ValueError, TypeError):
            inventory_item = None
        if not inventory_item:
            raise ValueError(f'Inventory item {inventory_item_id} does not exist for this business')

        mapping, _ = InventoryMRAProductMapping.objects.update_or_create(
            inventory_item=inventory_item,
            defaults={
                'branch': inventory_item.branch,
                'mra_product_code': mra_product_code,
                'mra_product_name': mra_product_name or product_name or inventory_item.name,
                'mra_tax_type': tax_category,
                'mra_tax_rate': tax_rate,
                'mra_unit_measure': 'unit',
                'tax_calculation_method': 'inclusive',
                'is_approved': True,
                'approved_at': timezone.now(),
                'mra_synced': True,
                'last_synced_at': timezone.now(),
            },
        )
        return mapping

    @staticmethod
    def get_product_mapping(business, inventory_item_id):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from inventory.models import MRAProductMapping as InventoryMRAProductMapping

        try:
            return (
                InventoryMRAProductMapping.objects.select_related('inventory_item')
                .filter(
                    inventory_item_id=inventory_item_id,
                    inventory_item__business=business,
                    is_approved=True,
                    mra_synced=True,
                )
                .first()
            )
        except (DjangoValidationError, ValueError, TypeError):
            return None

    @staticmethod
    def validate_product_for_sale(business, inventory_item_id):
        mapping = ProductMappingService.get_product_mapping(business, inventory_item_id)
        if not mapping:
            raise ValueError(f'Product {inventory_item_id} is not MRA-approved for sale')
        return mapping

    @staticmethod
    def _initial_inventory_number(value, field_name: str) -> float:
        try:
            parsed = Decimal(str(value).replace(',', '').strip())
        except (InvalidOperation, AttributeError):
            raise ValueError(f'{field_name} must be a valid number')
        if parsed < 0:
            raise ValueError(f'{field_name} cannot be negative')
        return float(parsed)

    @staticmethod
    def _get_initial_inventory_value(product: dict[str, Any], official_field: str) -> Any:
        for candidate in ProductMappingService.INITIAL_INVENTORY_FIELD_ALIASES.get(official_field, (official_field,)):
            if candidate in product:
                return product.get(candidate)
        return None

    @staticmethod
    def _get_initial_inventory_import_value(product: dict[str, Any], field: str) -> Any:
        for candidate in ProductMappingService.INITIAL_INVENTORY_IMPORT_FIELD_ALIASES.get(field, (field,)):
            if candidate in product:
                return product.get(candidate)
        return None

    @staticmethod
    def _normalize_initial_inventory_product(product: dict[str, Any], index: int) -> dict[str, Any]:
        if not isinstance(product, dict):
            raise ValueError(f'Products[{index}] must be an object')

        normalized: dict[str, Any] = {}
        for field in ProductMappingService.INITIAL_INVENTORY_FIELD_ALIASES.keys():
            value = ProductMappingService._get_initial_inventory_value(product, field)
            if value is None or str(value).strip() == '':
                if field not in ProductMappingService.INITIAL_INVENTORY_REQUIRED_FIELDS:
                    normalized[field] = None
                    continue
                raise ValueError(f'Products[{index}].{field} is required')

            if field in {
                'quantityInStock',
                'unitPrice',
                'costPrice',
                'sellingPrice',
                'reorderLevel',
                'overQuantityStockLevel',
            }:
                normalized[field] = ProductMappingService._initial_inventory_number(value, f'Products[{index}].{field}')
            else:
                normalized[field] = str(value).strip()

        return normalized

    @staticmethod
    def _normalize_initial_inventory_import_product(product: dict[str, Any], index: int) -> dict[str, Any]:
        if not isinstance(product, dict):
            raise ValueError(f'Products[{index}] must be an object')

        normalized: dict[str, Any] = {}
        for field in ProductMappingService.INITIAL_INVENTORY_FIELD_ALIASES.keys():
            value = ProductMappingService._get_initial_inventory_import_value(product, field)
            if value is None or str(value).strip() == '':
                if field == 'productDescription':
                    value = ProductMappingService._get_initial_inventory_import_value(product, 'productName')
                elif field in {'reorderLevel', 'overQuantityStockLevel'}:
                    normalized[field] = None
                    continue
                else:
                    raise ValueError(f'Products[{index}].{field} is required')

            if field in {
                'quantityInStock',
                'unitPrice',
                'costPrice',
                'sellingPrice',
                'reorderLevel',
                'overQuantityStockLevel',
            }:
                normalized[field] = ProductMappingService._initial_inventory_number(value, f'Products[{index}].{field}')
            else:
                normalized[field] = str(value).strip()

        for field in [
            'inventoryItemId',
            'category',
            'sku',
            'unitMeasure',
            'mraProductCode',
            'mraProductName',
            'mraTaxType',
            'mraTaxRate',
            'taxCalculationMethod',
            'mraLevies',
            'levies',
            'isApproved',
            'mraSynced',
        ]:
            value = ProductMappingService._get_initial_inventory_import_value(product, field)
            if value is None or str(value).strip() == '':
                normalized[field] = None
            elif field == 'mraTaxRate':
                normalized[field] = ProductMappingService._initial_inventory_number(value, f'Products[{index}].{field}')
            elif field in {'mraLevies', 'levies'} and isinstance(value, (list, dict)):
                normalized[field] = value
            else:
                normalized[field] = str(value).strip()

        return normalized

    @staticmethod
    def _decimal_from_initial_inventory_number(value: Any, decimal_places: str) -> Decimal:
        try:
            parsed = Decimal(str(value or 0))
        except (InvalidOperation, TypeError, ValueError):
            parsed = Decimal('0')
        return parsed.quantize(Decimal(decimal_places))

    @staticmethod
    def _normalize_mapping_tax_type(value: Any) -> str:
        normalized = str(value or '').strip().lower()
        if normalized in {'zero', 'zero_rated', 'zero-rated', 'vat_zero', 'vat-zero', '0'}:
            return 'zero'
        if normalized in {'exempt', 'vat_exempt', 'vat-exempt'}:
            return 'exempt'
        return 'standard'

    @staticmethod
    def _normalize_mapping_tax_method(value: Any) -> str:
        normalized = str(value or '').strip().lower()
        return 'exclusive' if normalized.startswith('excl') else 'inclusive'

    @staticmethod
    def normalize_tax_for_taxpayer(
        business,
        tax_type: Any,
        tax_rate: Any,
        tax_calculation_method: Any = 'inclusive',
    ) -> tuple[str, Decimal, str, bool]:
        normalized_type = ProductMappingService._normalize_mapping_tax_type(tax_type)
        try:
            normalized_rate = Decimal(str(tax_rate or 0)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            normalized_rate = Decimal('0.00')
        normalized_method = ProductMappingService._normalize_mapping_tax_method(tax_calculation_method)

        if normalized_type in {'zero', 'exempt'}:
            return normalized_type, Decimal('0.00'), 'inclusive', False

        return normalized_type, normalized_rate, normalized_method, False

    @staticmethod
    def _truthy_mra_value(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float, Decimal)):
            return value != 0
        normalized = str(value or '').strip().lower()
        return normalized in {'approved', 'active', 'synced', 'true', '1', 'yes'}

    @staticmethod
    def _catalog_key(value: Any) -> str:
        return re.sub(r'\s+', '', str(value or '').strip()).upper()

    @staticmethod
    def _catalog_first(item: dict[str, Any], fields: list[str]) -> Any:
        for field in fields:
            if field in item and item.get(field) not in (None, ''):
                return item.get(field)
        return None

    @staticmethod
    def _levy_identity(node: dict[str, Any] | Any) -> str:
        if not isinstance(node, dict):
            return str(node or '').strip()
        return str(
            ProductMappingService._catalog_first(
                node,
                [
                    'levyTypeId',
                    'levy_type_id',
                    'levyId',
                    'levy_id',
                    'typeId',
                    'type_id',
                    'id',
                    'code',
                    'levyCode',
                    'levy_code',
                    'name',
                ],
            )
            or ''
        ).strip()

    @staticmethod
    def _levy_rate(node: dict[str, Any] | Any) -> Decimal | None:
        if not isinstance(node, dict):
            return None
        raw_rate = ProductMappingService._catalog_first(
            node,
            [
                'levyRate',
                'levy_rate',
                'rate',
                'levyPercentage',
                'levy_percentage',
                'percentage',
            ],
        )
        if raw_rate in (None, ''):
            return None
        try:
            return Decimal(str(raw_rate or 0)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def _configured_levy_lookup(business) -> dict[str, dict[str, Any]]:
        if not business:
            return {}
        bundle = ConfigurationService.get_official_configuration_bundle(business)
        taxpayer_config = bundle.get('taxpayerConfiguration') if isinstance(bundle, dict) else {}
        global_config = bundle.get('globalConfiguration') if isinstance(bundle, dict) else {}
        candidates: list[Any] = []
        if isinstance(taxpayer_config, dict):
            candidates.extend(
                [
                    taxpayer_config.get('activatedLevies'),
                    taxpayer_config.get('activated_levies'),
                    taxpayer_config.get('levies'),
                ]
            )
        if isinstance(global_config, dict):
            candidates.extend(
                [
                    global_config.get('levies'),
                    global_config.get('levyTypes'),
                    global_config.get('levy_types'),
                ]
            )

        lookup: dict[str, dict[str, Any]] = {}
        queue = [candidate for candidate in candidates if candidate not in (None, '')]
        while queue:
            current = queue.pop(0)
            if isinstance(current, list):
                queue.extend(current)
                continue
            if isinstance(current, dict):
                nested = ProductMappingService._catalog_first(
                    current,
                    ['activatedLevies', 'activated_levies', 'levies', 'levyTypes', 'levy_types', 'items', 'data'],
                )
                if isinstance(nested, (list, dict)):
                    queue.append(nested)
                levy_id = ProductMappingService._levy_identity(current)
                levy_rate = ProductMappingService._levy_rate(current)
                if levy_id:
                    lookup[levy_id.upper()] = {
                        'levyTypeId': levy_id,
                        'levyRate': float(levy_rate or Decimal('0.00')),
                    }
                continue
            levy_id = ProductMappingService._levy_identity(current)
            if levy_id:
                lookup[levy_id.upper()] = {'levyTypeId': levy_id, 'levyRate': 0.0}
        return lookup

    @staticmethod
    def normalize_levies(raw_levies: Any, business=None) -> list[dict[str, Any]]:
        if raw_levies in (None, ''):
            return []

        configured = ProductMappingService._configured_levy_lookup(business)
        queue: list[Any] = [raw_levies]
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        while queue:
            current = queue.pop(0)
            if current in (None, ''):
                continue
            if isinstance(current, list):
                queue.extend(current)
                continue
            if isinstance(current, dict):
                nested = ProductMappingService._catalog_first(
                    current,
                    [
                        'levies',
                        'activatedLevies',
                        'activated_levies',
                        'productLevies',
                        'product_levies',
                        'levyTypes',
                        'levy_types',
                        'levyBreakDown',
                        'levyBreakdown',
                        'items',
                        'data',
                    ],
                )
                if isinstance(nested, (list, dict)):
                    queue.append(nested)

                levy_id = ProductMappingService._levy_identity(current)
                if not levy_id:
                    continue
                configured_node = configured.get(levy_id.upper()) or {}
                levy_rate = ProductMappingService._levy_rate(current)
                if levy_rate is None:
                    levy_rate = ProductMappingService._levy_rate(configured_node)
                if levy_rate is None:
                    levy_rate = Decimal(str(configured_node.get('levyRate') or 0)).quantize(Decimal('0.01'))
            else:
                levy_id = ProductMappingService._levy_identity(current)
                if not levy_id:
                    continue
                configured_node = configured.get(levy_id.upper()) or {}
                levy_rate = ProductMappingService._levy_rate(configured_node)
                if levy_rate is None:
                    levy_rate = Decimal(str(configured_node.get('levyRate') or 0)).quantize(Decimal('0.01'))

            key = (levy_id.upper(), str(levy_rate))
            if key in seen:
                continue
            seen.add(key)
            normalized.append({'levyTypeId': levy_id, 'levyRate': float(levy_rate)})

        return normalized

    @staticmethod
    def get_activated_levies(business) -> list[dict[str, Any]]:
        taxpayer_config = ConfigurationService.get_taxpayer_configuration(business)
        if not isinstance(taxpayer_config, dict):
            return []
        raw_levies = (
            taxpayer_config.get('activatedLevies')
            or taxpayer_config.get('activated_levies')
            or taxpayer_config.get('levies')
            or []
        )
        return ProductMappingService.normalize_levies(raw_levies, business=business)

    @staticmethod
    def _tax_details_from_rate_id(business, rate_id: Any) -> tuple[str, Decimal]:
        normalized_id = str(rate_id or '').strip()
        if normalized_id.upper() in {'E', 'EXEMPT'}:
            return 'exempt', Decimal('0.00')
        if normalized_id.upper() in {'Z', 'ZERO', 'NRT', 'NON_RATED', 'NONRATED', 'NON-RATED'}:
            return 'zero', Decimal('0.00')

        bundle = ConfigurationService.get_official_configuration_bundle(business) if business else {}
        global_config = bundle.get('globalConfiguration')
        tax_rates = global_config.get('taxrates') if isinstance(global_config, dict) else []
        if isinstance(tax_rates, list):
            for tax_rate_node in tax_rates:
                if not isinstance(tax_rate_node, dict):
                    continue
                candidate_id = str(tax_rate_node.get('id') or tax_rate_node.get('rateId') or '').strip()
                if candidate_id and normalized_id and candidate_id.upper() != normalized_id.upper():
                    continue
                try:
                    rate = Decimal(str(tax_rate_node.get('rate') or 0)).quantize(Decimal('0.01'))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                return ('zero' if rate == Decimal('0.00') else 'standard'), rate

        return 'standard', Decimal('16.50')

    @staticmethod
    def _catalog_decimal(value: Any, decimal_places: str, fallback: Decimal | None = None) -> Decimal:
        if value in (None, ''):
            return (fallback if fallback is not None else Decimal('0')).quantize(Decimal(decimal_places))
        return ProductMappingService._decimal_from_initial_inventory_number(value, decimal_places)

    @staticmethod
    def _catalog_expiry_date(value: Any):
        raw_value = str(value or '').strip()
        if not raw_value:
            return None
        try:
            return datetime.fromisoformat(raw_value.replace('Z', '+00:00')).date()
        except ValueError:
            try:
                return datetime.strptime(raw_value[:10], '%Y-%m-%d').date()
            except ValueError:
                return None

    @staticmethod
    def _normalize_mra_catalog_product(item: dict[str, Any], business=None) -> dict[str, Any] | None:
        raw_code = str(ProductMappingService._catalog_first(item, [
            'code', 'productCode', 'product_code', 'mraProductCode', 'mra_product_code',
            'barCode', 'barcode', 'productId', 'product_id',
        ]) or '').strip()
        code = ProductMappingService._catalog_key(raw_code)
        if not code:
            return None

        name = str(ProductMappingService._catalog_first(item, [
            'name', 'productName', 'product_name', 'mraProductName', 'mra_product_name',
            'description', 'productDescription',
        ]) or code).strip()
        description = str(ProductMappingService._catalog_first(item, [
            'description', 'productDescription', 'product_description',
        ]) or '').strip()
        tax_rate_id = ProductMappingService._catalog_first(item, [
            'taxRateId', 'tax_rate_id', 'rateId', 'rate_id',
        ])
        tax_type = ProductMappingService._normalize_mapping_tax_type(ProductMappingService._catalog_first(item, [
            'default_tax_type', 'defaultTaxType', 'tax_type', 'taxType',
            'vat_type', 'vatType', 'vat_category', 'vatCategory',
        ]))
        raw_tax_rate = ProductMappingService._catalog_first(item, [
            'default_tax_rate', 'defaultTaxRate', 'tax_rate', 'taxRate', 'vat_rate', 'vatRate',
        ])
        if raw_tax_rate is None and tax_rate_id not in (None, ''):
            tax_type, tax_rate = ProductMappingService._tax_details_from_rate_id(business, tax_rate_id)
        else:
            tax_rate = ProductMappingService._decimal_from_initial_inventory_number(
                raw_tax_rate if raw_tax_rate is not None else (0 if tax_type in {'zero', 'exempt'} else 16.5),
                '0.01',
            )
        tax_method = ProductMappingService._normalize_mapping_tax_method(
            ProductMappingService._catalog_first(item, [
                'taxCalculationMethod', 'tax_calculation_method', 'calculationMethod',
            ])
        )
        tax_type, tax_rate, tax_method, tax_adjusted_for_non_vat = ProductMappingService.normalize_tax_for_taxpayer(
            business,
            tax_type,
            tax_rate,
            tax_method,
        )
        approved_raw = ProductMappingService._catalog_first(item, [
            'is_approved', 'isApproved', 'approved', 'isActive', 'active', 'approvalStatus', 'status',
        ])
        is_approved = True if approved_raw in (None, '') else ProductMappingService._truthy_mra_value(approved_raw)
        is_product_raw = ProductMappingService._catalog_first(item, ['isProduct', 'is_product'])
        is_product = True if is_product_raw in (None, '') else ProductMappingService._truthy_mra_value(is_product_raw)
        quantity = ProductMappingService._catalog_decimal(
            ProductMappingService._catalog_first(item, ['quantity', 'quantityInStock', 'stockQuantity', 'stock_units']),
            '0.001',
        )
        price = ProductMappingService._catalog_decimal(
            ProductMappingService._catalog_first(item, ['price', 'sellingPrice', 'unitPrice']),
            '0.01',
        )
        minimum_stock = ProductMappingService._catalog_decimal(
            ProductMappingService._catalog_first(item, ['minimumStockLevel', 'minimum_stock_level', 'reorderLevel']),
            '0.001',
        )
        levies = ProductMappingService.normalize_levies(
            ProductMappingService._catalog_first(
                item,
                [
                    'levies',
                    'activatedLevies',
                    'activated_levies',
                    'productLevies',
                    'product_levies',
                    'levyTypes',
                    'levy_types',
                    'levyBreakDown',
                    'levyBreakdown',
                ],
            ),
            business=business,
        )

        return {
            'code': code,
            'display_code': raw_code or code,
            'name': name,
            'description': description,
            'tax_type': tax_type,
            'tax_rate': tax_rate,
            'tax_rate_id': str(tax_rate_id or '').strip(),
            'tax_adjusted_for_non_vat': tax_adjusted_for_non_vat,
            'unit_measure': str(ProductMappingService._catalog_first(item, [
                'unit', 'unitMeasure', 'unit_measure', 'unitOfMeasure', 'mra_unit_measure',
            ]) or 'unit').strip() or 'unit',
            'tax_calculation_method': tax_method,
            'quantity': quantity,
            'price': price,
            'minimum_stock': minimum_stock,
            'expiry': ProductMappingService._catalog_expiry_date(
                ProductMappingService._catalog_first(item, ['productExpiryDate', 'expiry', 'expiryDate'])
            ),
            'site_id': str(ProductMappingService._catalog_first(item, ['siteId', 'site_id']) or '').strip(),
            'is_product': is_product,
            'is_approved': is_approved,
            'levies': levies,
            'raw': item,
        }

    @staticmethod
    def _catalog_sale_description(product: dict[str, Any] | None) -> str:
        if not isinstance(product, dict):
            return ''

        raw = product.get('raw') if isinstance(product.get('raw'), dict) else {}
        description = str(
            ProductMappingService._catalog_first(raw, [
                'saleDescription',
                'salesDescription',
                'invoiceDescription',
                'lineDescription',
                'description',
                'productDescription',
                'product_description',
            ])
            or product.get('description')
            or ''
        ).strip()
        unit_measure = str(
            ProductMappingService._catalog_first(raw, [
                'unitOfMeasure',
                'unitMeasure',
                'unit_measure',
                'mra_unit_measure',
                'uom',
                'unit',
            ])
            or product.get('unit_measure')
            or ''
        ).strip()

        if description:
            if unit_measure and '|' not in description:
                return f'{description} | {unit_measure}'
            return description

        return str(product.get('name') or '').strip()

    @staticmethod
    def _active_mra_catalog_index(business) -> dict[str, dict[str, Any]]:
        catalog_index: dict[str, dict[str, Any]] = {}

        for config_type in ['terminal_site_products', 'product_codes']:
            config = ConfigurationService.get_active_configuration(business, config_type)
            if not config or not config.config_data:
                continue

            queue: list[Any] = [config.config_data]
            while queue:
                current = queue.pop(0)
                if isinstance(current, list):
                    queue.extend(entry for entry in current if isinstance(entry, (dict, list)))
                    continue
                if not isinstance(current, dict):
                    continue

                normalized = ProductMappingService._normalize_mra_catalog_product(current, business=business)
                if normalized:
                    catalog_index.setdefault(normalized['code'], normalized)

                for value in current.values():
                    if isinstance(value, (dict, list)):
                        queue.append(value)

            if catalog_index:
                break

        return catalog_index

    @staticmethod
    def _inventory_status(quantity: Decimal, reorder_level: Decimal) -> str:
        if quantity <= 0:
            return 'Out of Stock'
        if reorder_level > 0 and quantity <= reorder_level:
            return 'Low Stock'
        return 'In Stock'

    @staticmethod
    def _find_initial_inventory_item_match(business, branch, product: dict[str, Any]):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from inventory.models import InventoryItem

        inventory_item_id = str(product.get('inventoryItemId') or '').strip()
        if inventory_item_id:
            try:
                item = InventoryItem.objects.filter(
                    id=inventory_item_id,
                    business=business,
                    branch=branch,
                ).first()
                if item:
                    return item, 'inventory_item_id'
            except (DjangoValidationError, ValueError, TypeError):
                pass

        code = str(product.get('barCode') or '').strip()
        sku = str(product.get('sku') or '').strip()
        name = str(product.get('productName') or '').strip()

        for field, value, reason in [
            ('barcode', code, 'barcode'),
            ('product_code', code, 'product_code'),
            ('sku', sku or code, 'sku'),
        ]:
            if not value:
                continue
            item = InventoryItem.objects.filter(
                business=business,
                branch=branch,
                **{field: value},
            ).first()
            if item:
                return item, reason

        if name:
            item = InventoryItem.objects.filter(
                business=business,
                branch=branch,
                name__iexact=name,
            ).first()
            if item:
                return item, 'name'

        return None, ''

    @staticmethod
    def _find_inventory_item_for_catalog_product(business, branch, product: dict[str, Any]):
        from inventory.models import InventoryItem

        display_code = str(product.get('display_code') or product.get('code') or '').strip()
        code_key = ProductMappingService._catalog_key(display_code)
        name = str(product.get('name') or '').strip()

        for field in ['product_code', 'barcode', 'sku']:
            for candidate in [display_code, code_key]:
                if not candidate:
                    continue
                item = InventoryItem.objects.filter(
                    business=business,
                    branch=branch,
                    **{field: candidate},
                ).first()
                if item:
                    return item, field

        if name:
            item = InventoryItem.objects.filter(
                business=business,
                branch=branch,
                name__iexact=name,
            ).first()
            if item:
                return item, 'name'

        return None, ''

    @staticmethod
    @transaction.atomic
    def pull_approved_products_to_inventory(
        *,
        business,
        terminal: Terminal,
        user=None,
        refresh_from_mra: bool = True,
    ) -> dict[str, Any]:
        """Pull MRA portal-approved terminal/site products into POS inventory."""
        if not terminal:
            raise ValueError('An active MRA terminal is required')
        if terminal.business_id != business.id:
            raise ValueError('Terminal does not belong to this business')
        if terminal.status != 'active':
            raise ValueError('Terminal must be active before pulling approved products')
        if not terminal.branch_id:
            raise ValueError('Terminal must be linked to a branch before pulling approved products')

        product_sync = None
        if refresh_from_mra:
            product_sync = ProductMappingService.sync_terminal_site_products(
                business=business,
                terminal=terminal,
            )

        from inventory.models import AuditLog, InventoryItem, MRAProductMapping as InventoryMRAProductMapping

        branch = terminal.branch
        now = timezone.now()
        approved_products = [
            product for product in ProductMappingService._active_mra_catalog_index(business).values()
            if product.get('is_approved')
        ]

        created_count = 0
        updated_count = 0
        mapping_created_count = 0
        mapping_updated_count = 0
        imported_items: list[dict[str, Any]] = []
        imported_mappings: list[dict[str, Any]] = []
        taxpayer_incompatible: list[dict[str, Any]] = []

        for product in approved_products:
            item, match_reason = ProductMappingService._find_inventory_item_for_catalog_product(
                business,
                branch,
                product,
            )
            was_created = item is None
            display_code = str(product.get('display_code') or product.get('code') or '').strip()[:100]
            quantity = product.get('quantity') or Decimal('0.000')
            reorder_level = product.get('minimum_stock') or Decimal('0.000')
            price = product.get('price') or Decimal('0.00')
            cost = getattr(item, 'cost', None) if item else None
            value = (quantity * (cost or Decimal('0.00'))).quantize(Decimal('0.01'))
            category = 'MRA Approved Products' if product.get('is_product', True) else 'MRA Approved Services'
            unit_measure = str(product.get('unit_measure') or 'unit').strip()[:50] or 'unit'

            item_defaults = {
                'business': business,
                'branch': branch,
                'name': str(product.get('name') or display_code).strip()[:255] or display_code,
                'category': category,
                'item_type': 'sellable',
                'stock_units': quantity,
                'unit_type': unit_measure,
                'reorder_level': reorder_level,
                'status': ProductMappingService._inventory_status(quantity, reorder_level),
                'price': price,
                'value': value,
                'is_variable_price': False,
                'barcode': display_code,
                'sku': display_code,
                'expiry': product.get('expiry'),
                'on_menu': True,
                'price_locked': True,
                'tax_locked': True,
                'is_dirty': False,
            }
            if cost is not None:
                item_defaults['cost'] = cost

            product_code_conflict = InventoryItem.objects.filter(product_code=display_code).exclude(
                id=getattr(item, 'id', None)
            ).exists()
            if not product_code_conflict:
                item_defaults['product_code'] = display_code

            if item:
                existing_same_name = InventoryItem.objects.filter(
                    business=business,
                    branch=branch,
                    name__iexact=item_defaults['name'],
                ).exclude(id=item.id).exists()
                if existing_same_name:
                    item_defaults.pop('name', None)
                for field, value_to_set in item_defaults.items():
                    setattr(item, field, value_to_set)
                item.save()
                updated_count += 1
            else:
                item = InventoryItem.objects.create(**item_defaults)
                created_count += 1

            mapping, mapping_created = InventoryMRAProductMapping.objects.update_or_create(
                inventory_item=item,
                defaults={
                    'branch': branch,
                    'mra_product_code': display_code,
                    'mra_product_name': ProductMappingService._catalog_sale_description(product) or str(product.get('name') or item.name).strip() or item.name,
                    'mra_tax_type': product.get('tax_type') or 'standard',
                    'mra_tax_rate': product.get('tax_rate') or Decimal('16.50'),
                    'mra_unit_measure': unit_measure[:20] or 'unit',
                    'tax_calculation_method': product.get('tax_calculation_method') or 'inclusive',
                    'mra_levies': product.get('levies') or [],
                    'is_product': bool(product.get('is_product', True)),
                    'is_approved': True,
                    'approved_at': getattr(item, 'mra_mapping', None).approved_at if hasattr(item, 'mra_mapping') and item.mra_mapping.approved_at else now,
                    'mra_synced': True,
                    'last_synced_at': now,
                },
            )
            if mapping_created:
                mapping_created_count += 1
            else:
                mapping_updated_count += 1

            compatibility_error = mapping.taxpayer_compatibility_error()
            is_taxpayer_compatible = not bool(compatibility_error)
            if compatibility_error:
                taxpayer_incompatible.append({
                    'inventory_item_id': str(item.id),
                    'name': item.name,
                    'mra_product_code': mapping.mra_product_code,
                    'mra_product_name': mapping.mra_product_name,
                    'mra_tax_type': mapping.mra_tax_type,
                    'mra_tax_rate': str(mapping.mra_tax_rate),
                    'error': compatibility_error,
                })

            imported_items.append({
                'id': str(item.id),
                'name': item.name,
                'branch': str(branch.id),
                'mra_product_code': mapping.mra_product_code,
                'stock_units': str(item.stock_units),
                'price': str(item.price or ''),
                'created': was_created,
                'match_reason': match_reason or 'created',
            })
            imported_mappings.append({
                'id': str(mapping.id),
                'inventory_item': str(item.id),
                'mra_product_code': mapping.mra_product_code,
                'mra_product_name': mapping.mra_product_name,
                'mra_tax_type': mapping.mra_tax_type,
                'mra_tax_rate': str(mapping.mra_tax_rate),
                'mra_levies': mapping.mra_levies or [],
                'is_product': mapping.is_product,
                'is_approved': mapping.is_approved,
                'mra_synced': mapping.mra_synced,
                'is_ready_for_sale': mapping.is_ready_for_sale(),
                'is_taxpayer_compatible': is_taxpayer_compatible,
                'taxpayer_compatibility_error': compatibility_error,
            })

            AuditLog.objects.create(
                business=business,
                branch=branch,
                user=user if getattr(user, 'is_authenticated', False) else None,
                action_type='MRA_SYNC',
                entity_type='InventoryItem',
                entity_id=str(item.id),
                details={
                    'action': 'pull_approved_products_to_inventory',
                    'match_reason': match_reason or 'created',
                    'mra_product_code': mapping.mra_product_code,
                    'mapping_id': str(mapping.id),
                    'source': 'terminal_site_products',
                    'is_taxpayer_compatible': is_taxpayer_compatible,
                    'taxpayer_compatibility_error': compatibility_error,
                },
                mra_related=True,
                mra_reference=mapping.mra_product_code,
            )

        terminal.last_sync_at = now
        terminal.save(update_fields=['last_sync_at', 'updated_at'])

        return {
            'pulled': True,
            'product_count': len(approved_products),
            'created': created_count,
            'updated': updated_count,
            'mappings_created': mapping_created_count,
            'mappings_updated': mapping_updated_count,
            'taxpayer_incompatible_count': len(taxpayer_incompatible),
            'taxpayer_incompatible': taxpayer_incompatible[:50],
            'branch_id': str(branch.id),
            'product_sync': product_sync,
            'inventory_items': imported_items,
            'mra_mappings': imported_mappings,
        }

    @staticmethod
    @transaction.atomic
    def import_initial_inventory_to_pos(
        *,
        business,
        terminal: Terminal,
        products: list[dict[str, Any]],
        user=None,
        mark_as_mra_synced: bool = False,
    ) -> dict[str, Any]:
        """
        Bring MRA initial-inventory rows into local POS inventory.

        Initial upload rows create local stock. Mappings become sale-ready only
        when confirmed by the MRA terminal/site product catalog, or when the
        source payload explicitly carries approved and synced MRA status.
        """
        if not terminal:
            raise ValueError('An active MRA terminal is required')
        if terminal.business_id != business.id:
            raise ValueError('Terminal does not belong to this business')
        if terminal.status != 'active':
            raise ValueError('Terminal must be active before importing MRA initial stock')
        if not terminal.branch_id:
            raise ValueError('Terminal must be linked to a branch before importing MRA initial stock')
        if not isinstance(products, list) or not products:
            raise ValueError('At least one product is required')

        from inventory.models import AuditLog, InventoryItem, MRAProductMapping as InventoryMRAProductMapping

        branch = terminal.branch
        now = timezone.now()
        created_count = 0
        updated_count = 0
        mapping_created_count = 0
        mapping_updated_count = 0
        sale_ready_count = 0
        pending_mapping_count = 0
        catalog_match_count = 0
        skipped_product_code_count = 0
        imported_items: list[dict[str, Any]] = []
        imported_mappings: list[dict[str, Any]] = []
        catalog_index = ProductMappingService._active_mra_catalog_index(business)

        for index, raw_product in enumerate(products):
            product = ProductMappingService._normalize_initial_inventory_import_product(raw_product, index)
            name = str(product['productName']).strip()
            barcode = str(product['barCode']).strip()
            quantity = ProductMappingService._decimal_from_initial_inventory_number(
                product['quantityInStock'],
                '0.001',
            )
            reorder_level = ProductMappingService._decimal_from_initial_inventory_number(
                product.get('reorderLevel') or 0,
                '0.001',
            )
            unit_price = ProductMappingService._decimal_from_initial_inventory_number(product['unitPrice'], '0.01')
            cost = ProductMappingService._decimal_from_initial_inventory_number(product['costPrice'], '0.01')
            price = ProductMappingService._decimal_from_initial_inventory_number(
                product.get('sellingPrice') or unit_price,
                '0.01',
            )
            value = (quantity * cost).quantize(Decimal('0.01'))
            category = str(product.get('category') or '').strip()
            if not category or category == str(product.get('productDescription') or '').strip():
                category = 'MRA Initial Stock'
            unit_measure = str(product.get('unitMeasure') or '').strip() or 'unit'
            sku = str(product.get('sku') or '').strip() or barcode

            item, match_reason = ProductMappingService._find_initial_inventory_item_match(
                business,
                branch,
                product,
            )
            was_created = item is None

            item_defaults = {
                'business': business,
                'branch': branch,
                'name': name,
                'category': category,
                'item_type': 'sellable',
                'stock_units': quantity,
                'unit_type': unit_measure,
                'reorder_level': reorder_level,
                'status': ProductMappingService._inventory_status(quantity, reorder_level),
                'cost': cost,
                'price': price,
                'value': value,
                'is_variable_price': False,
                'barcode': barcode,
                'sku': sku,
                'on_menu': True,
                'price_locked': True,
                'tax_locked': True,
                'is_dirty': False,
            }

            product_code_conflict = InventoryItem.objects.filter(product_code=barcode).exclude(
                id=getattr(item, 'id', None)
            ).exists()
            if not product_code_conflict:
                item_defaults['product_code'] = barcode
            else:
                skipped_product_code_count += 1

            if item:
                existing_same_name = InventoryItem.objects.filter(
                    business=business,
                    branch=branch,
                    name__iexact=name,
                ).exclude(id=item.id).exists()
                if existing_same_name:
                    item_defaults.pop('name', None)
                for field, value_to_set in item_defaults.items():
                    setattr(item, field, value_to_set)
                item.save()
                updated_count += 1
            else:
                item = InventoryItem.objects.create(**item_defaults)
                created_count += 1

            mapping_code = str(product.get('mraProductCode') or barcode).strip()
            catalog_product = (
                catalog_index.get(ProductMappingService._catalog_key(mapping_code))
                or catalog_index.get(ProductMappingService._catalog_key(barcode))
            )
            if catalog_product:
                catalog_match_count += 1

            tax_type = (
                catalog_product.get('tax_type')
                if catalog_product
                else ProductMappingService._normalize_mapping_tax_type(product.get('mraTaxType'))
            )
            tax_rate = (
                catalog_product.get('tax_rate')
                if catalog_product
                else ProductMappingService._decimal_from_initial_inventory_number(
                    product.get('mraTaxRate') if product.get('mraTaxRate') is not None else (0 if tax_type in {'zero', 'exempt'} else 16.5),
                    '0.01',
                )
            )
            mapping_code = str(
                (catalog_product or {}).get('display_code')
                or (catalog_product or {}).get('code')
                or product.get('mraProductCode')
                or barcode
            ).strip()
            mapping_name = str(
                ProductMappingService._catalog_sale_description(catalog_product)
                or (catalog_product or {}).get('name')
                or product.get('mraProductName')
                or name
            ).strip() or name
            mapping_unit_measure = str(
                (catalog_product or {}).get('unit_measure')
                or unit_measure
            ).strip() or 'unit'
            mapping_tax_method = (
                (catalog_product or {}).get('tax_calculation_method')
                or ProductMappingService._normalize_mapping_tax_method(product.get('taxCalculationMethod'))
            )
            mapping_levies = (
                (catalog_product or {}).get('levies')
                or ProductMappingService.normalize_levies(product.get('mraLevies'), business=business)
                or ProductMappingService.normalize_levies(product.get('levies'), business=business)
                or []
            )
            row_approved_and_synced = (
                bool(mark_as_mra_synced)
                and ProductMappingService._truthy_mra_value(product.get('isApproved'))
                and ProductMappingService._truthy_mra_value(product.get('mraSynced'))
            )
            mapping_is_sale_ready = bool(
                (catalog_product and catalog_product.get('is_approved'))
                or row_approved_and_synced
            )
            if mapping_is_sale_ready:
                sale_ready_count += 1
            else:
                pending_mapping_count += 1
            mapping_defaults = {
                'branch': branch,
                'mra_product_code': mapping_code,
                'mra_product_name': mapping_name,
                'mra_tax_type': tax_type,
                'mra_tax_rate': tax_rate,
                'mra_unit_measure': mapping_unit_measure,
                'tax_calculation_method': mapping_tax_method,
                'mra_levies': mapping_levies,
                'is_product': bool((catalog_product or {}).get('is_product', True)),
                'is_approved': mapping_is_sale_ready,
                'approved_at': now if mapping_is_sale_ready else None,
                'mra_synced': mapping_is_sale_ready,
                'last_synced_at': now if mapping_is_sale_ready else None,
            }
            mapping, mapping_created = InventoryMRAProductMapping.objects.update_or_create(
                inventory_item=item,
                defaults=mapping_defaults,
            )
            if mapping_created:
                mapping_created_count += 1
            else:
                mapping_updated_count += 1

            compatibility_error = mapping.taxpayer_compatibility_error()
            is_taxpayer_compatible = not bool(compatibility_error)
            if compatibility_error:
                taxpayer_incompatible.append({
                    'inventory_item_id': str(item.id),
                    'name': item.name,
                    'mra_product_code': mapping.mra_product_code,
                    'mra_product_name': mapping.mra_product_name,
                    'mra_tax_type': mapping.mra_tax_type,
                    'mra_tax_rate': str(mapping.mra_tax_rate),
                    'error': compatibility_error,
                })

            imported_items.append({
                'id': str(item.id),
                'name': item.name,
                'branch': str(branch.id),
                'barcode': item.barcode,
                'stock_units': str(item.stock_units),
                'price': str(item.price or ''),
                'created': was_created,
                'match_reason': match_reason,
            })
            imported_mappings.append({
                'id': str(mapping.id),
                'inventory_item': str(item.id),
                'mra_product_code': mapping.mra_product_code,
                'mra_product_name': mapping.mra_product_name,
                'mra_levies': mapping.mra_levies or [],
                'is_product': mapping.is_product,
                'is_approved': mapping.is_approved,
                'mra_synced': mapping.mra_synced,
                'approval_source': 'mra_catalog' if catalog_product else ('approved_import_source' if row_approved_and_synced else 'pending_mra_approval'),
            })

            AuditLog.objects.create(
                business=business,
                branch=branch,
                user=user if getattr(user, 'is_authenticated', False) else None,
                action_type='MRA_SYNC',
                entity_type='InventoryItem',
                entity_id=str(item.id),
                details={
                    'action': 'import_initial_inventory_to_pos',
                    'mra_product_code': mapping.mra_product_code,
                    'match_reason': match_reason or 'created',
                    'mapping_id': str(mapping.id),
                },
                mra_related=True,
                mra_reference=mapping.mra_product_code,
            )

        terminal.last_sync_at = now
        terminal.save(update_fields=['last_sync_at', 'updated_at'])

        return {
            'imported': True,
            'product_count': len(products),
            'created': created_count,
            'updated': updated_count,
            'mappings_created': mapping_created_count,
            'mappings_updated': mapping_updated_count,
            'mappings_sale_ready': sale_ready_count,
            'mappings_pending': pending_mapping_count,
            'mra_catalog_matches': catalog_match_count,
            'skipped_product_codes': skipped_product_code_count,
            'branch_id': str(branch.id),
            'mark_as_mra_synced': bool(mark_as_mra_synced),
            'inventory_items': imported_items,
            'mra_mappings': imported_mappings,
        }

    @staticmethod
    def build_initial_inventory_payload(
        *,
        tin: str,
        products: list[dict[str, Any]],
        is_last_batch: bool,
    ) -> dict[str, Any]:
        tin = str(tin or '').strip()
        if not tin:
            raise ValueError('TIN is required')
        if not isinstance(products, list) or not products:
            raise ValueError('At least one product is required')

        return {
            'tin': tin,
            'isLastBatch': bool(is_last_batch),
            'products': [
                ProductMappingService._normalize_initial_inventory_product(product, index)
                for index, product in enumerate(products)
            ],
        }

    @staticmethod
    def _initial_inventory_batch_size() -> int:
        try:
            batch_size = int(getattr(settings, 'MRA_EIS_INITIAL_INVENTORY_BATCH_SIZE', 50) or 50)
        except (TypeError, ValueError):
            batch_size = 50
        return min(max(batch_size, 1), 50)

    @staticmethod
    def submit_initial_inventory(
        *,
        business,
        terminal: Terminal,
        tin: str,
        products: list[dict[str, Any]],
        is_last_batch: bool = False,
    ) -> dict[str, Any]:
        if not terminal:
            raise ValueError('An active MRA terminal is required')
        if terminal.business_id != business.id:
            raise ValueError('Terminal does not belong to this business')
        if terminal.status != 'active':
            raise ValueError('Terminal must be active before submitting initial inventory')
        if not terminal.mra_token:
            raise ValueError('Terminal token is missing; refresh or reactivate the terminal')
        if not isinstance(products, list) or not products:
            raise ValueError('At least one product is required')

        batch_size = ProductMappingService._initial_inventory_batch_size()
        product_batches = [
            products[index:index + batch_size]
            for index in range(0, len(products), batch_size)
        ]
        client = MRAEISClient(terminal=terminal)
        batch_results: list[dict[str, Any]] = []
        final_result: MRACallResult | None = None
        final_response_data: dict[str, Any] = {}

        for batch_index, product_batch in enumerate(product_batches, start=1):
            batch_is_last = bool(is_last_batch and batch_index == len(product_batches))
            payload = ProductMappingService.build_initial_inventory_payload(
                tin=tin,
                products=product_batch,
                is_last_batch=batch_is_last,
            )
            result = client.call(
                'initial_inventory_upload',
                payload=payload,
                method='POST',
                mutating=True,
            )
            response_data = result.data or {}
            batch_results.append({
                'batch_number': batch_index,
                'batch_count': len(product_batches),
                'product_count': len(payload['products']),
                'is_last_batch': batch_is_last,
                'dry_run': result.dry_run,
                'endpoint': result.endpoint,
                'status_code': result.status_code,
                'response': response_data,
                'remark': response_data.get('remark') if isinstance(response_data, dict) else None,
                'data': response_data.get('data') if isinstance(response_data, dict) else None,
            })
            final_result = result
            final_response_data = response_data

        terminal.last_sync_at = timezone.now()
        terminal.save(update_fields=['last_sync_at', 'updated_at'])

        return {
            'submitted': True,
            'dry_run': any(batch.get('dry_run') for batch in batch_results),
            'endpoint': final_result.endpoint if final_result else '',
            'status_code': final_result.status_code if final_result else 0,
            'is_last_batch': bool(is_last_batch),
            'product_count': len(products),
            'batch_size': batch_size,
            'batch_count': len(batch_results),
            'batches': batch_results,
            'response': final_response_data,
            'remark': final_response_data.get('remark') if isinstance(final_response_data, dict) else None,
            'data': final_response_data.get('data') if isinstance(final_response_data, dict) else None,
        }

    @staticmethod
    @transaction.atomic
    def sync_inventory_mapping_to_mra(inventory_mapping, terminal: Terminal | None = None) -> dict[str, Any]:
        """
        Verify an approved local mapping against MRA product status.

        The public MRA guide treats products/services as centrally managed in
        the EIS portal. POS software should sync approved site products down
        and can use product-status to validate the mapped code/stock level.
        """
        business = inventory_mapping.inventory_item.business
        payload = {
            'productId': inventory_mapping.mra_product_code,
            'tin': business.tin or '',
        }

        client = MRAEISClient(terminal=terminal)
        result = client.call('product_status', payload=payload, method='POST', mutating=False)

        inventory_mapping.mra_synced = True
        inventory_mapping.last_synced_at = timezone.now()
        inventory_mapping.save(update_fields=['mra_synced', 'last_synced_at', 'updated_at'])

        return {
            'mapping_id': str(inventory_mapping.id),
            'mra_product_code': inventory_mapping.mra_product_code,
            'synced': True,
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'response': result.data,
        }

    @staticmethod
    def sync_terminal_site_products(business, terminal: Terminal | None = None) -> dict[str, Any]:
        """
        Download approved products/services for the terminal site and store them
        as the active product catalog.
        """
        if terminal is None:
            terminal = (
                Terminal.objects.filter(business=business)
                .exclude(mra_token='')
                .order_by('-updated_at')
                .first()
            )
        site_id = ConfigurationService.get_terminal_site_id(
            business,
            terminal.branch if terminal else None,
        )
        payload = {
            'tin': business.tin or '',
            'siteId': site_id,
        }

        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'get_terminal_site_products',
            payload=payload,
            method='POST',
            mutating=False,
        )

        response_data = result.data or {}
        data = ConfigurationService._unwrap_response_data(response_data)
        product_data = data if data else response_data
        config_payload = product_data if isinstance(product_data, dict) else {'items': product_data}
        ConfigurationService._replace_active_config(
            business,
            'product_codes',
            config_payload,
            source='terminal-site-products',
        )
        ConfigurationService._replace_active_config(
            business,
            'terminal_site_products',
            config_payload,
            source='terminal-site-products',
        )
        branch_sync = EISBranchSyncService.sync_sites_from_payload(
            business,
            config_payload,
            source='terminal-site-products',
            preferred_branch=terminal.branch if terminal is not None else None,
        )

        return {
            'synced': True,
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'request_payload': payload,
            'branch_sync': branch_sync,
            'response': result.data,
        }

    @staticmethod
    def _require_active_terminal_for_product_operation(business, terminal: Terminal, action: str) -> None:
        if not terminal:
            raise ValueError(f'An active MRA terminal is required before {action}')
        if terminal.business_id != business.id:
            raise ValueError('Terminal does not belong to this business')
        if terminal.status != 'active':
            raise ValueError(f'Terminal must be active before {action}')
        if not terminal.mra_token:
            raise ValueError('Terminal token is missing; refresh or reactivate the terminal')

    @staticmethod
    def _normalize_add_product_payload(product: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(product, dict):
            raise ValueError('Product payload is required')

        payload = {
            'barcode': str(
                product.get('barcode')
                or product.get('barCode')
                or product.get('productCode')
                or product.get('product_code')
                or ''
            ).strip() or None,
            'hsCode': str(product.get('hsCode') or product.get('hs_code') or '').strip(),
            'name': str(product.get('name') or product.get('productName') or product.get('product_name') or '').strip(),
            'description': str(product.get('description') or product.get('productDescription') or product.get('product_description') or '').strip(),
            'uom': str(product.get('uom') or product.get('unitOfMeasure') or product.get('unit_of_measure') or '').strip(),
        }

        missing = [field for field in ['hsCode', 'name', 'description', 'uom'] if not payload[field]]
        if missing:
            raise ValueError(f'Missing required MRA product field(s): {", ".join(missing)}')
        if payload['barcode'] and len(payload['barcode']) < 4:
            raise ValueError('Barcode must be at least 4 characters when provided')
        return payload

    @staticmethod
    def add_product_to_mra(*, business, terminal: Terminal, product: dict[str, Any]) -> dict[str, Any]:
        """
        Create a product in MRA EIS using the official add-product stock endpoint.

        Swagger says this creates the master product and zero-quantity warehouse
        inventory for the taxpayer. We still expect the POS to pull approved/site
        products back from MRA before using the product in sales.
        """
        ProductMappingService._require_active_terminal_for_product_operation(
            business,
            terminal,
            'creating MRA products',
        )
        payload = ProductMappingService._normalize_add_product_payload(product)

        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'add_product',
            payload=payload,
            method='POST',
            mutating=True,
        )
        response_data = result.data or {}

        terminal.last_sync_at = timezone.now()
        terminal.save(update_fields=['last_sync_at', 'updated_at'])

        return {
            'submitted': True,
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'status_code': result.status_code,
            'payload': payload,
            'response': response_data,
            'data': response_data.get('data') if isinstance(response_data, dict) else None,
            'remark': response_data.get('remark') if isinstance(response_data, dict) else None,
            'requires_pull_from_mra': True,
        }

    @staticmethod
    def fetch_hs_codes(*, business, terminal: Terminal) -> dict[str, Any]:
        ProductMappingService._require_active_terminal_for_product_operation(
            business,
            terminal,
            'fetching MRA HS codes',
        )
        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'get_hs_codes',
            payload=None,
            method='GET',
            mutating=False,
            send_json=False,
        )
        response_data = result.data or {}
        return {
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'status_code': result.status_code,
            'response': response_data,
            'data': response_data.get('data') if isinstance(response_data, dict) else None,
            'remark': response_data.get('remark') if isinstance(response_data, dict) else None,
        }

    @staticmethod
    def fetch_units_of_measure(*, business, terminal: Terminal) -> dict[str, Any]:
        ProductMappingService._require_active_terminal_for_product_operation(
            business,
            terminal,
            'fetching MRA units of measure',
        )
        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'get_units_of_measure',
            payload=None,
            method='GET',
            mutating=False,
            send_json=False,
        )
        response_data = result.data or {}
        return {
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'status_code': result.status_code,
            'response': response_data,
            'data': response_data.get('data') if isinstance(response_data, dict) else None,
            'remark': response_data.get('remark') if isinstance(response_data, dict) else None,
        }

    @staticmethod
    def _extract_warehouse_stocks(response_data: dict[str, Any]) -> list[dict[str, Any]]:
        data = ConfigurationService._unwrap_response_data(response_data if isinstance(response_data, dict) else {})
        stocks = data.get('stocks') if isinstance(data, dict) else None
        if isinstance(stocks, list):
            return [stock for stock in stocks if isinstance(stock, dict)]
        if isinstance(data, list):
            return [stock for stock in data if isinstance(stock, dict)]
        return []

    @staticmethod
    def fetch_warehouse_inventory(
        *,
        business,
        terminal: Terminal | None = None,
        page_size: int = 200,
        max_pages: int = 25,
    ) -> dict[str, Any]:
        if terminal is None:
            terminal = (
                Terminal.objects.filter(business=business)
                .exclude(mra_token='')
                .order_by('-updated_at')
                .first()
            )
        client = MRAEISClient(terminal=terminal)
        all_stocks: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        dry_run = False
        endpoint = ''

        for page in range(1, max_pages + 1):
            result = client.call(
                'warehouse_inventory',
                payload=None,
                method='GET',
                mutating=False,
                params={'page': page, 'pageSize': page_size},
            )
            dry_run = bool(result.dry_run)
            endpoint = result.endpoint
            response_data = result.data or {}
            responses.append(response_data)
            stocks = ProductMappingService._extract_warehouse_stocks(response_data)
            all_stocks.extend(stocks)

            if dry_run:
                break

            data = ConfigurationService._unwrap_response_data(response_data)
            total = data.get('total') if isinstance(data, dict) else None
            response_page_size = data.get('pageSize') if isinstance(data, dict) else page_size
            try:
                total = int(total or 0)
                response_page_size = int(response_page_size or page_size)
            except (TypeError, ValueError):
                total = 0
                response_page_size = page_size
            if not stocks or (total and page * response_page_size >= total):
                break

        return {
            'dry_run': dry_run,
            'endpoint': endpoint,
            'stock_count': len(all_stocks),
            'stocks': all_stocks,
            'responses': responses,
        }

    @staticmethod
    def _normalize_inventory_transfer_items(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            raise ValueError('Transfer items must be a list.')

        normalized_items: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f'Transfer item {index} must be an object.')

            barcode = str(
                item.get('barcode')
                or item.get('barCode')
                or item.get('productCode')
                or item.get('product_code')
                or ''
            ).strip()
            if not barcode:
                raise ValueError(f'Transfer item {index} is missing barcode.')

            quantity = ProductMappingService._decimal_from_initial_inventory_number(
                item.get('quantity'),
                '0.001',
            )
            if quantity <= 0:
                raise ValueError(f'Transfer item {index} quantity must be greater than zero.')

            payload_item: dict[str, Any] = {
                'barcode': barcode,
                'quantity': float(quantity),
            }

            price_value = item.get('price')
            if price_value not in (None, ''):
                price = ProductMappingService._decimal_from_initial_inventory_number(price_value, '0.01')
                if price < 0:
                    raise ValueError(f'Transfer item {index} price cannot be negative.')
                payload_item['price'] = float(price)

            normalized_items.append(payload_item)

        if not normalized_items:
            raise ValueError('At least one transfer item is required.')

        return normalized_items

    @staticmethod
    def transfer_inventory(
        *,
        business,
        terminal: Terminal,
        items: Any,
        to_branch=None,
        to_site_id: str = '',
        from_site_id: str = '',
        from_warehouse_to_site: bool = True,
    ) -> dict[str, Any]:
        """
        Submit an official MRA inventory transfer.

        Swagger supports bulk transfer with one transfer type per request:
        Warehouse->Site or Site->Site. This method defaults to Warehouse->Site
        for the inventory warehouse screen.
        """
        normalized_items = ProductMappingService._normalize_inventory_transfer_items(items)
        source_site_id = str(from_site_id or '').strip()
        target_site_id = str(to_site_id or '').strip()
        if not target_site_id and to_branch is not None:
            target_site_id = str(
                getattr(to_branch, 'mra_site_id', '')
                or getattr(to_branch, 'mra_branch_code', '')
                or ''
            ).strip()
        if not target_site_id and to_branch is not None:
            target_site_id = ConfigurationService.get_terminal_site_id(business, to_branch)
        if not target_site_id:
            raise ValueError('Destination MRA site ID is required for inventory transfer.')
        if not bool(from_warehouse_to_site) and not source_site_id:
            raise ValueError('Source MRA site ID is required for branch/site inventory transfer.')

        payload: dict[str, Any] = {
            'fromWarehouseToSite': bool(from_warehouse_to_site),
            'fromSiteId': None if bool(from_warehouse_to_site) else source_site_id,
            'toSiteId': target_site_id,
            'items': normalized_items,
        }

        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'transfer_inventory',
            payload=payload,
            method='POST',
            mutating=True,
        )
        response_data = result.data or {}
        response_errors = _extract_mra_response_errors(response_data)
        if response_errors and not result.dry_run:
            raise MRAIntegrationError(
                f"MRA rejected inventory transfer: {'; '.join(response_errors)}",
                status_code=result.status_code,
                endpoint=result.endpoint,
                endpoint_key='transfer_inventory',
                response_data=response_data,
            )

        terminal.last_sync_at = timezone.now()
        terminal.save(update_fields=['last_sync_at', 'updated_at'])

        return {
            'submitted': True,
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'status_code': result.status_code,
            'payload': payload,
            'response': response_data,
            'errors': response_errors,
        }

    @staticmethod
    def reconcile_inventory_with_eis(
        *,
        business,
        terminal: Terminal | None = None,
        branch=None,
        quantity_tolerance: Decimal = Decimal('0.001'),
    ) -> dict[str, Any]:
        from inventory.models import InventoryItem, MRAProductMapping as InventoryMRAProductMapping

        if terminal is None:
            terminal = (
                Terminal.objects.filter(business=business)
                .exclude(mra_token='')
                .order_by('-updated_at')
                .first()
            )
        if branch is None and terminal is not None:
            branch = terminal.branch

        warehouse = ProductMappingService.fetch_warehouse_inventory(
            business=business,
            terminal=terminal,
        )
        remote_by_code: dict[str, dict[str, Any]] = {}
        for stock in warehouse.get('stocks') or []:
            code = ProductMappingService._catalog_key(
                stock.get('barcode') or stock.get('barCode') or stock.get('productCode')
            )
            if code:
                remote_by_code[code] = stock

        queryset = InventoryMRAProductMapping.objects.select_related('inventory_item').filter(
            inventory_item__business=business,
            is_approved=True,
            mra_synced=True,
        )
        if branch is not None:
            queryset = queryset.filter(inventory_item__branch=branch)

        matched = []
        quantity_mismatches = []
        missing_in_eis = []
        local_codes = set()
        for mapping in queryset:
            item: InventoryItem = mapping.inventory_item
            code = ProductMappingService._catalog_key(mapping.mra_product_code or item.barcode or item.product_code)
            if not code:
                continue
            local_codes.add(code)
            local_quantity = ProductMappingService._decimal_from_initial_inventory_number(
                getattr(item, 'stock_units', 0),
                '0.001',
            )
            remote = remote_by_code.get(code)
            row = {
                'inventory_item_id': str(item.id),
                'name': item.name,
                'mra_product_code': mapping.mra_product_code,
                'local_quantity': str(local_quantity),
                'branch_id': str(getattr(item, 'branch_id', '') or ''),
            }
            if not remote:
                missing_in_eis.append(row)
                continue
            remote_quantity = ProductMappingService._decimal_from_initial_inventory_number(
                remote.get('currentQuantity') or remote.get('quantityInStock') or remote.get('quantity') or 0,
                '0.001',
            )
            row.update({
                'remote_quantity': str(remote_quantity),
                'difference': str((local_quantity - remote_quantity).quantize(Decimal('0.001'))),
                'remote': remote,
            })
            if abs(local_quantity - remote_quantity) > quantity_tolerance:
                quantity_mismatches.append(row)
            else:
                matched.append(row)

        missing_in_pos = []
        for code, remote in remote_by_code.items():
            if code not in local_codes:
                missing_in_pos.append({
                    'mra_product_code': remote.get('barcode') or remote.get('barCode') or code,
                    'name': remote.get('productName') or remote.get('productDescription') or '',
                    'remote_quantity': str(
                        ProductMappingService._decimal_from_initial_inventory_number(
                            remote.get('currentQuantity') or remote.get('quantityInStock') or remote.get('quantity') or 0,
                            '0.001',
                        )
                    ),
                    'remote': remote,
                })

        return {
            'dry_run': bool(warehouse.get('dry_run')),
            'terminal_id': str(terminal.id) if terminal else None,
            'branch_id': str(branch.id) if branch else None,
            'warehouse_stock_count': warehouse.get('stock_count', 0),
            'matched_count': len(matched),
            'quantity_mismatch_count': len(quantity_mismatches),
            'missing_in_eis_count': len(missing_in_eis),
            'missing_in_pos_count': len(missing_in_pos),
            'matched': matched,
            'quantity_mismatches': quantity_mismatches,
            'missing_in_eis': missing_in_eis,
            'missing_in_pos': missing_in_pos,
            'endpoint': warehouse.get('endpoint'),
        }


class StockReceivingService:
    """Submit inventory receiving/adjustment movements to MRA EIS stock endpoints."""

    STOCK_RECEIPT_REASON = 'Stock received through POS'
    STOCK_CORRECTION_REASON = 'Receive stock correction'
    INFORMAL_PURCHASE_MIN_QUANTITY = Decimal('1')
    INFORMAL_PURCHASE_MIN_UNIT_PRICE = Decimal('0.01')
    _adjustment_reason_cache: dict[str, tuple[datetime, list[str]]] = {}

    @staticmethod
    def _is_eis_enabled(business) -> bool:
        try:
            business_settings = business.settings
        except Exception:
            business_settings = None
        return bool(getattr(business_settings, 'enable_eis', False))

    @staticmethod
    def _to_decimal(value: Any, default: Decimal = Decimal('0')) -> Decimal:
        if value in (None, ''):
            return default
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return default
        return parsed if parsed.is_finite() else default

    @staticmethod
    def _to_positive_int(value: Any) -> int | None:
        if value in (None, ''):
            return None
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _decimal_to_float(value: Any, places: str = '0.001') -> float:
        quantizer = Decimal(places)
        parsed = StockReceivingService._to_decimal(value)
        return float(parsed.quantize(quantizer, rounding=ROUND_HALF_UP))

    @staticmethod
    def _money_to_float(value: Any) -> float:
        parsed = StockReceivingService._to_decimal(value)
        return float(parsed.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

    @staticmethod
    def _active_terminal_for_branch(business, branch) -> Terminal | None:
        queryset = Terminal.objects.filter(business=business, status='active')
        if branch is not None:
            queryset = queryset.filter(branch=branch)
        terminal = queryset.exclude(mra_token='').order_by('-updated_at').first()
        if terminal:
            return terminal
        return queryset.order_by('-updated_at').first()

    @staticmethod
    def _stock_code_for_item(inventory_item, mapping=None) -> str:
        candidates = [
            getattr(mapping, 'mra_product_code', None),
            getattr(inventory_item, 'barcode', None),
            getattr(inventory_item, 'product_code', None),
            getattr(inventory_item, 'sku', None),
        ]
        for candidate in candidates:
            normalized = str(candidate or '').strip()
            if normalized:
                return normalized
        return str(getattr(inventory_item, 'id', '') or '').strip()

    @staticmethod
    def _mapping_for_item(inventory_item):
        try:
            mapping = inventory_item.mra_mapping
        except Exception:
            return None
        return mapping if mapping and mapping.is_approved and mapping.mra_synced else None

    @staticmethod
    def _normalize_match(value: Any) -> str:
        return re.sub(r'[^a-z0-9]', '', str(value or '').strip().lower())

    @staticmethod
    def _extract_suppliers(response_data: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(response_data, dict):
            return []
        data = response_data.get('data')
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ('suppliers', 'items', 'results'):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _get_mra_suppliers(client: MRAEISClient) -> tuple[list[dict[str, Any]], MRACallResult]:
        errors: list[str] = []
        last_result: MRACallResult | None = None

        for method, payload in (('POST', {}), ('GET', None)):
            try:
                result = client.call(
                    'get_suppliers',
                    payload=payload,
                    method=method,
                    mutating=False,
                    send_json=payload is not None,
                )
                last_result = result
                suppliers = StockReceivingService._extract_suppliers(result.data or {})
                return suppliers, result
            except Exception as exc:
                errors.append(f'{method}: {exc}')
                status_code = getattr(exc, 'status_code', None)
                if status_code not in (404, 405):
                    raise

        raise MRAIntegrationError(
            'Unable to fetch MRA EIS suppliers: ' + '; '.join(errors),
            endpoint_key='get_suppliers',
            endpoint=getattr(last_result, 'endpoint', None) if last_result else None,
        )

    @staticmethod
    def _extract_stock_adjustment_reasons(response_data: dict[str, Any]) -> list[str]:
        if not isinstance(response_data, dict):
            return []

        data = response_data.get('data', response_data)
        candidate_lists: list[Any] = []
        if isinstance(data, list):
            candidate_lists.append(data)
        elif isinstance(data, dict):
            for key in (
                'stockAdjustmentReasons',
                'adjustmentReasons',
                'reasons',
                'items',
                'results',
                'data',
            ):
                value = data.get(key)
                if isinstance(value, list):
                    candidate_lists.append(value)

        reasons: list[str] = []
        for candidates in candidate_lists:
            for candidate in candidates:
                if isinstance(candidate, str):
                    reason = candidate.strip()
                elif isinstance(candidate, dict):
                    reason = str(
                        candidate.get('adjustmentReason')
                        or candidate.get('reason')
                        or candidate.get('reasonName')
                        or candidate.get('reasonId')
                        or candidate.get('name')
                        or candidate.get('code')
                        or candidate.get('value')
                        or candidate.get('description')
                        or candidate.get('id')
                        or ''
                    ).strip()
                else:
                    reason = ''
                if reason and reason not in reasons:
                    reasons.append(reason)
        return reasons

    @staticmethod
    def _get_stock_adjustment_reasons(client: MRAEISClient) -> list[str]:
        terminal_id = str(getattr(getattr(client, 'terminal', None), 'id', '') or 'default')
        cached = StockReceivingService._adjustment_reason_cache.get(terminal_id)
        if cached:
            cached_at, reasons = cached
            if timezone.now() - cached_at < timedelta(hours=1):
                return reasons

        errors: list[str] = []
        for method, payload in (('POST', {}), ('GET', None)):
            try:
                result = client.call(
                    'get_stock_adjustment_reasons',
                    payload=payload,
                    method=method,
                    mutating=False,
                )
                reasons = StockReceivingService._extract_stock_adjustment_reasons(result.data or {})
                if reasons:
                    StockReceivingService._adjustment_reason_cache[terminal_id] = (timezone.now(), reasons)
                    return reasons
            except Exception as exc:
                errors.append(f'{method}: {exc}')
                status_code = getattr(exc, 'status_code', None)
                if status_code not in (404, 405):
                    break

        if errors:
            logger.warning('Unable to fetch MRA EIS stock adjustment reasons: %s', '; '.join(errors))
        return []

    @staticmethod
    def _reason_matches(value: str, keyword: str) -> bool:
        normalized_value = StockReceivingService._normalize_match(value)
        normalized_keyword = StockReceivingService._normalize_match(keyword)
        return bool(
            normalized_value
            and normalized_keyword
            and (
                normalized_keyword in normalized_value
                or normalized_value in normalized_keyword
            )
        )

    @staticmethod
    def _requested_stock_reason_keywords(requested_reason: str, adjustment_type: str) -> tuple[str, ...]:
        normalized = StockReceivingService._normalize_match(requested_reason)
        is_increase = str(adjustment_type).strip().lower() == 'increase'

        if is_increase:
            if any(token in normalized for token in ('reverse', 'reversed', 'restore', 'restored', 'void')):
                return ('increase', 'restore', 'correction', 'count', 'other')
            if any(token in normalized for token in ('reduced', 'reduce')):
                return ('increase', 'correction', 'count', 'other')
            if any(token in normalized for token in ('receive', 'received', 'receipt', 'purchase')):
                return ('purchase', 'received', 'receipt', 'increase', 'correction', 'other')
            return ()

        decrease_keywords: list[str] = []
        for token, keywords in (
            ('expired', ('expired', 'expiry')),
            ('damage', ('damage', 'damaged')),
            ('damaged', ('damage', 'damaged')),
            ('spoil', ('spoil', 'spoiled', 'spoilage')),
            ('waste', ('waste', 'wastage')),
            ('loss', ('loss', 'lost')),
            ('theft', ('theft', 'stolen')),
            ('removed', ('decrease', 'removed', 'correction')),
            ('reduced', ('decrease', 'reduced', 'correction')),
            ('reduce', ('decrease', 'reduced', 'correction')),
        ):
            if token in normalized:
                for keyword in keywords:
                    if keyword not in decrease_keywords:
                        decrease_keywords.append(keyword)

        return tuple(decrease_keywords)

    @staticmethod
    def _select_stock_adjustment_reason(
        *,
        client: MRAEISClient,
        requested_reason: str,
        adjustment_type: str,
    ) -> str:
        requested_reason = str(requested_reason or '').strip()
        valid_reasons = StockReceivingService._get_stock_adjustment_reasons(client)
        if not valid_reasons:
            return (
                str(getattr(settings, 'MRA_EIS_STOCK_ADJUSTMENT_REASON_FALLBACK', '') or '').strip()
                or requested_reason
                or StockReceivingService.STOCK_CORRECTION_REASON
            )

        normalized_requested = StockReceivingService._normalize_match(requested_reason)
        for reason in valid_reasons:
            if normalized_requested and StockReceivingService._normalize_match(reason) == normalized_requested:
                return reason

        reason_override = ''
        is_increase = str(adjustment_type).strip().lower() == 'increase'
        if is_increase:
            reason_override = str(getattr(settings, 'MRA_EIS_STOCK_INCREASE_ADJUSTMENT_REASON', '') or '').strip()
            preferred_keywords = (
                'purchase',
                'received',
                'receipt',
                'stock increase',
                'increase',
                'opening',
                'count',
                'correction',
                'other',
            )
        else:
            reason_override = str(getattr(settings, 'MRA_EIS_STOCK_DECREASE_ADJUSTMENT_REASON', '') or '').strip()
            preferred_keywords = (
                'waste',
                'damage',
                'expired',
                'spoil',
                'loss',
                'decrease',
                'count',
                'correction',
                'other',
            )

        if reason_override:
            for reason in valid_reasons:
                if StockReceivingService._normalize_match(reason) == StockReceivingService._normalize_match(reason_override):
                    return reason

        requested_keywords = StockReceivingService._requested_stock_reason_keywords(
            requested_reason,
            adjustment_type,
        )
        for keyword in requested_keywords:
            for reason in valid_reasons:
                if StockReceivingService._reason_matches(reason, keyword):
                    return reason

        for keyword in preferred_keywords:
            for reason in valid_reasons:
                if StockReceivingService._reason_matches(reason, keyword):
                    return reason

        return valid_reasons[0]

    @staticmethod
    def _prepare_stock_adjustment_payload(client: MRAEISClient, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        requested_reason = str(payload.get('adjustmentReason') or '').strip()
        adjustment_type = str(payload.get('adjustmentType') or '').strip()
        selected_reason = StockReceivingService._select_stock_adjustment_reason(
            client=client,
            requested_reason=requested_reason,
            adjustment_type=adjustment_type,
        )
        payload['adjustmentReason'] = selected_reason[:255]

        if requested_reason and StockReceivingService._normalize_match(requested_reason) != StockReceivingService._normalize_match(selected_reason):
            remarks = str(payload.get('taxpayerRemarks') or '').strip()
            original_note = f"POS reason: {requested_reason}"
            payload['taxpayerRemarks'] = (
                f"{remarks}; {original_note}" if remarks else original_note
            )[:500]

        return payload

    @staticmethod
    def _resolve_supplier_id(purchase_order, client: MRAEISClient) -> int | None:
        supplier = getattr(purchase_order, 'supplier', None)
        explicit_supplier_id = (
            getattr(purchase_order, 'mra_supplier_id', None)
            or getattr(supplier, 'mra_supplier_id', None)
            or getattr(settings, 'MRA_EIS_DEFAULT_SUPPLIER_ID', None)
        )
        parsed_explicit_id = StockReceivingService._to_positive_int(explicit_supplier_id)
        if parsed_explicit_id:
            return parsed_explicit_id

        tin = str(
            getattr(purchase_order, 'supplier_tin', '') or getattr(supplier, 'supplier_tin', '') or ''
        ).strip()
        supplier_name = str(getattr(supplier, 'name', '') or '').strip()

        if not tin and not supplier_name:
            return None

        try:
            suppliers, _result = StockReceivingService._get_mra_suppliers(client)
        except Exception as exc:
            logger.warning('Unable to fetch MRA EIS suppliers for stock receiving: %s', exc)
            return None

        if not suppliers:
            return None

        normalized_tin = StockReceivingService._normalize_match(tin)
        normalized_name = StockReceivingService._normalize_match(supplier_name)
        for candidate in suppliers:
            candidate_tin = StockReceivingService._normalize_match(
                candidate.get('supplierTin') or candidate.get('supplierTIN') or candidate.get('tin')
            )
            if normalized_tin and candidate_tin == normalized_tin:
                resolved_id = StockReceivingService._to_positive_int(candidate.get('supplierId'))
                if resolved_id:
                    StockReceivingService._persist_supplier_id(purchase_order, supplier, resolved_id)
                return resolved_id

        for candidate in suppliers:
            candidate_name = StockReceivingService._normalize_match(
                candidate.get('supplierName') or candidate.get('name')
            )
            if normalized_name and candidate_name == normalized_name:
                resolved_id = StockReceivingService._to_positive_int(candidate.get('supplierId'))
                if resolved_id:
                    StockReceivingService._persist_supplier_id(purchase_order, supplier, resolved_id)
                return resolved_id

        return None

    @staticmethod
    def _persist_supplier_id(purchase_order, supplier, supplier_id: int) -> None:
        try:
            if supplier_id and getattr(purchase_order, 'mra_supplier_id', None) != supplier_id:
                purchase_order.mra_supplier_id = supplier_id
                purchase_order.save(update_fields=['mra_supplier_id', 'updated_at'])
        except Exception as exc:
            logger.debug('Could not persist MRA supplier ID on purchase order: %s', exc)

        try:
            if supplier and supplier_id and getattr(supplier, 'mra_supplier_id', None) != supplier_id:
                supplier.mra_supplier_id = supplier_id
                supplier.save(update_fields=['mra_supplier_id', 'updated_at'])
        except Exception as exc:
            logger.debug('Could not persist MRA supplier ID on supplier: %s', exc)

    @staticmethod
    def _build_goods_receiving_payload(
        *,
        purchase_item,
        inventory_item,
        mapping,
        supplier_id: int,
        quantity: Decimal,
    ) -> dict[str, Any]:
        purchase_order = purchase_item.purchase_order
        quantity_value = StockReceivingService._to_decimal(quantity)
        if quantity_value < StockReceivingService.INFORMAL_PURCHASE_MIN_QUANTITY:
            raise MRAIntegrationError(
                'MRA informal purchase quantity must be at least 1 for each received line.'
            )

        unit_price = StockReceivingService._to_decimal(
            getattr(purchase_item, 'cost_per_unit', None),
            Decimal('0'),
        )
        if unit_price < StockReceivingService.INFORMAL_PURCHASE_MIN_UNIT_PRICE:
            raise MRAIntegrationError(
                'MRA informal purchase unit price must be at least 0.01. '
                'Use B2B stock-transfer mode only when MRA already increased stock from the supplier transfer.'
            )

        total_price = quantity_value * unit_price
        received_at = purchase_order.received_date or getattr(purchase_item, 'created_at', None) or timezone.now()
        description = (
            str(getattr(mapping, 'mra_product_name', '') or '').strip()
            or str(getattr(inventory_item, 'name', '') or '').strip()
            or StockReceivingService._stock_code_for_item(inventory_item, mapping)
        )

        return {
            'supplierId': supplier_id,
            'deliveryNoteNumber': str(purchase_order.reference_number or '')[:100] or None,
            'receivingDate': received_at.isoformat(),
            'purchaseOrderNumber': str(purchase_order.order_number or purchase_order.id),
            'receivedBy': str(purchase_order.created_by or 'System')[:255] or 'System',
            'totalItems': 1,
            'totalQuantity': StockReceivingService._decimal_to_float(quantity_value),
            'totalValue': StockReceivingService._money_to_float(total_price),
            'notes': f"POS stock receipt for {description}"[:500],
            'items': [
                {
                    'itemCode': StockReceivingService._stock_code_for_item(inventory_item, mapping),
                    'description': description[:255],
                    'quantityOrdered': StockReceivingService._decimal_to_float(quantity_value),
                    'quantityReceived': StockReceivingService._decimal_to_float(quantity_value),
                    'unitOfMeasure': str(getattr(mapping, 'mra_unit_measure', '') or getattr(inventory_item, 'unit_type', '') or 'unit')[:50],
                    'unitPrice': StockReceivingService._money_to_float(unit_price),
                    'totalPrice': StockReceivingService._money_to_float(total_price),
                    'isFinishedProduct': getattr(inventory_item, 'item_type', '') == 'sellable',
                }
            ],
        }

    @staticmethod
    def _build_stock_adjustment_payload(
        *,
        business,
        branch,
        inventory_item,
        mapping,
        quantity: Decimal,
        adjustment_type: str,
        reason: str,
        remarks: str,
    ) -> dict[str, Any]:
        return {
            'barcode': StockReceivingService._stock_code_for_item(inventory_item, mapping),
            'quantity': StockReceivingService._decimal_to_float(max(quantity, Decimal('0.001'))),
            'adjustmentReason': reason[:255] or StockReceivingService.STOCK_CORRECTION_REASON,
            'adjustmentType': adjustment_type,
            'siteId': ConfigurationService.get_terminal_site_id(business, branch) or None,
            'taxpayerRemarks': remarks[:500] if remarks else None,
        }

    @staticmethod
    def _queue_purchase_receipt_retry(
        *,
        terminal: Terminal,
        purchase_item,
        quantity: Decimal,
        last_error: str,
    ) -> str | None:
        purchase_item_id = str(purchase_item.id)
        payload = {
            'purchase_item_id': purchase_item_id,
            'quantity': str(quantity),
        }

        try:
            pending = SyncRetryQueue.objects.filter(
                terminal=terminal,
                operation_type='submit_purchase_item_receipt',
                status__in=['pending', 'processing'],
            )
            for retry in pending:
                if str((retry.payload or {}).get('purchase_item_id')) == purchase_item_id:
                    retry.payload = payload
                    retry.last_error = last_error
                    retry.next_attempt_at = timezone.now()
                    retry.save(update_fields=['payload', 'last_error', 'next_attempt_at'])
                    return str(retry.id)

            retry = RetryService.queue_retry(
                terminal,
                'submit_purchase_item_receipt',
                payload,
                max_attempts=10,
            )
            retry.last_error = last_error
            retry.save(update_fields=['last_error'])
            return str(retry.id)
        except Exception as exc:
            logger.warning('Failed to queue EIS purchase receipt retry for %s: %s', purchase_item_id, exc)
            return None

    @staticmethod
    def _purchase_receipt_action_required_result(
        *,
        terminal: Terminal,
        purchase_item,
        quantity: Decimal,
        message: str,
        reason: str,
        error_code: str,
        queue_on_failure: bool,
        raise_on_error: bool,
    ) -> dict[str, Any]:
        retry_id = None
        if queue_on_failure:
            retry_id = StockReceivingService._queue_purchase_receipt_retry(
                terminal=terminal,
                purchase_item=purchase_item,
                quantity=quantity,
                last_error=message,
            )

        try:
            MRAAPIError.objects.create(
                terminal=terminal,
                error_type='invalid_request',
                error_message=message,
                error_code=error_code,
            )
        except Exception as exc:
            logger.debug('Could not record EIS purchase receipt validation error: %s', exc)

        if raise_on_error:
            raise MRAIntegrationError(message)

        return {
            'submitted': False,
            'skipped': False,
            'reason': reason,
            'endpoint_key': 'submit_informal_purchase',
            'requires_action': True,
            'retry_id': retry_id,
            'error': message,
        }

    @staticmethod
    def _purchase_receipt_supplier_error(purchase_order) -> str:
        supplier = getattr(purchase_order, 'supplier', None)
        supplier_name = str(getattr(supplier, 'name', '') or getattr(purchase_order, 'supplier_name', '') or '').strip()
        supplier_tin = str(
            getattr(purchase_order, 'supplier_tin', '')
            or getattr(supplier, 'supplier_tin', '')
            or ''
        ).strip()
        details = []
        if supplier_name:
            details.append(f'supplier "{supplier_name}"')
        if supplier_tin:
            details.append(f'TIN {supplier_tin}')
        suffix = f" for {' / '.join(details)}" if details else ''
        return (
            f'MRA EIS supplierId is required{suffix} before receive-stock can be submitted. '
            'Sync/select the supplier from MRA or set mra_supplier_id on the supplier/purchase order.'
        )

    @staticmethod
    def submit_stock_payload(
        *,
        terminal: Terminal,
        endpoint_key: str,
        payload: dict[str, Any],
        operation_label: str,
        queue_on_failure: bool = True,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        client = MRAEISClient(terminal=terminal)
        if endpoint_key == 'submit_stock_adjustment':
            payload = StockReceivingService._prepare_stock_adjustment_payload(client, payload)
        try:
            result = client.call(endpoint_key, payload=payload, method='POST', mutating=True)
            response_data = result.data or {}
            response_errors = _extract_mra_response_errors(response_data)
            if response_errors and not result.dry_run:
                raise MRAIntegrationError(
                    f"MRA rejected {operation_label}: {'; '.join(response_errors)}",
                    status_code=result.status_code,
                    endpoint=result.endpoint,
                    endpoint_key=endpoint_key,
                    response_data=response_data,
                )

            terminal.last_sync_at = timezone.now()
            terminal.save(update_fields=['last_sync_at', 'updated_at'])
            return {
                'submitted': True,
                'dry_run': result.dry_run,
                'endpoint': result.endpoint,
                'endpoint_key': endpoint_key,
                'status_code': result.status_code,
                'payload': payload,
                'response': response_data,
                'errors': response_errors,
            }
        except Exception as exc:
            endpoint = getattr(exc, 'endpoint', None)
            if not endpoint:
                try:
                    endpoint = client._resolve_endpoint(endpoint_key)
                except Exception:
                    endpoint = endpoint_key

            MRAAPIError.objects.create(
                terminal=terminal,
                error_type='invalid_request' if isinstance(exc, MRAIntegrationError) else 'connection_error',
                error_message=str(exc),
                error_code=str(getattr(exc, 'status_code', '') or ''),
            )
            if queue_on_failure:
                try:
                    RetryService.queue_retry(
                        terminal,
                        'submit_stock_payload',
                        {
                            'terminal_id': str(terminal.id),
                            'endpoint_key': endpoint_key,
                            'payload': payload,
                            'operation_label': operation_label,
                        },
                    )
                except Exception as retry_exc:
                    logger.warning('Failed to queue EIS stock retry: %s', retry_exc)
            logger.warning('MRA EIS stock submission failed (%s): %s', endpoint_key, exc)
            if raise_on_error:
                raise
            return {
                'submitted': False,
                'endpoint': endpoint,
                'endpoint_key': endpoint_key,
                'payload': payload,
                'error': str(exc),
            }

    @staticmethod
    def submit_purchase_item_receipt(
        purchase_item,
        quantity: Any,
        *,
        queue_on_failure: bool = True,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Submit a positive purchase receipt quantity using MRA goods receiving."""
        purchase_order = purchase_item.purchase_order
        if str(getattr(purchase_order, 'eis_stock_receipt_source', '') or '') == 'supplier_sale':
            return {'submitted': False, 'skipped': True, 'reason': 'already_posted_by_b2b_transfer'}
        business = purchase_order.business
        branch = purchase_order.branch
        if not StockReceivingService._is_eis_enabled(business):
            return {'submitted': False, 'skipped': True, 'reason': 'eis_disabled'}

        terminal = StockReceivingService._active_terminal_for_branch(business, branch)
        if terminal is None:
            return {'submitted': False, 'skipped': True, 'reason': 'no_active_terminal'}

        inventory_item = purchase_item.inventory_item
        mapping = StockReceivingService._mapping_for_item(inventory_item)
        if mapping is None:
            return {'submitted': False, 'skipped': True, 'reason': 'missing_approved_mra_mapping'}

        quantity_value = StockReceivingService._to_decimal(quantity)
        if quantity_value <= 0:
            return {'submitted': False, 'skipped': True, 'reason': 'non_positive_quantity'}

        use_goods_receiving = bool(getattr(settings, 'MRA_EIS_RECEIVE_STOCK_USE_GOODS_RECEIVING', True))
        if not use_goods_receiving:
            message = (
                'MRA EIS goods receiving is disabled. Purchase receipts will not be submitted '
                'as generic stock adjustments for certification safety.'
            )
            if raise_on_error:
                raise MRAIntegrationError(message)
            return {
                'submitted': False,
                'skipped': False,
                'reason': 'goods_receiving_disabled',
                'endpoint_key': 'submit_informal_purchase',
                'error': message,
            }

        if quantity_value < StockReceivingService.INFORMAL_PURCHASE_MIN_QUANTITY:
            message = (
                'MRA informal purchase quantity must be at least 1 for each received line. '
                'Use B2B stock-transfer mode for local batch capture only when MRA already increased stock.'
            )
            return StockReceivingService._purchase_receipt_action_required_result(
                terminal=terminal,
                purchase_item=purchase_item,
                quantity=quantity_value,
                message=message,
                reason='invalid_informal_purchase_quantity',
                error_code='invalid_informal_purchase_quantity',
                queue_on_failure=queue_on_failure,
                raise_on_error=raise_on_error,
            )

        unit_price = StockReceivingService._to_decimal(getattr(purchase_item, 'cost_per_unit', None))
        if unit_price < StockReceivingService.INFORMAL_PURCHASE_MIN_UNIT_PRICE:
            message = (
                'MRA informal purchase unit price must be at least 0.01. '
                'Enter the supplier cost before submitting receive-stock to EIS, or use B2B stock-transfer mode only when MRA already increased stock.'
            )
            return StockReceivingService._purchase_receipt_action_required_result(
                terminal=terminal,
                purchase_item=purchase_item,
                quantity=quantity_value,
                message=message,
                reason='invalid_informal_purchase_unit_price',
                error_code='invalid_informal_purchase_unit_price',
                queue_on_failure=queue_on_failure,
                raise_on_error=raise_on_error,
            )

        client = MRAEISClient(terminal=terminal)
        supplier_id = StockReceivingService._resolve_supplier_id(purchase_order, client)
        if not supplier_id:
            message = StockReceivingService._purchase_receipt_supplier_error(purchase_order)
            return StockReceivingService._purchase_receipt_action_required_result(
                terminal=terminal,
                purchase_item=purchase_item,
                quantity=quantity_value,
                message=message,
                reason='missing_mra_supplier_id',
                error_code='missing_mra_supplier_id',
                queue_on_failure=queue_on_failure,
                raise_on_error=raise_on_error,
            )

        payload = StockReceivingService._build_goods_receiving_payload(
            purchase_item=purchase_item,
            inventory_item=inventory_item,
            mapping=mapping,
            supplier_id=supplier_id,
            quantity=quantity_value,
        )
        return StockReceivingService.submit_stock_payload(
            terminal=terminal,
            endpoint_key='submit_informal_purchase',
            payload=payload,
            operation_label='stock receipt pending EIS approval',
            queue_on_failure=queue_on_failure,
            raise_on_error=raise_on_error,
        )

    @staticmethod
    def submit_purchase_item_adjustment(
        *,
        purchase_item,
        quantity: Any,
        adjustment_type: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Submit an EIS stock adjustment for receive-stock corrections."""
        purchase_order = purchase_item.purchase_order
        if str(getattr(purchase_order, 'eis_stock_receipt_source', '') or '') == 'supplier_sale':
            return {'submitted': False, 'skipped': True, 'reason': 'already_posted_by_b2b_transfer'}
        business = purchase_order.business
        branch = purchase_order.branch
        if not StockReceivingService._is_eis_enabled(business):
            return {'submitted': False, 'skipped': True, 'reason': 'eis_disabled'}

        terminal = StockReceivingService._active_terminal_for_branch(business, branch)
        if terminal is None:
            return {'submitted': False, 'skipped': True, 'reason': 'no_active_terminal'}

        inventory_item = purchase_item.inventory_item
        mapping = StockReceivingService._mapping_for_item(inventory_item)
        if mapping is None:
            return {'submitted': False, 'skipped': True, 'reason': 'missing_approved_mra_mapping'}

        quantity_value = StockReceivingService._to_decimal(quantity)
        if quantity_value <= 0:
            return {'submitted': False, 'skipped': True, 'reason': 'non_positive_quantity'}

        normalized_type = 'Decrease' if str(adjustment_type).lower() == 'decrease' else 'Increase'
        payload = StockReceivingService._build_stock_adjustment_payload(
            business=business,
            branch=branch,
            inventory_item=inventory_item,
            mapping=mapping,
            quantity=quantity_value,
            adjustment_type=normalized_type,
            reason=reason or StockReceivingService.STOCK_CORRECTION_REASON,
            remarks=f"Purchase receipt correction for {purchase_order.order_number}.",
        )
        return StockReceivingService.submit_stock_payload(
            terminal=terminal,
            endpoint_key='submit_stock_adjustment',
            payload=payload,
            operation_label='stock correction adjustment',
        )

    @staticmethod
    def submit_inventory_item_adjustment(
        *,
        business,
        branch,
        inventory_item,
        quantity: Any,
        adjustment_type: str,
        reason: str,
        remarks: str = '',
    ) -> dict[str, Any]:
        """Submit a direct EIS stock adjustment for inventory movements like waste."""
        if not StockReceivingService._is_eis_enabled(business):
            return {'submitted': False, 'skipped': True, 'reason': 'eis_disabled'}

        terminal = StockReceivingService._active_terminal_for_branch(business, branch)
        if terminal is None:
            return {'submitted': False, 'skipped': True, 'reason': 'no_active_terminal'}

        mapping = StockReceivingService._mapping_for_item(inventory_item)
        if mapping is None:
            return {'submitted': False, 'skipped': True, 'reason': 'missing_approved_mra_mapping'}

        quantity_value = StockReceivingService._to_decimal(quantity)
        if quantity_value <= 0:
            return {'submitted': False, 'skipped': True, 'reason': 'non_positive_quantity'}

        normalized_type = 'Decrease' if str(adjustment_type).lower() == 'decrease' else 'Increase'
        payload = StockReceivingService._build_stock_adjustment_payload(
            business=business,
            branch=branch,
            inventory_item=inventory_item,
            mapping=mapping,
            quantity=quantity_value,
            adjustment_type=normalized_type,
            reason=reason,
            remarks=remarks,
        )
        return StockReceivingService.submit_stock_payload(
            terminal=terminal,
            endpoint_key='submit_stock_adjustment',
            payload=payload,
            operation_label='inventory stock adjustment',
        )

    @staticmethod
    def retry_stock_payload(payload: dict[str, Any]) -> dict[str, Any]:
        terminal = Terminal.objects.get(id=payload['terminal_id'])
        return StockReceivingService.submit_stock_payload(
            terminal=terminal,
            endpoint_key=payload['endpoint_key'],
            payload=payload.get('payload') or {},
            operation_label=payload.get('operation_label') or 'stock payload',
            queue_on_failure=False,
            raise_on_error=True,
        )


class SupplierSyncService:
    """Synchronize official MRA EIS suppliers into the local supplier table."""

    @staticmethod
    def _first(item: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = item.get(key)
            if value not in (None, ''):
                return value
        return None

    @staticmethod
    def _clean_text(value: Any) -> str:
        return str(value or '').strip()

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {'1', 'true', 'yes', 'y', 'on'}:
            return True
        if normalized in {'0', 'false', 'no', 'n', 'off'}:
            return False
        return default

    @staticmethod
    def _normalize_supplier(item: dict[str, Any]) -> dict[str, Any] | None:
        supplier_id = StockReceivingService._to_positive_int(
            SupplierSyncService._first(item, 'supplierId', 'supplierID', 'id')
        )
        if not supplier_id:
            return None

        supplier_name = SupplierSyncService._clean_text(
            SupplierSyncService._first(item, 'supplierName', 'name', 'tradingName')
        )
        supplier_tin = SupplierSyncService._clean_text(
            SupplierSyncService._first(item, 'supplierTin', 'supplierTIN', 'tin', 'TIN')
        )

        return {
            'mra_supplier_id': supplier_id,
            'name': supplier_name or f'EIS Supplier {supplier_id}',
            'supplier_tin': supplier_tin or None,
            'contact_person': SupplierSyncService._clean_text(
                SupplierSyncService._first(
                    item,
                    'contactPerson',
                    'supplierContactPerson',
                    'contact_person',
                    'contactName',
                )
            ),
            'email': SupplierSyncService._clean_text(
                SupplierSyncService._first(
                    item,
                    'contactEmail',
                    'supplierContactEmail',
                    'email',
                    'emailAddress',
                )
            ),
            'phone': SupplierSyncService._clean_text(
                SupplierSyncService._first(
                    item,
                    'contactPhone',
                    'supplierContactPhone',
                    'phone',
                    'phoneNumber',
                )
            ),
            'address': SupplierSyncService._clean_text(
                SupplierSyncService._first(
                    item,
                    'supplierAddress',
                    'address',
                    'physicalAddress',
                    'postalAddress',
                    'addressLine',
                )
            ),
            'city': SupplierSyncService._clean_text(
                SupplierSyncService._first(
                    item,
                    'cityPlaceOfBusiness',
                    'placeOfBusiness',
                    'city',
                    'district',
                    'town',
                )
            ),
            'region': SupplierSyncService._clean_text(
                SupplierSyncService._first(item, 'regionState', 'region', 'state', 'province')
            ),
            'country': SupplierSyncService._clean_text(
                SupplierSyncService._first(item, 'country', 'countryName')
            ),
            'vat_registered': SupplierSyncService._to_bool(
                SupplierSyncService._first(
                    item,
                    'vatRegistered',
                    'isVATRegistered',
                    'isVatRegistered',
                    'is_vat_registered',
                ),
                default=False,
            ),
            'raw': item,
        }

    @staticmethod
    def _resolve_terminal(business, terminal: Terminal | None = None) -> Terminal | None:
        if terminal:
            return terminal

        return (
            Terminal.objects.filter(business=business, status='active')
            .exclude(mra_token='')
            .order_by('-last_sync_at', '-updated_at')
            .first()
            or Terminal.objects.filter(business=business, status='active')
            .order_by('-last_sync_at', '-updated_at')
            .first()
        )

    @staticmethod
    def sync_from_mra(*, business, terminal: Terminal | None = None) -> dict[str, Any]:
        """Pull suppliers from MRA and upsert local Supplier records."""
        from inventory.models import Supplier
        from inventory.serializers import SupplierSerializer

        terminal = SupplierSyncService._resolve_terminal(business, terminal)
        if not terminal:
            raise ValueError('Activate an MRA terminal before syncing EIS suppliers.')

        client = MRAEISClient(terminal=terminal)
        mra_suppliers, result = StockReceivingService._get_mra_suppliers(client)

        created = 0
        updated = 0
        skipped = 0
        synced_ids: list[str] = []

        with transaction.atomic():
            for item in mra_suppliers:
                normalized = SupplierSyncService._normalize_supplier(item)
                if not normalized:
                    skipped += 1
                    continue

                supplier = Supplier.objects.filter(
                    business=business,
                    mra_supplier_id=normalized['mra_supplier_id'],
                ).first()
                if supplier is None and normalized.get('supplier_tin'):
                    supplier = Supplier.objects.filter(
                        business=business,
                        supplier_tin=normalized['supplier_tin'],
                    ).first()

                defaults = {
                    'name': normalized['name'],
                    'supplier_tin': normalized['supplier_tin'],
                    'mra_supplier_id': normalized['mra_supplier_id'],
                    'vat_registered': normalized['vat_registered'],
                    'is_active': True,
                }
                for field_name in ('contact_person', 'email', 'phone', 'address', 'city', 'region', 'country'):
                    if normalized.get(field_name):
                        defaults[field_name] = normalized[field_name]

                if supplier is None:
                    supplier = Supplier.objects.create(
                        business=business,
                        **defaults,
                    )
                    created += 1
                else:
                    changed_fields = []
                    for field_name, value in defaults.items():
                        if getattr(supplier, field_name) != value:
                            setattr(supplier, field_name, value)
                            changed_fields.append(field_name)
                    if changed_fields:
                        supplier.save(update_fields=[*changed_fields, 'updated_at'])
                        updated += 1

                synced_ids.append(str(supplier.id))

        suppliers = Supplier.objects.filter(id__in=synced_ids).order_by('name')
        return {
            'submitted': True,
            'dry_run': bool(result.dry_run),
            'endpoint': result.endpoint,
            'endpoint_key': 'get_suppliers',
            'status_code': result.status_code,
            'fetched': len(mra_suppliers),
            'created': created,
            'updated': updated,
            'skipped': skipped,
            'suppliers': SupplierSerializer(suppliers, many=True).data,
            'raw_response': result.data,
        }


class InvoiceService:
    """Invoice creation and MRA submission for standalone MRAInvoice flow."""

    @staticmethod
    def _to_json_safe(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, list):
            return [InvoiceService._to_json_safe(item) for item in value]
        if isinstance(value, dict):
            return {key: InvoiceService._to_json_safe(val) for key, val in value.items()}
        return value

    @staticmethod
    def _resolve_signature_secret(terminal: Terminal | None) -> str:
        terminal_secret = str(getattr(terminal, 'mra_api_key', '') or '').strip()
        if terminal_secret:
            return terminal_secret

        # Optional fallback for local/dev setups. Official MRA flow returns the
        # signing secret in the terminal activation response.
        secret = str(getattr(settings, 'MRA_EIS_SECRET_KEY', '') or '').strip()
        if secret:
            return secret

        if getattr(settings, 'MRA_EIS_IS_LIVE', False):
            raise MRAIntegrationError('Terminal signing secret is missing for live mode. Activate the terminal first.')
        return ''

    @staticmethod
    def _mra_base64_to_base10(value: Any) -> int:
        chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
        result = 0
        for char in str(value or '').strip():
            if char not in chars:
                raise ValueError('Invalid MRA base64 digit')
            result = result * 64 + chars.index(char)
        return result

    @staticmethod
    def extract_julian_from_fiscal_invoice_number(fiscal_invoice_number: str) -> int:
        parts = str(fiscal_invoice_number or '').split('-')
        if len(parts) < 3:
            return 0
        try:
            return InvoiceService._mra_base64_to_base10(parts[2])
        except Exception:
            return 0

    @staticmethod
    def extract_sequence_from_fiscal_invoice_number(fiscal_invoice_number: str) -> int:
        last_part = str(fiscal_invoice_number or '').rsplit('-', 1)[-1]
        try:
            return InvoiceService._mra_base64_to_base10(last_part)
        except Exception:
            try:
                return int(last_part)
            except Exception:
                return 0

    @staticmethod
    def _local_max_sequence_for_day(terminal: Terminal, julian_date: int) -> int:
        direct_max = (
            MRAInvoice.objects.filter(
                terminal=terminal,
                fiscal_julian_date=julian_date,
            ).aggregate(value=Max('invoice_number')).get('value')
            or 0
        )
        max_sequence = int(direct_max or 0)

        # Backfill safety for invoices created before fiscal_julian_date existed.
        for invoice in MRAInvoice.objects.filter(terminal=terminal, fiscal_julian_date__isnull=True).only(
            'invoice_number',
            'invoice_date',
            'mra_response',
        ):
            invoice_julian = InvoiceService._to_julian_date(invoice.invoice_date)
            if invoice_julian == julian_date:
                max_sequence = max(max_sequence, int(invoice.invoice_number or 0))
                continue

            response = invoice.mra_response if isinstance(invoice.mra_response, dict) else {}
            payload = response.get('payload') if isinstance(response.get('payload'), dict) else {}
            header = payload.get('invoiceHeader') if isinstance(payload.get('invoiceHeader'), dict) else {}
            fiscal_number = str(header.get('invoiceNumber') or '').strip()
            if (
                fiscal_number
                and InvoiceService.extract_julian_from_fiscal_invoice_number(fiscal_number) == julian_date
            ):
                max_sequence = max(
                    max_sequence,
                    InvoiceService.extract_sequence_from_fiscal_invoice_number(fiscal_number),
                )

        return max_sequence

    @staticmethod
    def _mra_no_last_transaction_errors(errors: list[str]) -> bool:
        if not errors:
            return False
        text = ' '.join(str(error or '') for error in errors).lower()
        return any(
            phrase in text
            for phrase in (
                'no transaction',
                'no invoice',
                'no receipt',
                'not found',
                'no record',
            )
        )

    @staticmethod
    def _fiscal_invoice_number_matches_terminal_identity(
        terminal: Terminal,
        fiscal_invoice_number: str,
    ) -> bool:
        taxpayer_id = getattr(terminal, 'mra_taxpayer_id', None)
        terminal_position = getattr(terminal, 'terminal_position', None)
        if not taxpayer_id or not terminal_position:
            return True

        parts = str(fiscal_invoice_number or '').split('-')
        if len(parts) < 2:
            return False

        try:
            return (
                InvoiceService._mra_base64_to_base10(parts[0]) == int(taxpayer_id)
                and InvoiceService._mra_base64_to_base10(parts[1]) == int(terminal_position)
            )
        except Exception:
            return False

    @staticmethod
    def _fetch_last_transaction_sequence_for_day(
        terminal: Terminal,
        *,
        mode: str,
        julian_date: int,
        require_success: bool,
    ) -> dict[str, Any]:
        endpoint_key = {
            'online': 'get_last_online_transaction',
            'offline': 'get_last_offline_transaction',
        }.get(mode)
        if not endpoint_key:
            raise ValueError(f'Unsupported MRA transaction mode: {mode}')

        client = MRAEISClient(terminal=terminal)
        try:
            result = client.call(
                endpoint_key,
                payload=None,
                method='POST',
                mutating=False,
                send_json=False,
            )
            response_data = MRAEISClient._normalize_response_data(result.data)
        except MRAIntegrationError as exc:
            response_data = exc.response_data if isinstance(exc.response_data, dict) else {}
            errors = _extract_mra_response_errors(response_data) or [str(exc)]
            if InvoiceService._mra_no_last_transaction_errors(errors):
                return {
                    'checked': True,
                    'mode': mode,
                    'endpoint_key': endpoint_key,
                    'remote_sequence': 0,
                    'reason': 'no_remote_transaction',
                    'errors': errors,
                }
            status_code = int(getattr(exc, 'status_code', None) or 0)
            if _is_mra_network_failure(exc, status_code=status_code, response_data=response_data):
                return {
                    'checked': False,
                    'mode': mode,
                    'endpoint_key': endpoint_key,
                    'remote_sequence': 0,
                    'reason': 'mra_network_unreachable',
                    'error': str(exc),
                }
            if require_success:
                raise MRAIntegrationError(
                    f'Unable to recover MRA last {mode} fiscal sequence before sale: {exc}'
                ) from exc
            return {
                'checked': False,
                'mode': mode,
                'endpoint_key': endpoint_key,
                'remote_sequence': 0,
                'error': str(exc),
            }
        except Exception as exc:
            if _is_mra_network_failure(exc):
                return {
                    'checked': False,
                    'mode': mode,
                    'endpoint_key': endpoint_key,
                    'remote_sequence': 0,
                    'reason': 'mra_network_unreachable',
                    'error': str(exc),
                }
            if require_success:
                raise MRAIntegrationError(
                    f'Unable to recover MRA last {mode} fiscal sequence before sale: {exc}'
                ) from exc
            return {
                'checked': False,
                'mode': mode,
                'endpoint_key': endpoint_key,
                'remote_sequence': 0,
                'error': str(exc),
            }

        if result.dry_run:
            return {
                'checked': False,
                'dry_run': True,
                'mode': mode,
                'endpoint_key': endpoint_key,
                'remote_sequence': 0,
                'response': response_data,
            }

        errors = _extract_mra_response_errors(response_data)
        if errors:
            if InvoiceService._mra_no_last_transaction_errors(errors):
                return {
                    'checked': True,
                    'mode': mode,
                    'endpoint_key': endpoint_key,
                    'remote_sequence': 0,
                    'reason': 'no_remote_transaction',
                    'errors': errors,
                    'response': response_data,
                }
            if require_success:
                raise MRAIntegrationError(
                    f'Unable to recover MRA last {mode} fiscal sequence before sale: '
                    + '; '.join(errors)
                )
            return {
                'checked': False,
                'mode': mode,
                'endpoint_key': endpoint_key,
                'remote_sequence': 0,
                'errors': errors,
                'response': response_data,
            }

        remote_invoice_number = InvoiceService._extract_invoice_number_from_mra_response(response_data)
        if not remote_invoice_number:
            return {
                'checked': True,
                'mode': mode,
                'endpoint_key': endpoint_key,
                'remote_sequence': 0,
                'reason': 'no_remote_invoice_number',
                'response': response_data,
            }

        remote_julian_date = InvoiceService.extract_julian_from_fiscal_invoice_number(remote_invoice_number)
        remote_sequence = InvoiceService.extract_sequence_from_fiscal_invoice_number(remote_invoice_number)
        if remote_julian_date <= 0 or remote_sequence <= 0:
            raise MRAIntegrationError(
                f'Unable to decode MRA last {mode} invoice number before sale: {remote_invoice_number}'
            )

        if not InvoiceService._fiscal_invoice_number_matches_terminal_identity(terminal, remote_invoice_number):
            raise MRAIntegrationError(
                f'MRA last {mode} invoice number {remote_invoice_number} does not match this terminal identity.'
            )

        if remote_julian_date != julian_date:
            return {
                'checked': True,
                'mode': mode,
                'endpoint_key': endpoint_key,
                'remote_invoice_number': remote_invoice_number,
                'remote_julian_date': remote_julian_date,
                'remote_sequence': 0,
                'reason': 'remote_invoice_from_different_fiscal_day',
                'response': response_data,
            }

        return {
            'checked': True,
            'mode': mode,
            'endpoint_key': endpoint_key,
            'remote_invoice_number': remote_invoice_number,
            'remote_julian_date': remote_julian_date,
            'remote_sequence': remote_sequence,
            'response': response_data,
        }

    @staticmethod
    def recover_fiscal_sequence_from_mra(terminal: Terminal, julian_date: int) -> dict[str, Any]:
        if not bool(getattr(settings, 'MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES', True)):
            return {'checked': False, 'reason': 'disabled', 'max_sequence': 0, 'results': {}}

        client = MRAEISClient(terminal=terminal)
        if not client.http_enabled:
            return {'checked': False, 'reason': 'http_calls_disabled', 'max_sequence': 0, 'results': {}}
        if client.dry_run:
            return {'checked': False, 'reason': 'dry_run_enabled', 'max_sequence': 0, 'results': {}}

        results: dict[str, Any] = {}
        max_sequence = 0
        unreachable_modes: list[str] = []
        for mode in ('online', 'offline'):
            result = InvoiceService._fetch_last_transaction_sequence_for_day(
                terminal,
                mode=mode,
                julian_date=julian_date,
                require_success=True,
            )
            results[mode] = result
            if result.get('reason') == 'mra_network_unreachable':
                unreachable_modes.append(mode)
            max_sequence = max(max_sequence, int(result.get('remote_sequence') or 0))

        if unreachable_modes:
            return {
                'checked': False,
                'julian_date': julian_date,
                'max_sequence': 0,
                'reason': 'mra_network_unreachable',
                'unreachable_modes': unreachable_modes,
                'results': results,
            }

        return {
            'checked': True,
            'julian_date': julian_date,
            'max_sequence': max_sequence,
            'results': results,
        }

    @staticmethod
    def _mirror_legacy_terminal_counters(terminal: Terminal, sequence: int) -> None:
        try:
            current_online = int(getattr(terminal, 'online_invoice_counter', 0) or 0)
            current_offline = int(getattr(terminal, 'offline_invoice_counter', 0) or 0)
            terminal.online_invoice_counter = max(current_online, int(sequence or 0))
            terminal.offline_invoice_counter = max(current_offline, int(sequence or 0))
            terminal.save(update_fields=['online_invoice_counter', 'offline_invoice_counter', 'updated_at'])
        except Exception as exc:
            logger.debug('Could not mirror fiscal sequence to legacy terminal counters: %s', exc)

    @staticmethod
    def allocate_fiscal_sequence(terminal: Terminal, invoice_date_time: Any) -> tuple[int, int]:
        julian_date = InvoiceService._to_julian_date(invoice_date_time)
        recovery = InvoiceService.recover_fiscal_sequence_from_mra(terminal, julian_date)
        remote_max = int(recovery.get('max_sequence') or 0)

        for _attempt in range(2):
            try:
                with transaction.atomic():
                    sequence_row, _created = (
                        FiscalInvoiceSequence.objects.select_for_update().get_or_create(
                            terminal=terminal,
                            julian_date=julian_date,
                            defaults={'last_sequence': 0},
                        )
                    )
                    local_max = InvoiceService._local_max_sequence_for_day(terminal, julian_date)
                    recovered_max = max(local_max, remote_max)
                    if recovered_max > sequence_row.last_sequence:
                        sequence_row.last_sequence = recovered_max
                    sequence_row.last_sequence += 1
                    sequence_row.save(update_fields=['last_sequence', 'updated_at'])
                    InvoiceService._mirror_legacy_terminal_counters(terminal, sequence_row.last_sequence)
                    return int(sequence_row.last_sequence), int(julian_date)
            except IntegrityError:
                continue

        raise MRAIntegrationError('Could not allocate a fiscal invoice sequence number. Retry the sale.')

    @staticmethod
    def _build_signature_payload(invoice: MRAInvoice) -> dict[str, Any]:
        gross_amount = Decimal(str(invoice.gross_amount or 0)).quantize(Decimal('0.01'))
        return {
            'invoiceNumber': str(invoice.invoice_number),
            'terminalId': invoice.terminal.mra_terminal_id,
            'sellerTin': invoice.seller_tin,
            'invoiceDate': invoice.invoice_date.isoformat(),
            'grossAmount': format(gross_amount, 'f'),
            'items': invoice.items,
        }

    @staticmethod
    def verify_invoice_hash(invoice: MRAInvoice) -> bool:
        """
        Validate stored invoice signature/hash against canonical invoice data.

        - Online invoice: deterministic SHA256 generated by MRAInvoice.generate_signature()
        - Offline invoice: HMAC/SHA256 signature generated from offline payload + secret policy
        """
        current_signature = str(invoice.invoice_signature or '').strip()
        if not current_signature:
            return False

        if invoice.is_online:
            expected_signature = str(invoice.generate_signature() or '').strip()
        else:
            submitted_payload = (
                (invoice.mra_response or {}).get('payload')
                if isinstance(invoice.mra_response, dict)
                else None
            )
            if isinstance(submitted_payload, dict) and submitted_payload.get('invoiceHeader'):
                expected_signature = str(
                    InvoiceService.build_offline_validation_artifacts_from_payload(
                        submitted_payload,
                        invoice.terminal,
                    ).get('offline_signature') or ''
                ).strip()
            else:
                payload = InvoiceService._build_signature_payload(invoice)
                expected_signature = str(
                    InvoiceService._build_offline_signature(payload, invoice.terminal) or ''
                ).strip()

        if not expected_signature:
            return False

        return hmac.compare_digest(current_signature, expected_signature)

    @staticmethod
    def _build_offline_signature(payload: dict[str, Any], terminal: Terminal | None) -> str:
        canonical_payload = json.dumps(payload, separators=(',', ':'), sort_keys=True, default=str)
        secret = InvoiceService._resolve_signature_secret(terminal)
        if secret:
            return hmac.new(secret.encode('utf-8'), canonical_payload.encode('utf-8'), hashlib.sha256).hexdigest()
        # Fallback for dev mode only.
        return hashlib.sha256(canonical_payload.encode('utf-8')).hexdigest()

    @staticmethod
    def _coerce_datetime(value: Any):
        if hasattr(value, 'date'):
            return value
        if value:
            try:
                return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except (TypeError, ValueError):
                pass
        return timezone.now()

    @staticmethod
    def _base10_to_mra_base64(number: int) -> str:
        chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            return 'A'
        result = ''
        while number > 0:
            number, remainder = divmod(number, 64)
            result = chars[remainder] + result
        return result

    @staticmethod
    def _to_julian_date(value: Any) -> int:
        date_value = InvoiceService._coerce_datetime(value).date()
        year = date_value.year
        month = date_value.month
        day = date_value.day
        if month <= 2:
            year -= 1
            month += 12
        century = year // 100
        correction = 2 - century + (century // 4)
        return int((365.25 * (year + 4716)) // 1 + (30.6001 * (month + 1)) // 1 + day + correction - 1524)

    @staticmethod
    def _format_offline_validation_amount(value: Any) -> str:
        if value is None or value == '':
            return '0'
        if isinstance(value, bool):
            return str(int(value))
        if isinstance(value, (int, float)):
            try:
                return json.dumps(value, allow_nan=False)
            except (TypeError, ValueError):
                return str(value)
        return str(value).strip() or '0'

    @staticmethod
    def build_offline_validation_artifacts(
        *,
        invoice_number: str,
        invoice_date_time: Any,
        item_count: int,
        invoice_total: Any,
        vat_amount: Any,
        terminal: Terminal | None,
    ) -> dict[str, str]:
        julian_date = InvoiceService._to_julian_date(invoice_date_time)
        julian_date_b64 = InvoiceService._base10_to_mra_base64(julian_date)
        params = (
            f'TI={invoice_number}'
            f'&N={max(int(item_count or 0), 0)}'
            f'&I={InvoiceService._format_offline_validation_amount(invoice_total)}'
            f'&V={InvoiceService._format_offline_validation_amount(vat_amount)}'
            f'&T={julian_date_b64}'
        )
        secret = InvoiceService._resolve_signature_secret(terminal)
        if secret:
            digest = hmac.new(secret.encode('utf-8'), params.encode('utf-8'), hashlib.sha256).digest()
            offline_signature = base64.urlsafe_b64encode(digest).decode('utf-8').rstrip('=')
        else:
            offline_signature = hashlib.sha256(params.encode('utf-8')).hexdigest()

        validation_base = str(
            getattr(settings, 'MRA_EIS_OFFLINE_VALIDATION_BASE_URL', '')
            or 'https://dev-eis-portal.mra.mw/ReceiptValidation/Validate/'
        ).rstrip('/')

        return {
            'offline_signature': offline_signature,
            'validation_url': f'{validation_base}?{params}&S={offline_signature}',
            'validation_params': params,
        }

    @staticmethod
    def build_offline_validation_artifacts_from_payload(
        payload: dict[str, Any],
        terminal: Terminal | None,
    ) -> dict[str, str]:
        header = payload.get('invoiceHeader') if isinstance(payload.get('invoiceHeader'), dict) else {}
        summary = payload.get('invoiceSummary') if isinstance(payload.get('invoiceSummary'), dict) else {}
        line_items = payload.get('invoiceLineItems') if isinstance(payload.get('invoiceLineItems'), list) else []
        return InvoiceService.build_offline_validation_artifacts(
            invoice_number=str(header.get('invoiceNumber') or ''),
            invoice_date_time=header.get('invoiceDateTime'),
            item_count=len(line_items),
            invoice_total=summary.get('invoiceTotal'),
            vat_amount=summary.get('totalVAT'),
            terminal=terminal,
        )

    @staticmethod
    def _fetch_last_offline_transaction_snapshot(
        terminal: Terminal,
        *,
        require_success: bool = False,
    ) -> dict[str, Any] | None:
        """Read MRA's last offline transaction state before replay.

        When sequence guarding is required, a failed read blocks replay because
        the next offline sequence cannot be proven safe.
        """
        client = MRAEISClient(terminal=terminal)
        try:
            result = client.call(
                'get_last_offline_transaction',
                payload=None,
                method='POST',
                mutating=False,
            )
            response_data = MRAEISClient._normalize_response_data(result.data)
            meta = response_data.get('_handyPosMeta')
            if not isinstance(meta, dict):
                meta = {}
            meta.update(
                {
                    'endpoint_key': 'get_last_offline_transaction',
                    'endpoint': result.endpoint,
                    'status_code': result.status_code,
                    'dry_run': result.dry_run,
                }
            )
            response_data['_handyPosMeta'] = meta
            return response_data
        except Exception as exc:
            logger.warning(
                'Could not fetch last offline transaction for terminal %s: %s',
                terminal.terminal_id,
                exc,
            )
            if require_success:
                raise MRAIntegrationError(
                    f'Unable to verify MRA last offline transaction before replay: {exc}'
                ) from exc
            return None

    @staticmethod
    def _extract_invoice_number_from_mra_response(response_data: dict[str, Any] | None) -> str:
        if not isinstance(response_data, dict):
            return ''
        data = response_data.get('data')
        inner = data if isinstance(data, dict) else response_data
        header = inner.get('invoiceHeader') if isinstance(inner.get('invoiceHeader'), dict) else {}
        return str(
            header.get('invoiceNumber')
            or inner.get('invoiceNumber')
            or inner.get('receiptNumber')
            or response_data.get('invoiceNumber')
            or response_data.get('receiptNumber')
            or ''
        ).strip()

    @staticmethod
    def _offline_invoice_sequence(invoice: MRAInvoice) -> tuple[int, str]:
        response_data = invoice.mra_response if isinstance(invoice.mra_response, dict) else {}
        payload = response_data.get('payload') if isinstance(response_data.get('payload'), dict) else {}
        header = payload.get('invoiceHeader') if isinstance(payload.get('invoiceHeader'), dict) else {}
        fiscal_invoice_number = str(
            header.get('invoiceNumber')
            or response_data.get('fiscal_invoice_number')
            or ''
        ).strip()
        sequence = (
            POSOrderSubmissionService._extract_sequence_from_fiscal_number(fiscal_invoice_number)
            if fiscal_invoice_number
            else 0
        )
        if sequence <= 0:
            try:
                sequence = int(invoice.invoice_number or 0)
            except (TypeError, ValueError):
                sequence = 0
        return sequence, fiscal_invoice_number or str(invoice.invoice_number or '')

    @staticmethod
    def _validate_offline_replay_sequence_guard(
        terminal: Terminal,
        first_entry: OfflineInvoiceQueue | None,
        last_offline_snapshot: dict[str, Any] | None,
        queued_entries=None,
    ) -> dict[str, Any]:
        if not bool(getattr(settings, 'MRA_EIS_REQUIRE_OFFLINE_REPLAY_SEQUENCE_GUARD', True)):
            return {'checked': False, 'reason': 'disabled'}
        if first_entry is None:
            return {'checked': False, 'reason': 'empty_queue'}

        if not isinstance(last_offline_snapshot, dict):
            raise MRAIntegrationError('Unable to verify MRA last offline transaction before replay.')

        meta = last_offline_snapshot.get('_handyPosMeta')
        if isinstance(meta, dict) and meta.get('dry_run'):
            return {'checked': False, 'reason': 'dry_run', 'last_offline_transaction': last_offline_snapshot}
        if str(last_offline_snapshot.get('status') or '').lower() == 'prepared':
            return {'checked': False, 'reason': 'dry_run', 'last_offline_transaction': last_offline_snapshot}

        response_errors = _extract_mra_response_errors(last_offline_snapshot)
        if response_errors:
            raise MRAIntegrationError(
                'Unable to verify MRA last offline transaction before replay: '
                + '; '.join(response_errors)
            )

        remote_invoice_number = InvoiceService._extract_invoice_number_from_mra_response(last_offline_snapshot)
        remote_sequence = 0
        if remote_invoice_number:
            remote_sequence = POSOrderSubmissionService._extract_sequence_from_fiscal_number(remote_invoice_number)
            if remote_sequence <= 0:
                raise MRAIntegrationError(
                    'Unable to decode MRA last offline invoice sequence before replay: '
                    f'{remote_invoice_number}'
                )

        expected_next_sequence = remote_sequence + 1
        entries_to_check = list(queued_entries) if queued_entries is not None else [first_entry]
        queue_sequence_plan: list[dict[str, Any]] = []
        expected_sequence = expected_next_sequence

        for index, queued_entry in enumerate(entries_to_check):
            local_sequence, local_invoice_number = InvoiceService._offline_invoice_sequence(queued_entry.mra_invoice)
            if local_sequence <= 0:
                raise MRAIntegrationError(
                    'Unable to determine next queued offline invoice sequence before replay.'
                )
            if local_sequence != expected_sequence:
                if index == 0:
                    raise MRAIntegrationError(
                        'Offline replay sequence mismatch: MRA last offline sequence is '
                        f'{remote_sequence}; expected next offline sequence {expected_sequence} '
                        f'but next queued offline sequence is {local_sequence}. '
                        'Run last-offline reconciliation before replaying queued sales.'
                    )
                raise MRAIntegrationError(
                    'Offline replay sequence mismatch: queued offline invoice at position '
                    f'{queued_entry.queue_position} has sequence {local_sequence}; '
                    f'expected contiguous sequence {expected_sequence}. '
                    'Fix/reconcile the offline queue before replaying queued sales.'
                )
            queue_sequence_plan.append(
                {
                    'queue_position': queued_entry.queue_position,
                    'invoice_id': str(queued_entry.mra_invoice_id),
                    'invoice_number': local_invoice_number,
                    'sequence': local_sequence,
                }
            )
            expected_sequence += 1

        local_counter = int(terminal.offline_invoice_counter or 0)
        highest_queued_sequence = queue_sequence_plan[-1]['sequence'] if queue_sequence_plan else 0
        if remote_sequence > local_counter:
            raise MRAIntegrationError(
                'Offline replay sequence mismatch: MRA last offline sequence '
                f'({remote_sequence}) is ahead of local terminal offline counter ({local_counter}). '
                'Run last-offline reconciliation before replaying queued sales.'
            )
        if highest_queued_sequence > local_counter:
            raise MRAIntegrationError(
                'Offline replay sequence mismatch: local terminal offline counter '
                f'({local_counter}) is behind highest queued offline sequence ({highest_queued_sequence}).'
            )

        return {
            'checked': True,
            'remote_invoice_number': remote_invoice_number,
            'remote_sequence': remote_sequence,
            'expected_next_sequence': expected_next_sequence,
            'terminal_offline_counter': local_counter,
            'queue_sequence_plan': queue_sequence_plan,
        }

    @staticmethod
    def _sum_queued_offline_gross_amount(terminal: Terminal) -> Decimal:
        queued_total = (
            OfflineInvoiceQueue.objects.filter(
                terminal=terminal,
                status__in=['queued', 'syncing', 'failed'],
            )
            .aggregate(total=Sum('mra_invoice__gross_amount'))
            .get('total')
        )
        return queued_total if isinstance(queued_total, Decimal) else Decimal('0')

    @staticmethod
    def _calculate_amounts(items: list[dict[str, Any]]) -> tuple[Decimal, Decimal, Decimal, dict[str, Decimal]]:
        net_amount = Decimal('0')
        tax_amount = Decimal('0')
        tax_breakdown = {
            'standard': Decimal('0'),
            'zero': Decimal('0'),
            'exempt': Decimal('0'),
        }

        for item in items:
            quantity = Decimal(str(item.get('quantity', 0)))
            unit_price = Decimal(str(item.get('unit_price', 0)))
            item_net = max(Decimal('0'), quantity * unit_price)
            net_amount += item_net

            tax_category = item.get('tax_category', 'standard')
            tax_rate = Decimal(str(item.get('tax_rate', 0)))

            item_tax = Decimal('0')
            if tax_category not in {'exempt', 'zero'} and tax_rate > 0:
                item_tax = item_net * (tax_rate / Decimal('100'))

            tax_amount += item_tax
            if tax_category in tax_breakdown:
                tax_breakdown[tax_category] += item_tax

        gross_amount = net_amount + tax_amount
        return net_amount, tax_amount, gross_amount, tax_breakdown

    @staticmethod
    @transaction.atomic
    def create_invoice(
        terminal,
        seller_tin,
        seller_name,
        items,
        buyer_tin=None,
        buyer_name=None,
        is_online=True,
    ):
        net_amount, tax_amount, gross_amount, tax_breakdown = InvoiceService._calculate_amounts(items)
        stored_items = InvoiceService._to_json_safe(items)

        invoice_date = timezone.now()
        invoice_number, fiscal_julian_date = InvoiceService.allocate_fiscal_sequence(
            terminal,
            invoice_date,
        )

        invoice = MRAInvoice.objects.create(
            business=terminal.business,
            branch=terminal.branch,
            terminal=terminal,
            invoice_number=invoice_number,
            fiscal_julian_date=fiscal_julian_date,
            seller_tin=seller_tin,
            seller_name=seller_name,
            buyer_tin=buyer_tin or '',
            buyer_name=buyer_name or '',
            items=stored_items,
            net_amount=net_amount,
            tax_amount=tax_amount,
            gross_amount=gross_amount,
            tax_breakdown={k: str(v) for k, v in tax_breakdown.items()},
            is_online=is_online,
            invoice_date=invoice_date,
            status='draft',
        )

        if is_online:
            invoice.invoice_signature = invoice.generate_signature()
        else:
            signature_payload = InvoiceService._build_signature_payload(invoice)
            invoice.invoice_signature = InvoiceService._build_offline_signature(
                signature_payload,
                terminal,
            )
        invoice.save(update_fields=['invoice_signature', 'updated_at'])

        InvoiceAuditLog.objects.create(
            mra_invoice=invoice,
            action='created',
            details={
                'seller_tin': seller_tin,
                'gross_amount': str(gross_amount),
                'is_online': is_online,
            },
        )

        return invoice

    @staticmethod
    def _format_decimal(value: Any, places: str = '0.01') -> float:
        try:
            return float(Decimal(str(value or 0)).quantize(Decimal(places)))
        except (InvalidOperation, TypeError, ValueError):
            return float(Decimal('0').quantize(Decimal(places)))

    @staticmethod
    def _build_tax_breakdown(business, line_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Decimal]] = {}
        for item in line_items:
            rate_id = str(item.get('taxRateId') or 'T')
            if rate_id not in grouped:
                grouped[rate_id] = {'taxableAmount': Decimal('0'), 'taxAmount': Decimal('0')}
            grouped[rate_id]['taxableAmount'] += Decimal(str(item.get('total') or 0))
            grouped[rate_id]['taxAmount'] += Decimal(str(item.get('totalVAT') or 0))

        return [
            {
                'rateId': rate_id,
                'taxableAmount': InvoiceService._format_decimal(values['taxableAmount']),
                'taxAmount': InvoiceService._format_decimal(values['taxAmount']),
            }
            for rate_id, values in grouped.items()
        ]

    @staticmethod
    def _resolve_line_levies(business, raw_levies: Any) -> list[dict[str, Any]]:
        return ProductMappingService.normalize_levies(raw_levies, business=business)

    @staticmethod
    def _build_levy_breakdown(business, levy_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Decimal | str]] = {}
        for line in levy_lines:
            try:
                taxable_amount = Decimal(str(line.get('taxableAmount') or line.get('total') or 0))
            except (InvalidOperation, TypeError, ValueError):
                taxable_amount = Decimal('0')
            if taxable_amount <= 0:
                continue

            for levy in InvoiceService._resolve_line_levies(business, line.get('levies')):
                levy_type_id = str(levy.get('levyTypeId') or '').strip()
                if not levy_type_id:
                    continue
                try:
                    levy_rate = Decimal(str(levy.get('levyRate') or 0)).quantize(Decimal('0.01'))
                except (InvalidOperation, TypeError, ValueError):
                    levy_rate = Decimal('0.00')
                levy_amount = taxable_amount * (levy_rate / Decimal('100'))
                key = (levy_type_id, str(levy_rate))
                if key not in grouped:
                    grouped[key] = {
                        'levyTypeId': levy_type_id,
                        'levyRate': levy_rate,
                        'levyAmount': Decimal('0'),
                    }
                grouped[key]['levyAmount'] = Decimal(str(grouped[key]['levyAmount'])) + levy_amount

        return [
            {
                'levyTypeId': str(values['levyTypeId']),
                'levyRate': InvoiceService._format_decimal(values['levyRate']),
                'levyAmount': InvoiceService._format_decimal(values['levyAmount']),
            }
            for values in grouped.values()
        ]

    @staticmethod
    def _sum_levy_breakdown(levy_breakdown: list[dict[str, Any]]) -> Decimal:
        total = Decimal('0')
        for row in levy_breakdown or []:
            try:
                total += Decimal(str(row.get('levyAmount') or 0))
            except (InvalidOperation, TypeError, ValueError):
                continue
        return total.quantize(Decimal('0.01'))

    @staticmethod
    def _build_mra_invoice_payload(invoice: MRAInvoice) -> dict[str, Any]:
        versions = ConfigurationService.get_config_versions(invoice.business)
        site_id = ConfigurationService.get_terminal_site_id(invoice.business, invoice.branch)
        seller_tin = ConfigurationService.get_taxpayer_tin(invoice.business) or invoice.seller_tin

        def first_present(*values):
            for value in values:
                if value is not None and value != '':
                    return value
            return None

        line_items: list[dict[str, Any]] = []
        levy_lines: list[dict[str, Any]] = []
        for index, item in enumerate(invoice.items or [], start=1):
            quantity = Decimal(str(first_present(item.get('quantity'), item.get('qty'), 0)))
            unit_price = Decimal(str(first_present(item.get('unit_price'), item.get('unitPrice'), 0)))
            total = Decimal(
                str(
                    first_present(
                        item.get('lineNetAmount'),
                        item.get('subtotal'),
                        item.get('total'),
                        quantity * unit_price,
                    )
                )
            )
            total_vat = Decimal(
                str(first_present(item.get('lineTaxAmount'), item.get('tax_amount'), item.get('totalVAT'), 0))
            )
            tax_category = item.get('tax_category') or item.get('taxType') or item.get('tax_type')
            tax_rate = item.get('tax_rate') or item.get('taxRate') or 0
            tax_rate_id = (
                item.get('taxRateId')
                or item.get('tax_rate_id')
                or ConfigurationService.resolve_tax_rate_id(invoice.business, tax_rate, tax_category)
            )
            line_items.append(
                {
                    'id': index,
                    'productCode': item.get('mra_product_code') or item.get('productCode') or '',
                    'description': item.get('name') or item.get('description') or '',
                    'unitPrice': InvoiceService._format_decimal(unit_price),
                    'quantity': InvoiceService._format_decimal(quantity, '0.001'),
                    'discount': InvoiceService._format_decimal(0),
                    'total': InvoiceService._format_decimal(total),
                    'totalVAT': InvoiceService._format_decimal(total_vat),
                    'taxRateId': str(tax_rate_id),
                    'isProduct': bool(item.get('isProduct', True)),
                }
            )
            levy_lines.append(
                {
                    'taxableAmount': total,
                    'levies': item.get('mra_levies') or item.get('levies') or item.get('productLevies') or [],
                }
            )

        original_payload = (
            invoice.mra_response.get('payload')
            if isinstance(invoice.mra_response, dict)
            else None
        )
        original_header = (
            original_payload.get('invoiceHeader')
            if isinstance(original_payload, dict) and isinstance(original_payload.get('invoiceHeader'), dict)
            else {}
        )
        original_summary = (
            original_payload.get('invoiceSummary')
            if isinstance(original_payload, dict) and isinstance(original_payload.get('invoiceSummary'), dict)
            else {}
        )
        buyer_tin = str(invoice.buyer_tin or original_header.get('buyerTIN') or '').strip()
        buyer_name = str(invoice.buyer_name or original_header.get('buyerName') or '').strip()
        invoice_header = {
            'invoiceNumber': str(original_header.get('invoiceNumber') or invoice.invoice_number),
            'invoiceDateTime': str(original_header.get('invoiceDateTime') or invoice.invoice_date.isoformat()),
            'sellerTIN': str(original_header.get('sellerTIN') or seller_tin),
            'siteId': str(original_header.get('siteId') or site_id),
            'globalConfigVersion': original_header.get('globalConfigVersion', versions['global']),
            'taxpayerConfigVersion': original_header.get('taxpayerConfigVersion', versions['taxpayer']),
            'terminalConfigVersion': original_header.get('terminalConfigVersion', versions['terminal']),
            'isExport': bool(original_header.get('isExport', False)),
            'isReliefSupply': bool(original_header.get('isReliefSupply', False)),
            'paymentMethod': POSOrderSubmissionService._normalize_payment_method_for_mra(
                original_header.get('paymentMethod') or 'Cash'
            ),
        }
        if buyer_tin:
            invoice_header['buyerTIN'] = buyer_tin
        if buyer_name:
            invoice_header['buyerName'] = buyer_name
        buyer_authorization_code = str(original_header.get('buyerAuthorizationCode') or '').strip()
        if buyer_authorization_code:
            invoice_header['buyerAuthorizationCode'] = buyer_authorization_code
        vat5_details = original_header.get('vat5CertificateDetails')
        if invoice_header['isReliefSupply'] and isinstance(vat5_details, dict):
            invoice_header['vat5CertificateDetails'] = vat5_details

        levy_breakdown = (
            original_summary.get('levyBreakDown')
            if isinstance(original_summary.get('levyBreakDown'), list)
            else InvoiceService._build_levy_breakdown(invoice.business, levy_lines)
        )
        levy_amount = InvoiceService._sum_levy_breakdown(levy_breakdown)
        invoice_total = Decimal(str(invoice.gross_amount or 0)).quantize(Decimal('0.01'))
        if 'invoiceTotal' in original_summary:
            invoice_total = Decimal(str(original_summary.get('invoiceTotal') or invoice_total)).quantize(Decimal('0.01'))
        elif levy_amount:
            invoice_total = (invoice_total + levy_amount).quantize(Decimal('0.01'))
        amount_tendered = Decimal(str(original_summary.get('amountTendered') or invoice_total)).quantize(Decimal('0.01'))

        payload = {
            'invoiceHeader': invoice_header,
            'invoiceLineItems': line_items,
            'invoiceSummary': {
                'taxBreakDown': InvoiceService._build_tax_breakdown(invoice.business, line_items),
                'levyBreakDown': levy_breakdown,
                'totalVAT': InvoiceService._format_decimal(invoice.tax_amount),
                'invoiceTotal': InvoiceService._format_decimal(invoice_total),
                'amountTendered': InvoiceService._format_decimal(amount_tendered),
            },
        }
        if not invoice.is_online:
            offline_artifacts = InvoiceService.build_offline_validation_artifacts_from_payload(
                payload,
                invoice.terminal,
            )
            payload['invoiceSummary']['offlineSignature'] = offline_artifacts['offline_signature']
        return payload

    @staticmethod
    def _extract_validation_url(response_data: dict[str, Any]) -> str:
        if not isinstance(response_data, dict):
            return ''
        response_inner = response_data.get('data') if isinstance(response_data.get('data'), dict) else {}
        return str(
            response_inner.get('validationURL')
            or response_inner.get('validationUrl')
            or response_data.get('validationURL')
            or response_data.get('validationUrl')
            or ''
        ).strip()

    @staticmethod
    def _extract_remote_invoice_identifier(response_data: dict[str, Any], fallback: str = '') -> str:
        if not isinstance(response_data, dict):
            return str(fallback or '').strip()
        response_inner = response_data.get('data') if isinstance(response_data.get('data'), dict) else {}
        return str(
            response_inner.get('invoiceId')
            or response_inner.get('invoiceID')
            or response_inner.get('invoice_id')
            or response_inner.get('eisUuid')
            or response_inner.get('eisUUID')
            or response_inner.get('eis_uuid')
            or response_inner.get('transactionId')
            or response_inner.get('transactionID')
            or response_inner.get('transaction_id')
            or response_data.get('invoiceId')
            or response_data.get('invoiceID')
            or response_data.get('invoice_id')
            or response_data.get('eisUuid')
            or response_data.get('eisUUID')
            or response_data.get('eis_uuid')
            or response_data.get('transactionId')
            or response_data.get('transactionID')
            or response_data.get('transaction_id')
            or fallback
            or ''
        ).strip()

    @staticmethod
    def _mark_related_pos_order_submitted(
        invoice: MRAInvoice,
        *,
        validation_url: str,
        response_data: dict[str, Any],
    ) -> bool:
        previous_response = invoice.mra_response if isinstance(invoice.mra_response, dict) else {}
        local_metadata = (
            previous_response.get('local_metadata')
            if isinstance(previous_response.get('local_metadata'), dict)
            else {}
        )
        previous_payload = (
            previous_response.get('payload')
            if isinstance(previous_response.get('payload'), dict)
            else {}
        )
        previous_header = (
            previous_payload.get('invoiceHeader')
            if isinstance(previous_payload.get('invoiceHeader'), dict)
            else {}
        )
        order_id = str(
            previous_response.get('order_id')
            or local_metadata.get('orderId')
            or local_metadata.get('order_id')
            or ''
        ).strip()
        fiscal_invoice_number = str(previous_header.get('invoiceNumber') or '').strip()

        try:
            from pos_sessions.models import Order
        except Exception as exc:
            logger.warning('Could not import POS order model for invoice replay confirmation: %s', exc)
            return False

        queryset = Order.objects.filter(business=invoice.business, branch=invoice.branch)
        order = queryset.filter(pk=order_id).first() if order_id else None
        if order is None and fiscal_invoice_number:
            order = queryset.filter(fiscal_invoice_number=fiscal_invoice_number).first()
        if order is None:
            logger.warning(
                'Could not find POS order for submitted MRA invoice %s during replay confirmation',
                invoice.id,
            )
            return False

        now = timezone.now()
        remote_identifier = InvoiceService._extract_remote_invoice_identifier(
            response_data,
            fallback=invoice.mra_invoice_id or '',
        )
        update_values = {
            'eis_status': 'SUBMITTED',
            'eis_submitted_at': now,
            'is_fiscal_locked': True,
            'is_dirty': False,
            'updated_at': now,
        }
        if remote_identifier:
            update_values['eis_uuid'] = remote_identifier[:100]
        if validation_url:
            update_values['qr_code_payload'] = validation_url
        if invoice.invoice_signature and not order.digital_signature:
            update_values['digital_signature'] = invoice.invoice_signature

        Order.objects.filter(pk=order.pk).update(**update_values)
        return True

    @staticmethod
    @transaction.atomic
    def submit_invoice(invoice):
        endpoint_key = 'report_sale' if invoice.is_online else 'report_sale_offline'
        payload = InvoiceService._build_mra_invoice_payload(invoice)

        try:
            client = MRAEISClient(terminal=invoice.terminal)
            result = client.call(endpoint_key, payload=payload, method='POST', mutating=True)
            response_data = MRAEISClient._normalize_response_data(result.data)
            response_inner = response_data.get('data') if isinstance(response_data.get('data'), dict) else {}
            validation_url = InvoiceService._extract_validation_url(response_data)
            response_errors = _extract_mra_response_errors(response_data)
            previous_response = invoice.mra_response if isinstance(invoice.mra_response, dict) else {}
            preserved_response = {
                key: previous_response[key]
                for key in ('source', 'order_id', 'local_metadata')
                if key in previous_response
            }

            invoice.mra_invoice_id = (
                InvoiceService._extract_remote_invoice_identifier(response_data)
                or validation_url
                or invoice.mra_invoice_id
            )
            invoice.status = 'rejected' if (response_errors and not result.dry_run) else 'submitted'
            invoice.submitted_at = timezone.now() if not result.dry_run else None
            invoice.mra_response = {
                **preserved_response,
                'dry_run': result.dry_run,
                'endpoint': result.endpoint,
                'payload': payload,
                'response': response_data,
                'errors': response_errors,
            }
            invoice.save(update_fields=['mra_invoice_id', 'status', 'submitted_at', 'mra_response', 'updated_at'])

            if response_errors and not result.dry_run:
                InvoiceAuditLog.objects.create(
                    mra_invoice=invoice,
                    action='rejected',
                    details={
                        'endpoint': result.endpoint,
                        'errors': response_errors,
                    },
                )
                raise MRAIntegrationError(f"MRA rejected invoice: {'; '.join(response_errors)}")

            if not result.dry_run:
                InvoiceService._mark_related_pos_order_submitted(
                    invoice,
                    validation_url=validation_url,
                    response_data=response_data,
                )

            if ConfigurationService.response_requests_latest_config(response_data):
                try:
                    ConfigurationService.fetch_and_store_configuration(
                        invoice.business,
                        terminal=invoice.terminal,
                    )
                except Exception as config_exc:
                    logger.warning('Latest config sync after invoice response failed: %s', config_exc)

            InvoiceAuditLog.objects.create(
                mra_invoice=invoice,
                action='submitted',
                details={
                    'dry_run': result.dry_run,
                    'endpoint': result.endpoint,
                    'mra_invoice_id': invoice.mra_invoice_id,
                },
            )

            return invoice
        except Exception as exc:
            MRAAPIError.objects.create(
                terminal=invoice.terminal,
                error_type='invalid_request',
                error_message=str(exc),
                related_invoice=invoice,
            )
            raise

    @staticmethod
    @transaction.atomic
    def queue_offline_invoice(invoice):
        if invoice.is_online:
            raise ValueError('Cannot queue online invoice')

        existing_entry = OfflineInvoiceQueue.objects.filter(mra_invoice=invoice).first()
        if existing_entry:
            if existing_entry.status != 'queued':
                existing_entry.status = 'queued'
                existing_entry.save(update_fields=['status'])
            if invoice.status != 'offline_queued':
                invoice.status = 'offline_queued'
                invoice.save(update_fields=['status', 'updated_at'])
            return existing_entry

        limits = ConfigurationService.get_offline_limits(invoice.business)
        if limits.max_transaction_age_hours is not None:
            age_hours = (timezone.now() - invoice.invoice_date).total_seconds() / 3600
            if age_hours > float(limits.max_transaction_age_hours):
                raise MRAIntegrationError(
                    'Offline transaction age exceeds configured limit '
                    f'({age_hours:.2f}h > {limits.max_transaction_age_hours}h).'
                )

        if limits.max_cumulative_amount is not None:
            queued_total = InvoiceService._sum_queued_offline_gross_amount(invoice.terminal)
            projected_total = queued_total + Decimal(str(invoice.gross_amount or 0))
            if projected_total > limits.max_cumulative_amount:
                raise MRAIntegrationError(
                    'Offline cumulative amount exceeds configured limit '
                    f'({projected_total} > {limits.max_cumulative_amount}).'
                )

        last_entry = (
            OfflineInvoiceQueue.objects.filter(terminal=invoice.terminal)
            .order_by('-queue_position')
            .first()
        )
        queue_position = (last_entry.queue_position + 1) if last_entry else 1

        queue_entry = OfflineInvoiceQueue.objects.create(
            terminal=invoice.terminal,
            mra_invoice=invoice,
            queue_position=queue_position,
            status='queued',
        )

        invoice.status = 'offline_queued'
        invoice.save(update_fields=['status', 'updated_at'])

        OfflineAuditLog.objects.create(
            terminal=invoice.terminal,
            event_type='invoice_queued',
            details={
                'invoice_number': invoice.invoice_number,
                'queue_position': queue_position,
            },
        )

        return queue_entry

    @staticmethod
    @transaction.atomic
    def sync_offline_invoices(terminal):
        queued_entries = OfflineInvoiceQueue.objects.filter(
            terminal=terminal,
            status__in=['queued', 'failed'],
        ).order_by('queue_position')

        synced_count = 0
        failed_count = 0
        offline_limits = ConfigurationService.get_offline_limits(terminal.business)
        first_entry = queued_entries.first()
        queue_count = queued_entries.count()
        last_offline_snapshot = None
        sequence_guard: dict[str, Any] = {'checked': False, 'reason': 'empty_queue'}

        logger.warning(
            '[MRA REPLAY] start terminal_pk=%s terminal_id=%s queued_or_failed=%s is_online=%s',
            terminal.pk,
            terminal.terminal_id,
            queue_count,
            terminal.is_online,
        )

        if queue_count == 0:
            logger.warning(
                '[MRA REPLAY] no queued invoices terminal_pk=%s terminal_id=%s',
                terminal.pk,
                terminal.terminal_id,
            )

        if first_entry is not None:
            try:
                last_offline_snapshot = InvoiceService._fetch_last_offline_transaction_snapshot(
                    terminal,
                    require_success=bool(
                        getattr(settings, 'MRA_EIS_REQUIRE_OFFLINE_REPLAY_SEQUENCE_GUARD', True)
                    ),
                )
                sequence_guard = InvoiceService._validate_offline_replay_sequence_guard(
                    terminal,
                    first_entry,
                    last_offline_snapshot,
                    queued_entries=queued_entries,
                )
            except Exception as exc:
                logger.exception(
                    '[MRA REPLAY] sequence guard blocked terminal_pk=%s terminal_id=%s '
                    'queue_entry=%s invoice=%s error=%s',
                    terminal.pk,
                    terminal.terminal_id,
                    first_entry.id,
                    first_entry.mra_invoice.invoice_number,
                    exc,
                )
                first_entry.status = 'failed'
                first_entry.last_sync_error = str(exc)
                first_entry.sync_attempts += 1
                first_entry.last_sync_attempt_at = timezone.now()
                first_entry.save(
                    update_fields=['status', 'last_sync_error', 'sync_attempts', 'last_sync_attempt_at']
                )
                terminal.last_sync_at = timezone.now()
                terminal.save(update_fields=['last_sync_at', 'updated_at'])
                sequence_guard = {
                    'checked': True,
                    'blocked': True,
                    'error': str(exc),
                    'last_offline_transaction': last_offline_snapshot,
                    'queue_entry_id': str(first_entry.id),
                    'invoice_id': str(first_entry.mra_invoice_id),
                }
                OfflineAuditLog.objects.create(
                    terminal=terminal,
                    event_type='sync_failed',
                    details={
                        'reason': 'offline_replay_sequence_guard',
                        'error': str(exc),
                        'sequence_guard': sequence_guard,
                        'offline_limit_source': offline_limits.source,
                    },
                )
                return {
                    'synced': 0,
                    'failed': 1,
                    'blocked': True,
                    'error': str(exc),
                    'sequence_guard': sequence_guard,
                }

        for entry in queued_entries:
            try:
                logger.warning(
                    '[MRA REPLAY] attempting queue_entry=%s terminal_id=%s position=%s '
                    'invoice=%s attempts_before=%s',
                    entry.id,
                    terminal.terminal_id,
                    entry.queue_position,
                    entry.mra_invoice.invoice_number,
                    entry.sync_attempts,
                )
                entry.status = 'syncing'
                entry.last_sync_attempt_at = timezone.now()
                entry.save(update_fields=['status', 'last_sync_attempt_at'])

                if offline_limits.max_transaction_age_hours is not None:
                    age_hours = (
                        timezone.now() - entry.mra_invoice.invoice_date
                    ).total_seconds() / 3600
                    if age_hours > float(offline_limits.max_transaction_age_hours):
                        raise MRAIntegrationError(
                            'Offline transaction age exceeds configured limit '
                            f'({age_hours:.2f}h > {offline_limits.max_transaction_age_hours}h).'
                        )

                InvoiceService.submit_invoice(entry.mra_invoice)

                entry.status = 'synced'
                entry.synced_at = timezone.now()
                entry.mra_invoice.status = 'offline_synced'
                entry.mra_invoice.save(update_fields=['status', 'updated_at'])
                entry.save(update_fields=['status', 'synced_at'])
                try:
                    from .receipt import ReceiptService

                    ReceiptService.generate_receipt(entry.mra_invoice, force_refresh=True)
                except Exception as receipt_exc:
                    logger.warning(
                        'Failed to refresh receipt after offline invoice sync %s: %s',
                        entry.mra_invoice_id,
                        receipt_exc,
                    )

                synced_count += 1
                logger.warning(
                    '[MRA REPLAY] synced queue_entry=%s terminal_id=%s position=%s invoice=%s',
                    entry.id,
                    terminal.terminal_id,
                    entry.queue_position,
                    entry.mra_invoice.invoice_number,
                )
            except Exception as exc:
                entry.status = 'failed'
                entry.last_sync_error = str(exc)
                entry.sync_attempts += 1
                entry.last_sync_attempt_at = timezone.now()
                entry.save(
                    update_fields=['status', 'last_sync_error', 'sync_attempts', 'last_sync_attempt_at']
                )
                failed_count += 1
                logger.exception(
                    '[MRA REPLAY] failed queue_entry=%s terminal_id=%s position=%s '
                    'invoice=%s attempts_now=%s error=%s',
                    entry.id,
                    terminal.terminal_id,
                    entry.queue_position,
                    entry.mra_invoice.invoice_number,
                    entry.sync_attempts,
                    exc,
                )

        terminal.last_sync_at = timezone.now()
        terminal.save(update_fields=['last_sync_at', 'updated_at'])

        OfflineAuditLog.objects.create(
            terminal=terminal,
            event_type='sync_completed',
            details={
                'synced_count': synced_count,
                'failed_count': failed_count,
                'offline_limit_source': offline_limits.source,
                'max_transaction_age_hours': offline_limits.max_transaction_age_hours,
                'max_cumulative_amount': (
                    str(offline_limits.max_cumulative_amount)
                    if offline_limits.max_cumulative_amount is not None
                    else None
                ),
                'last_offline_transaction': last_offline_snapshot,
                'sequence_guard': sequence_guard,
            },
        )

        logger.warning(
            '[MRA REPLAY] complete terminal_pk=%s terminal_id=%s synced=%s failed=%s',
            terminal.pk,
            terminal.terminal_id,
            synced_count,
            failed_count,
        )

        return {'synced': synced_count, 'failed': failed_count}


class EISSaleComplianceService:
    """Utility validations for optional EIS sale header fields."""

    @staticmethod
    def _response_inner(response_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response_data, dict):
            return {}
        data = response_data.get('data')
        return data if isinstance(data, dict) else response_data

    @staticmethod
    def _active_terminal_for_business(business, branch=None) -> Terminal | None:
        queryset = Terminal.objects.filter(business=business, status='active')
        if branch is not None:
            queryset = queryset.filter(branch=branch)
        return queryset.exclude(mra_token='').order_by('-updated_at').first() or queryset.order_by('-updated_at').first()

    @staticmethod
    def _clean(value: Any, max_length: int = 100) -> str:
        return str(value or '').strip()[:max_length]

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value or 0))
            return parsed if parsed.is_finite() else Decimal('0')
        except (InvalidOperation, TypeError, ValueError):
            return Decimal('0')

    @staticmethod
    def _is_positive_status(response_data: dict[str, Any]) -> bool:
        try:
            return int(response_data.get('statusCode') or response_data.get('status_code') or 0) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def check_tin_authorization_requirement(*, business, tin: str, terminal: Terminal | None = None) -> dict[str, Any]:
        tin = EISSaleComplianceService._clean(tin, 50)
        if not tin:
            raise ValueError('Buyer TIN is required')
        terminal = terminal or EISSaleComplianceService._active_terminal_for_business(business)
        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'check_tin_authorization_requirement',
            payload={'tin': tin},
            method='POST',
            mutating=False,
        )
        response_data = result.data or {}
        inner = EISSaleComplianceService._response_inner(response_data)
        response_errors = _extract_mra_response_errors(response_data)
        if result.dry_run:
            return {
                'checked': False,
                'dry_run': True,
                'tin': tin,
                'tin_exists': None,
                'requires_authorization_code': False,
                'response': response_data,
            }
        return {
            'checked': True,
            'dry_run': False,
            'tin': inner.get('tin') or tin,
            'tin_exists': inner.get('tinExists'),
            'requires_authorization_code': bool(inner.get('requiresAuthorizationCode')),
            'response': response_data,
            'errors': response_errors,
        }

    @staticmethod
    def validate_authorization_code(
        *,
        business,
        authorization_code: str,
        terminal: Terminal | None = None,
    ) -> dict[str, Any]:
        authorization_code = EISSaleComplianceService._clean(authorization_code, 100)
        if not authorization_code:
            raise ValueError('Buyer authorization code is required')
        terminal = terminal or EISSaleComplianceService._active_terminal_for_business(business)
        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'validate_authorization_code',
            payload={'authorizationCode': authorization_code},
            method='POST',
            mutating=False,
        )
        response_data = result.data or {}
        inner = EISSaleComplianceService._response_inner(response_data)
        response_errors = _extract_mra_response_errors(response_data)
        is_valid = inner.get('isValidAuthorizationCode')
        if is_valid is None:
            is_valid = EISSaleComplianceService._is_positive_status(response_data) and not response_errors
        if result.dry_run:
            is_valid = True
        return {
            'checked': not result.dry_run,
            'dry_run': result.dry_run,
            'is_valid': bool(is_valid),
            'response': response_data,
            'errors': response_errors,
        }

    @staticmethod
    def validate_vat5_certificate(
        *,
        business,
        project_number: str,
        certificate_number: str,
        quantity: Any,
        terminal: Terminal | None = None,
    ) -> dict[str, Any]:
        project_number = EISSaleComplianceService._clean(project_number, 100)
        certificate_number = EISSaleComplianceService._clean(certificate_number, 100)
        quantity_value = EISSaleComplianceService._to_decimal(quantity)
        if not project_number:
            raise ValueError('VAT5 project number is required')
        if not certificate_number:
            raise ValueError('VAT5 certificate number is required')
        if quantity_value <= 0:
            raise ValueError('VAT5 quantity must be greater than zero')
        terminal = terminal or EISSaleComplianceService._active_terminal_for_business(business)
        client = MRAEISClient(terminal=terminal)
        result = client.call(
            'validate_vat5',
            payload={
                'projectNumber': project_number,
                'certificateNumber': certificate_number,
                'quantity': float(quantity_value.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)),
            },
            method='POST',
            mutating=False,
        )
        response_data = result.data or {}
        inner = EISSaleComplianceService._response_inner(response_data)
        response_errors = _extract_mra_response_errors(response_data)
        is_valid = inner.get('isValid')
        if is_valid is None:
            is_valid = EISSaleComplianceService._is_positive_status(response_data) and not response_errors
        if result.dry_run:
            is_valid = True
        return {
            'checked': not result.dry_run,
            'dry_run': result.dry_run,
            'is_valid': bool(is_valid),
            'response': response_data,
            'errors': response_errors,
        }

    @staticmethod
    def validate_order_special_fields(order, terminal: Terminal, buyer_tin: str) -> dict[str, Any]:
        metadata = dict(getattr(order, 'eis_validation_metadata', None) or {})
        result: dict[str, Any] = {}
        buyer_tin = EISSaleComplianceService._clean(buyer_tin, 50)
        buyer_authorization_code = EISSaleComplianceService._clean(
            getattr(order, 'buyer_authorization_code', None),
            100,
        )

        if buyer_tin and getattr(settings, 'MRA_EIS_VALIDATE_BUYER_TIN_BEFORE_SALE', True):
            try:
                auth_requirement = EISSaleComplianceService.check_tin_authorization_requirement(
                    business=order.business,
                    tin=buyer_tin,
                    terminal=terminal,
                )
            except Exception as exc:
                if _is_mra_network_failure(exc):
                    raise MRAIntegrationError('B2B sales need MRA online.') from exc
                raise

            result['buyer_authorization_requirement'] = auth_requirement
            if auth_requirement.get('tin_exists') is False:
                raise MRAIntegrationError(f'Buyer TIN {buyer_tin} was not found by MRA EIS.')
            if auth_requirement.get('requires_authorization_code') and not buyer_authorization_code:
                raise MRAIntegrationError(
                    f'Buyer TIN {buyer_tin} requires an MRA buyer authorization code before sale submission.'
                )

        if buyer_authorization_code:
            try:
                auth_validation = EISSaleComplianceService.validate_authorization_code(
                    business=order.business,
                    authorization_code=buyer_authorization_code,
                    terminal=terminal,
                )
            except Exception as exc:
                if _is_mra_network_failure(exc):
                    raise MRAIntegrationError('B2B sales need MRA online.') from exc
                raise

            result['buyer_authorization_validation'] = auth_validation
            if auth_validation.get('checked') and not auth_validation.get('is_valid'):
                raise MRAIntegrationError('MRA buyer authorization code is invalid or expired.')

        if bool(getattr(order, 'is_relief_supply', False)):
            project_number = EISSaleComplianceService._clean(getattr(order, 'vat5_project_number', None), 100)
            certificate_number = EISSaleComplianceService._clean(getattr(order, 'vat5_certificate_number', None), 100)
            quantity_value = EISSaleComplianceService._to_decimal(getattr(order, 'vat5_quantity', None))
            if not project_number or not certificate_number or quantity_value <= 0:
                raise MRAIntegrationError(
                    'VAT5 project number, certificate number, and positive quantity are required for relief supply.'
                )
            if getattr(settings, 'MRA_EIS_VALIDATE_VAT5_BEFORE_SALE', True):
                try:
                    vat5_validation = EISSaleComplianceService.validate_vat5_certificate(
                        business=order.business,
                        project_number=project_number,
                        certificate_number=certificate_number,
                        quantity=quantity_value,
                        terminal=terminal,
                    )
                except Exception as exc:
                    if _is_mra_network_failure(exc):
                        raise MRAIntegrationError('Relief sale needs MRA online.') from exc
                    raise
                result['vat5_validation'] = vat5_validation
                if vat5_validation.get('checked') and not vat5_validation.get('is_valid'):
                    raise MRAIntegrationError('MRA VAT5 certificate is invalid or expired.')

        metadata['special_sale_validation'] = result
        order.eis_validation_metadata = metadata
        order.save(update_fields=['eis_validation_metadata', 'updated_at'])
        return result


class POSOrderSubmissionService:
    """
    POS order submission lifecycle.

    In dry-run mode this service prepares and stores everything needed for MRA
    without sending live transactions.
    """

    @staticmethod
    def _resolve_order_terminal(
        order,
        *,
        request_device_serial: str | None = None,
        enforce_device_binding: bool = False,
    ):
        terminal_qs = Terminal.objects.filter(
            business=order.business,
            branch=order.branch,
        )
        device_serial = TerminalService.normalize_device_serial(request_device_serial)
        terminal = None

        if device_serial:
            terminal = (
                terminal_qs.filter(device_serial__iexact=device_serial)
                .order_by('-updated_at')
                .first()
            )
            if not terminal and enforce_device_binding:
                raise MRAIntegrationError(
                    'This device is not activated as an MRA EIS terminal for this branch. '
                    'Activate this device with a TAC before making fiscal sales.'
                )

        if not terminal:
            terminal = (
                terminal_qs
                .order_by('-updated_at')
                .first()
            )

        if terminal:
            return terminal

        if enforce_device_binding:
            raise MRAIntegrationError(
                'No active MRA EIS terminal exists for this branch/device. '
                'Activate this device with a TAC before making fiscal sales.'
            )

        # Ensure order can still be prepared in backend/no-terminal maintenance scenarios.
        local_terminal_id = f"TRM-{order.branch_id}-{uuid.uuid4().hex[:6].upper()}"
        terminal = Terminal.objects.create(
            business=order.business,
            branch=order.branch,
            terminal_id=local_terminal_id,
            device_serial=f"AUTO-{order.branch_id}",
            mac_address='',
            pos_name='Handy-POS',
            pos_version='1.0.0',
            os_type='Backend',
            mra_terminal_id=local_terminal_id,
            mra_api_key='',
            status='pending_activation',
            is_online=False,
        )
        return terminal

    @staticmethod
    def _base10_to_mra_base64(number: int) -> str:
        chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            return 'A'
        result = ''
        while number > 0:
            number, remainder = divmod(number, 64)
            result = chars[remainder] + result
        return result

    @staticmethod
    def _mra_base64_to_base10(value: str) -> int:
        chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
        result = 0
        for char in str(value or ''):
            if char not in chars:
                raise ValueError('Invalid MRA base64 digit')
            result = result * 64 + chars.index(char)
        return result

    @staticmethod
    def _to_julian_date(value) -> int:
        date_value = value.date() if hasattr(value, 'date') else timezone.now().date()
        year = date_value.year
        month = date_value.month
        day = date_value.day
        if month <= 2:
            year -= 1
            month += 12
        century = year // 100
        correction = 2 - century + (century // 4)
        return int((365.25 * (year + 4716)) // 1 + (30.6001 * (month + 1)) // 1 + day + correction - 1524)

    @staticmethod
    def _numeric_identifier(value: Any, fallback: int = 1) -> int:
        digits = re.sub(r'\D+', '', str(value or ''))
        if not digits:
            return fallback
        try:
            return int(digits)
        except ValueError:
            return fallback

    @staticmethod
    def _invoice_identity_from_activation_audit(terminal: Terminal) -> tuple[int | None, int | None]:
        try:
            audit = terminal.audit_logs.filter(action='activated').order_by('-created_at').first()
        except Exception:
            audit = None
        if not audit or not isinstance(audit.details, dict):
            return None, None

        response_data = audit.details.get('response')
        if not isinstance(response_data, dict):
            return None, None

        activated_terminal = TerminalService._extract_activation_terminal(response_data)
        taxpayer_id = TerminalService._to_positive_int(
            TerminalService._dict_get_any(
                activated_terminal,
                'taxpayerId',
                'TaxpayerId',
                'taxpayerID',
                'TaxpayerID',
                'taxpayer_id',
                'businessId',
                'BusinessId',
            )
            or TerminalService._find_nested_value(
                response_data,
                'taxpayerId',
                'TaxpayerId',
                'taxpayerID',
                'TaxpayerID',
                'taxpayer_id',
                'businessId',
                'BusinessId',
            )
        )
        terminal_position = TerminalService._to_positive_int(
            TerminalService._dict_get_any(
                activated_terminal,
                'terminalPosition',
                'TerminalPosition',
                'terminal_position',
                'position',
                'Position',
            )
            or TerminalService._find_nested_value(
                response_data,
                'terminalPosition',
                'TerminalPosition',
                'terminal_position',
                'position',
                'Position',
            )
        )
        return taxpayer_id, terminal_position

    @staticmethod
    def _terminal_invoice_identity(order, terminal: Terminal) -> tuple[int, int]:
        taxpayer_id = TerminalService._to_positive_int(getattr(terminal, 'mra_taxpayer_id', None))
        terminal_position = TerminalService._to_positive_int(getattr(terminal, 'terminal_position', None))

        if not taxpayer_id or not terminal_position:
            audit_taxpayer_id, audit_terminal_position = POSOrderSubmissionService._invoice_identity_from_activation_audit(terminal)
            update_fields: list[str] = []
            if not taxpayer_id and audit_taxpayer_id:
                taxpayer_id = audit_taxpayer_id
                terminal.mra_taxpayer_id = audit_taxpayer_id
                update_fields.append('mra_taxpayer_id')
            if not terminal_position and audit_terminal_position:
                terminal_position = audit_terminal_position
                terminal.terminal_position = audit_terminal_position
                update_fields.append('terminal_position')
            if update_fields:
                update_fields.append('updated_at')
                try:
                    terminal.save(update_fields=update_fields)
                except Exception as exc:
                    logger.debug('Could not persist terminal invoice identity from activation audit: %s', exc)

        if not taxpayer_id:
            taxpayer_id = POSOrderSubmissionService._numeric_identifier(
                getattr(order.business, 'tin', None),
                fallback=int(getattr(order.business, 'id', 1) or 1),
            )
        if not terminal_position:
            terminal_position = POSOrderSubmissionService._numeric_identifier(
                getattr(order.branch, 'mra_branch_code', None),
                fallback=int(getattr(order.branch, 'id', 1) or 1),
            )

        return taxpayer_id, terminal_position

    @staticmethod
    def _fiscal_invoice_identity_matches_order_terminal(
        fiscal_invoice_number: str,
        order,
        terminal: Terminal,
    ) -> bool:
        parts = str(fiscal_invoice_number or '').split('-')
        if len(parts) < 2:
            return False

        try:
            invoice_taxpayer_id = POSOrderSubmissionService._mra_base64_to_base10(parts[0])
            invoice_terminal_position = POSOrderSubmissionService._mra_base64_to_base10(parts[1])
        except ValueError:
            return False

        taxpayer_id, terminal_position = POSOrderSubmissionService._terminal_invoice_identity(order, terminal)
        return invoice_taxpayer_id == taxpayer_id and invoice_terminal_position == terminal_position

    @staticmethod
    def _generate_fiscal_invoice_number(
        order,
        terminal: Terminal,
        is_online: bool,
        invoice_date_time: Any | None = None,
    ) -> str:
        if order.fiscal_invoice_number:
            return order.fiscal_invoice_number

        sequence, julian_date = InvoiceService.allocate_fiscal_sequence(
            terminal,
            invoice_date_time or getattr(order, 'created_at', None),
        )
        taxpayer_id, terminal_position = POSOrderSubmissionService._terminal_invoice_identity(order, terminal)

        return '-'.join(
            [
                POSOrderSubmissionService._base10_to_mra_base64(taxpayer_id),
                POSOrderSubmissionService._base10_to_mra_base64(terminal_position),
                POSOrderSubmissionService._base10_to_mra_base64(julian_date),
                POSOrderSubmissionService._base10_to_mra_base64(int(sequence)),
            ]
        )

    @staticmethod
    def _extract_sequence_from_fiscal_number(fiscal_invoice_number: str) -> int:
        return InvoiceService.extract_sequence_from_fiscal_invoice_number(fiscal_invoice_number)

    @staticmethod
    def _extract_julian_from_fiscal_number(fiscal_invoice_number: str) -> int:
        return InvoiceService.extract_julian_from_fiscal_invoice_number(fiscal_invoice_number)

    @staticmethod
    def _format_decimal(value: Any, places: str = '0.01') -> float:
        try:
            return float(Decimal(str(value or 0)).quantize(Decimal(places)))
        except Exception:
            return float(Decimal('0').quantize(Decimal(places)))

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value or 0))
            return parsed if parsed.is_finite() else Decimal('0')
        except (InvalidOperation, TypeError, ValueError):
            return Decimal('0')

    @staticmethod
    def _money(value: Any) -> Decimal:
        return POSOrderSubmissionService._to_decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @staticmethod
    def _quantity(value: Any) -> Decimal:
        return POSOrderSubmissionService._to_decimal(value).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)

    @staticmethod
    def _sum_queued_offline_gross_amount(terminal: Terminal) -> Decimal:
        return InvoiceService._sum_queued_offline_gross_amount(terminal)

    @staticmethod
    def _enforce_offline_limits(
        order,
        terminal: Terminal,
        is_online: bool,
        *,
        is_new_offline_issue: bool,
    ) -> None:
        if is_online:
            return

        limits = ConfigurationService.get_offline_limits(order.business)

        if limits.max_transaction_age_hours is not None:
            transaction_age_hours = (timezone.now() - order.created_at).total_seconds() / 3600
            if transaction_age_hours > float(limits.max_transaction_age_hours):
                raise MRAIntegrationError(
                    'Offline transaction age exceeds configured limit '
                    f'({transaction_age_hours:.2f}h > {limits.max_transaction_age_hours}h).'
                )

        # Enforce cumulative cap only when issuing a new offline fiscal number.
        # Re-preparing the same order should not double-count its amount.
        if is_new_offline_issue and limits.max_cumulative_amount is not None:
            queued_total = POSOrderSubmissionService._sum_queued_offline_gross_amount(terminal)
            current_amount = Decimal(str(order.gross_amount or order.total or 0))
            projected_total = queued_total + current_amount
            if projected_total > limits.max_cumulative_amount:
                raise MRAIntegrationError(
                    'Offline cumulative amount exceeds configured limit '
                    f'({projected_total} > {limits.max_cumulative_amount}).'
                )

    @staticmethod
    def _apply_offline_signature(payload: dict[str, Any], terminal: Terminal, is_online: bool) -> str | None:
        invoice_summary = payload.setdefault('invoiceSummary', {})
        if is_online:
            invoice_summary.pop('offlineSignature', None)
            return None

        offline_artifacts = InvoiceService.build_offline_validation_artifacts_from_payload(payload, terminal)
        offline_signature = offline_artifacts['offline_signature']
        invoice_summary['offlineSignature'] = offline_signature
        payload.setdefault('handyPosMetadata', {})['offlineValidationURL'] = offline_artifacts['validation_url']
        payload.setdefault('handyPosMetadata', {})['offlineValidationParams'] = offline_artifacts['validation_params']
        return offline_signature

    @staticmethod
    def _mra_payload_only(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            'invoiceHeader': payload.get('invoiceHeader', {}),
            'invoiceLineItems': payload.get('invoiceLineItems', []),
            'invoiceSummary': payload.get('invoiceSummary', {}),
        }

    @staticmethod
    def _is_network_only_submission_failure(
        exc: Exception,
        *,
        status_code: int,
        response_data: dict[str, Any],
    ) -> bool:
        return _is_mra_network_failure(exc, status_code=status_code, response_data=response_data)

    @staticmethod
    def _build_sale_submission_diagnostic(
        *,
        result: MRACallResult,
        endpoint_key: str,
        is_online: bool,
        response_data: dict[str, Any],
        response_errors: list[str],
        queue_entry,
        offline_validation_url: str,
    ) -> dict[str, Any]:
        response_inner = response_data.get('data') if isinstance(response_data.get('data'), dict) else {}
        reason = str(response_data.get('reason') or response_inner.get('reason') or '').strip()
        remark = str(response_data.get('remark') or response_inner.get('remark') or '').strip()
        error = str(response_data.get('error') or response_inner.get('error') or '').strip()
        http_status = response_data.get('httpStatusCode') or response_data.get('http_status_code') or result.status_code
        mra_status_code = response_data.get('statusCode') or response_data.get('status_code')

        def user_safe_detail() -> str:
            detail = error or remark or reason or '; '.join(response_errors)
            lower_detail = detail.lower()
            if '<html' in lower_detail or '<!doctype' in lower_detail or 'asp.net core app failed to start' in lower_detail:
                return 'MRA EIS is temporarily unavailable.'
            if result.status_code and int(result.status_code) >= 500:
                return 'MRA EIS is temporarily unavailable.'
            return detail[:220]

        if response_errors and not result.dry_run:
            state = 'rejected'
            message = 'MRA rejected the sale: ' + '; '.join(response_errors)
            retryable = False
        elif result.dry_run and not is_online and queue_entry:
            state = 'offline_queued'
            message = 'Offline fiscal receipt issued and queued for MRA replay.'
            retryable = True
        elif result.dry_run:
            state = 'prepared_retry'
            detail = user_safe_detail() or 'MRA submission was prepared but not confirmed.'
            message = f'MRA sale submission is pending retry: {detail}'
            retryable = True
        else:
            state = 'accepted'
            message = remark or 'MRA accepted the sale.'
            retryable = False

        return {
            'state': state,
            'message': message,
            'retryable': retryable,
            'queued_offline': bool(queue_entry),
            'is_online': bool(is_online),
            'dry_run': bool(result.dry_run),
            'endpoint': endpoint_key,
            'http_status': http_status,
            'mra_status_code': mra_status_code,
            'reason': reason,
            'remark': remark,
            'error': error,
            'errors': response_errors,
            'offline_validation_url': offline_validation_url or '',
            'checked_at': timezone.now().isoformat(),
        }

    @staticmethod
    def _get_item_mapping_map(order_item_ids: list[str]) -> dict[str, Any]:
        try:
            from inventory.models import MRAProductMapping as InventoryMRAProductMapping

            mappings = InventoryMRAProductMapping.objects.filter(
                inventory_item_id__in=order_item_ids,
            ).values(
                'id',
                'inventory_item_id',
                'mra_product_code',
                'mra_product_name',
                'mra_tax_type',
                'mra_tax_rate',
                'tax_calculation_method',
                'mra_levies',
                'is_product',
                'is_approved',
                'mra_synced',
            )
            return {str(m['inventory_item_id']): m for m in mappings}
        except Exception:
            return {}

    @staticmethod
    def _find_site_catalog_product(business, product_code: Any, site_id: Any = '') -> dict[str, Any] | None:
        normalized_code = ProductMappingService._catalog_key(product_code)
        if not normalized_code:
            return None

        normalized_site_id = str(site_id or '').strip().lower()
        fallback: dict[str, Any] | None = None

        for config_type in ['terminal_site_products', 'product_codes']:
            config = ConfigurationService.get_active_configuration(business, config_type)
            if not config or not config.config_data:
                continue

            queue: list[Any] = [config.config_data]
            while queue:
                current = queue.pop(0)
                if isinstance(current, list):
                    queue.extend(entry for entry in current if isinstance(entry, (dict, list)))
                    continue
                if not isinstance(current, dict):
                    continue

                normalized = ProductMappingService._normalize_mra_catalog_product(current, business=business)
                if normalized and normalized.get('code') == normalized_code:
                    product_site_id = str(normalized.get('site_id') or '').strip().lower()
                    if normalized_site_id and product_site_id == normalized_site_id:
                        return normalized
                    if not product_site_id and fallback is None:
                        fallback = normalized
                    elif fallback is None and not normalized_site_id:
                        fallback = normalized

                for value in current.values():
                    if isinstance(value, (dict, list)):
                        queue.append(value)

            if fallback:
                return fallback

        return fallback

    @staticmethod
    def _resolve_sale_line_description(order, item, mapping: dict[str, Any], site_id: str) -> str:
        product_code = str(mapping.get('mra_product_code') or '').strip()
        catalog_product = POSOrderSubmissionService._find_site_catalog_product(
            order.business,
            product_code,
            site_id,
        )
        catalog_description = ProductMappingService._catalog_sale_description(catalog_product)
        if catalog_description:
            mapping_description = str(mapping.get('mra_product_name') or '').strip()
            if mapping.get('id') and mapping_description != catalog_description:
                try:
                    from inventory.models import MRAProductMapping as InventoryMRAProductMapping

                    InventoryMRAProductMapping.objects.filter(id=mapping['id']).update(
                        mra_product_name=catalog_description[:255],
                        updated_at=timezone.now(),
                    )
                    mapping['mra_product_name'] = catalog_description[:255]
                except Exception as exc:
                    logger.debug('Could not update local MRA mapping description from site catalog: %s', exc)
            return catalog_description[:255]

        return str(mapping.get('mra_product_name') or getattr(item, 'name', '') or product_code).strip()[:255]

    @staticmethod
    def _require_ready_mapping(order, item, mapping: dict[str, Any] | None) -> dict[str, Any]:
        product_code = str((mapping or {}).get('mra_product_code') or '').strip()
        if (
            not mapping
            or not product_code
            or not bool(mapping.get('is_approved'))
            or not bool(mapping.get('mra_synced'))
        ):
            raise MRAIntegrationError(
                f'Product "{getattr(item, "name", "Unknown")}" is not MRA-approved and synced for sale. '
                'Pull approved products from MRA EIS before selling this item.'
            )
        return mapping

    @staticmethod
    def _ensure_terminal_can_issue_sale(
        terminal: Terminal,
        *,
        request_device_serial: str | None = None,
        enforce_device_binding: bool = False,
        check_mra_block: bool = True,
    ) -> None:
        if terminal.status != 'active':
            raise MRAIntegrationError(
                f'MRA terminal is not active for sales (current status: {terminal.status}). '
                'Activate or unblock the terminal before processing EIS sales.'
            )
        if enforce_device_binding:
            TerminalService.enforce_terminal_device_binding(
                terminal,
                request_device_serial,
                operation='issuing EIS sales',
            )
        if (
            bool(getattr(settings, 'MRA_EIS_ENABLE_HTTP_CALLS', False))
            and not bool(getattr(settings, 'MRA_EIS_DRY_RUN', True))
            and bool(getattr(settings, 'MRA_EIS_ALLOW_LIVE_SUBMISSION', False))
        ):
            missing_credentials = []
            if not str(getattr(terminal, 'mra_api_key', '') or '').strip():
                missing_credentials.append('secretKey')
            if not str(getattr(terminal, 'mra_token', '') or '').strip():
                missing_credentials.append('terminal JWT token')
            if missing_credentials:
                raise MRAIntegrationError(
                    'MRA terminal is missing credentials required for sale submission: '
                    f'{", ".join(missing_credentials)}. '
                    'Reactivate the terminal with a fresh TAC or restore the credentials returned by MRA activation.'
                )
            if check_mra_block and getattr(settings, 'MRA_EIS_CHECK_TERMINAL_BLOCK_BEFORE_SALE', True):
                TerminalService.ensure_terminal_not_blocked_for_sale(terminal)

    @staticmethod
    def _normalize_mapping_tax_for_order(order, item, mapping: dict[str, Any]) -> tuple[str, Decimal, str]:
        tax_type = mapping.get('mra_tax_type') or item.tax_type or 'standard'
        tax_rate = mapping.get('mra_tax_rate') or item.tax_rate or 0
        tax_method = mapping.get('tax_calculation_method') or item.tax_calculation_method or 'inclusive'
        normalized_type, normalized_rate, normalized_method, adjusted = ProductMappingService.normalize_tax_for_taxpayer(
            order.business,
            tax_type,
            tax_rate,
            tax_method,
        )

        mapping['mra_tax_type'] = normalized_type
        mapping['mra_tax_rate'] = normalized_rate
        mapping['tax_calculation_method'] = normalized_method
        return normalized_type, normalized_rate, normalized_method

    @staticmethod
    def _ensure_mapping_matches_site_catalog_tax(
        order,
        item,
        mapping: dict[str, Any],
        site_id: str = '',
    ) -> None:
        product_code = str(mapping.get('mra_product_code') or '').strip()
        catalog_product = POSOrderSubmissionService._find_site_catalog_product(
            order.business,
            product_code,
            site_id,
        )
        if not catalog_product:
            return

        mapping_type = ProductMappingService._normalize_mapping_tax_type(mapping.get('mra_tax_type'))
        catalog_type = ProductMappingService._normalize_mapping_tax_type(catalog_product.get('tax_type'))
        mapping_rate = POSOrderSubmissionService._money(mapping.get('mra_tax_rate'))
        catalog_rate = POSOrderSubmissionService._money(catalog_product.get('tax_rate'))

        if mapping_type == catalog_type and mapping_rate == catalog_rate:
            return

        raise MRAIntegrationError(
            f'MRA site product "{product_code}" for "{getattr(item, "name", "Unknown")}" is configured in EIS as '
            f'{catalog_type} VAT ({catalog_rate}%), but the local POS mapping is {mapping_type} VAT ({mapping_rate}%). '
            'Update the product tax in the MRA EIS portal and pull approved products again, or activate VAT registration '
            'before selling this item.'
        )

    @staticmethod
    def _validate_order_items_ready_for_eis(order) -> None:
        order_items = list(order.items.all())
        if not order_items:
            raise MRAIntegrationError('Cannot submit an empty POS order to MRA EIS.')

        mapping_map = POSOrderSubmissionService._get_item_mapping_map(
            [str(item.inventory_item_id) for item in order_items]
        )
        for item in order_items:
            mapping = POSOrderSubmissionService._require_ready_mapping(
                order,
                item,
                mapping_map.get(str(item.inventory_item_id)),
            )
            tax_type, tax_rate, _tax_method = POSOrderSubmissionService._normalize_mapping_tax_for_order(
                order,
                item,
                mapping,
            )
            POSOrderSubmissionService._ensure_mapping_matches_site_catalog_tax(order, item, mapping)

    @staticmethod
    def _calculate_mra_line_amounts(
        *,
        unit_price: Any,
        quantity: Any,
        tax_rate: Any,
        tax_type: str,
        tax_calculation_method: str,
        discount_amount: Any = 0,
        remove_standard_vat: bool = False,
    ) -> tuple[Decimal, Decimal, Decimal]:
        price = POSOrderSubmissionService._to_decimal(unit_price)
        qty = POSOrderSubmissionService._quantity(quantity)
        line_amount_before_discount = POSOrderSubmissionService._money(max(Decimal('0'), price * qty))
        line_discount = POSOrderSubmissionService._money(
            max(POSOrderSubmissionService._to_decimal(discount_amount), Decimal('0'))
        )
        if line_discount >= line_amount_before_discount and line_discount > 0:
            raise MRAIntegrationError(
                'Discount must be less than the item total. '
                f'Discount {line_discount} cannot be applied to item total {line_amount_before_discount}.'
            )
        line_amount = POSOrderSubmissionService._money(line_amount_before_discount - line_discount)
        rate = max(POSOrderSubmissionService._to_decimal(tax_rate), Decimal('0'))
        method = 'exclusive' if str(tax_calculation_method or '').lower() == 'exclusive' else 'inclusive'
        category = str(tax_type or '').lower()
        taxable = category not in {'zero', 'vat_zero', 'zero_rated', 'exempt', 'vat_exempt'} and rate > 0

        if remove_standard_vat and taxable:
            if method == 'exclusive':
                line_net = line_amount
            else:
                line_net = POSOrderSubmissionService._money(line_amount * Decimal('100') / (Decimal('100') + rate))
            return line_net, Decimal('0.00'), line_net

        if method == 'exclusive':
            line_net = line_amount
            line_tax = POSOrderSubmissionService._money(line_net * rate / Decimal('100')) if taxable else Decimal('0.00')
            line_gross = POSOrderSubmissionService._money(line_net + line_tax)
            return line_net, line_tax, line_gross

        line_gross = line_amount
        line_tax = (
            POSOrderSubmissionService._money(line_gross * rate / (Decimal('100') + rate))
            if taxable
            else Decimal('0.00')
        )
        line_net = POSOrderSubmissionService._money(line_gross - line_tax)
        return line_net, line_tax, line_gross

    @staticmethod
    def _line_uses_standard_vat(tax_type: Any, tax_rate: Any) -> bool:
        category = str(tax_type or '').strip().lower()
        rate = POSOrderSubmissionService._to_decimal(tax_rate)
        return category not in {'zero', 'vat_zero', 'zero_rated', 'exempt', 'vat_exempt'} and rate > 0

    @staticmethod
    def _ensure_taxpayer_can_use_line_tax(order, item, tax_type: Any, tax_rate: Any) -> None:
        """Retained for compatibility; MRA-approved product tax is authoritative."""
        return None

    @staticmethod
    def _payload_totals(payload_items: list[dict[str, Any]]) -> tuple[Decimal, Decimal, Decimal]:
        net_amount = sum(
            (POSOrderSubmissionService._money(item.get('total')) for item in payload_items),
            Decimal('0.00'),
        )
        tax_amount = sum(
            (POSOrderSubmissionService._money(item.get('totalVAT')) for item in payload_items),
            Decimal('0.00'),
        )
        gross_amount = POSOrderSubmissionService._money(net_amount + tax_amount)
        return net_amount, tax_amount, gross_amount

    @staticmethod
    def _persist_mra_amount_snapshot(order, payload: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
        payload_items = payload.get('invoiceLineItems') if isinstance(payload.get('invoiceLineItems'), list) else []
        line_snapshots = (
            payload.get('handyPosMetadata', {}).get('lineSnapshots', [])
            if isinstance(payload.get('handyPosMetadata'), dict)
            else []
        )
        order_items = list(order.items.all())

        for item, line, snapshot in zip(order_items, payload_items, line_snapshots):
            item.mra_product_code = snapshot.get('mraProductCode') or line.get('productCode') or item.mra_product_code
            item.tax_rate = POSOrderSubmissionService._money(snapshot.get('taxRate'))
            item.tax_type = snapshot.get('taxType') or item.tax_type
            item.tax_calculation_method = snapshot.get('taxCalculationMethod') or item.tax_calculation_method
            item.subtotal = POSOrderSubmissionService._money(line.get('total'))
            item.tax_amount = POSOrderSubmissionService._money(line.get('totalVAT'))
            item.total = POSOrderSubmissionService._money(snapshot.get('grossAmount'))
            item.save(
                update_fields=[
                    'mra_product_code',
                    'tax_rate',
                    'tax_type',
                    'tax_calculation_method',
                    'subtotal',
                    'tax_amount',
                    'total',
                    'updated_at',
                ]
            )

        net_amount, tax_amount, line_gross_amount = POSOrderSubmissionService._payload_totals(payload_items)
        summary = payload.get('invoiceSummary') if isinstance(payload.get('invoiceSummary'), dict) else {}
        invoice_total = POSOrderSubmissionService._money(summary.get('invoiceTotal') or line_gross_amount)
        order.subtotal = net_amount
        order.net_amount = net_amount
        order.vat_amount = tax_amount
        order.gross_amount = invoice_total
        order.total = invoice_total

        tax_types = {str(snapshot.get('taxType') or '').lower() for snapshot in line_snapshots if snapshot}
        tax_rates = {
            POSOrderSubmissionService._money(snapshot.get('taxRate'))
            for snapshot in line_snapshots
            if snapshot is not None
        }
        if len(tax_rates) == 1:
            order.tax_rate_value = next(iter(tax_rates))
        if len(tax_types) == 1:
            tax_type = next(iter(tax_types))
            order.tax_type = {
                'standard': 'VAT_STANDARD',
                'zero': 'VAT_ZERO',
                'exempt': 'VAT_EXEMPT',
            }.get(tax_type, order.tax_type)

        return net_amount, tax_amount, invoice_total

    @staticmethod
    def _clean_buyer_value(value: Any, max_length: int) -> str:
        if value is None:
            return ''
        return str(value).strip()[:max_length]

    @staticmethod
    def _normalize_payment_method_for_mra(value: Any) -> str:
        raw_value = str(value or '').strip()
        if not raw_value:
            return 'Cash'

        normalized_key = raw_value.lower().replace('-', ' ').replace('_', ' ')
        normalized_key = ' '.join(normalized_key.split())
        compact_key = normalized_key.replace(' ', '')
        aliases = {
            'cash': 'Cash',
            'card': 'Card',
            'creditcard': 'Card',
            'debitcard': 'Card',
            'mobilemoney': 'MobileMoney',
            'momo': 'MobileMoney',
            'onaccount': 'OnAccount',
            'account': 'OnAccount',
            'credit': 'Credit',
            'banktransfer': 'BankTransfer',
            'transfer': 'BankTransfer',
            'other': 'Other',
        }
        return aliases.get(compact_key, raw_value.replace(' ', ''))

    @staticmethod
    def _resolve_related_invoice(order):
        """
        Resolve the business invoice linked to this POS order (if any).
        Supports both direct invoice_id linkage and reverse related_order_id lookup.
        """
        try:
            from business.models import Invoice

            invoice_qs = Invoice.objects.select_related('customer').filter(business=order.business)
            invoice_ref = POSOrderSubmissionService._clean_buyer_value(getattr(order, 'invoice_id', ''), 255)

            if invoice_ref:
                try:
                    if invoice_ref.isdigit():
                        invoice = invoice_qs.filter(id=int(invoice_ref)).first()
                    else:
                        invoice = invoice_qs.filter(id=invoice_ref).first()
                    if invoice:
                        return invoice
                except Exception:
                    # Fall back to reverse lookup below.
                    pass

            return invoice_qs.filter(related_order_id=str(order.id)).order_by('-created_at').first()
        except Exception as exc:
            logger.debug('Could not resolve related invoice for POS order %s: %s', order.id, exc)
            return None

    @staticmethod
    def _resolve_buyer_details(order) -> tuple[str, str]:
        """
        Resolve buyer details for MRA payloads.
        Priority:
        1) Order-level fields (future/optional compatibility)
        2) Linked business invoice + customer
        """
        buyer_tin = POSOrderSubmissionService._clean_buyer_value(
            getattr(order, 'buyer_tin', None) or getattr(order, 'customer_tin', None),
            50,
        )
        buyer_name = POSOrderSubmissionService._clean_buyer_value(
            getattr(order, 'buyer_name', None) or getattr(order, 'customer_name', None),
            255,
        )

        invoice = POSOrderSubmissionService._resolve_related_invoice(order)
        if invoice:
            customer = getattr(invoice, 'customer', None)

            if not buyer_tin:
                buyer_tin = POSOrderSubmissionService._clean_buyer_value(
                    getattr(customer, 'customer_tin', None),
                    50,
                )

            if not buyer_name:
                buyer_name = POSOrderSubmissionService._clean_buyer_value(
                    getattr(customer, 'name', None) or getattr(invoice, 'customer_name', None),
                    255,
                )

        return buyer_tin, buyer_name

    @staticmethod
    def _is_b2b_order(order, buyer_tin: str = '') -> bool:
        buyer_tin = POSOrderSubmissionService._clean_buyer_value(
            buyer_tin or getattr(order, 'buyer_tin', None) or getattr(order, 'customer_tin', None),
            50,
        )
        buyer_authorization_code = POSOrderSubmissionService._clean_buyer_value(
            getattr(order, 'buyer_authorization_code', None),
            100,
        )
        return bool(buyer_tin or buyer_authorization_code)

    @staticmethod
    def _enforce_b2b_online_only(order, is_online: bool, buyer_tin: str = '') -> None:
        if POSOrderSubmissionService._is_b2b_order(order, buyer_tin=buyer_tin) and not is_online:
            raise MRAIntegrationError(
                'B2B EIS sales require MRA online confirmation. Connect to internet and retry.'
            )

    @staticmethod
    def build_pos_order_payload(
        order,
        terminal: Terminal,
        is_online: bool,
        buyer_tin: str = '',
        buyer_name: str = '',
        invoice_date_time: Any | None = None,
        time_sync_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        buyer_tin = POSOrderSubmissionService._clean_buyer_value(buyer_tin, 50)
        buyer_name = POSOrderSubmissionService._clean_buyer_value(buyer_name, 255)
        order_items = list(order.items.all())
        if not order_items:
            raise MRAIntegrationError('Cannot submit an empty POS order to MRA EIS.')

        is_relief_supply = bool(getattr(order, 'is_relief_supply', False))
        mapping_map = POSOrderSubmissionService._get_item_mapping_map(
            [str(item.inventory_item_id) for item in order_items]
        )
        site_id = ConfigurationService.get_terminal_site_id(order.business, order.branch)

        payload_items: list[dict[str, Any]] = []
        levy_lines: list[dict[str, Any]] = []
        line_snapshots: list[dict[str, Any]] = []
        for index, item in enumerate(order_items, start=1):
            key = str(item.inventory_item_id)
            mapping = POSOrderSubmissionService._require_ready_mapping(order, item, mapping_map.get(key))
            tax_type, tax_rate, tax_method = POSOrderSubmissionService._normalize_mapping_tax_for_order(
                order,
                item,
                mapping,
            )
            removes_standard_vat = is_relief_supply and POSOrderSubmissionService._line_uses_standard_vat(
                tax_type,
                tax_rate,
            )
            POSOrderSubmissionService._ensure_mapping_matches_site_catalog_tax(order, item, mapping, site_id)
            tax_rate_id = ConfigurationService.resolve_tax_rate_id(order.business, tax_rate, tax_type)
            line_description = POSOrderSubmissionService._resolve_sale_line_description(
                order,
                item,
                mapping,
                site_id,
            )
            line_net, line_tax, line_gross = POSOrderSubmissionService._calculate_mra_line_amounts(
                unit_price=item.price,
                quantity=item.quantity,
                discount_amount=getattr(item, 'discount_amount', 0),
                tax_rate=tax_rate,
                tax_type=tax_type,
                tax_calculation_method=tax_method,
                remove_standard_vat=removes_standard_vat,
            )
            line_quantity = POSOrderSubmissionService._quantity(item.quantity)
            line_unit_price = POSOrderSubmissionService._money(line_gross / line_quantity) if (
                removes_standard_vat and line_quantity > 0
            ) else POSOrderSubmissionService._money(item.price)
            is_product = bool(mapping.get('is_product', True))
            payload_items.append(
                {
                    'id': index,
                    'productCode': str(mapping.get('mra_product_code') or '').strip(),
                    'description': line_description,
                    'unitPrice': POSOrderSubmissionService._format_decimal(line_unit_price),
                    'quantity': POSOrderSubmissionService._format_decimal(item.quantity, '0.001'),
                    'discount': POSOrderSubmissionService._format_decimal(getattr(item, 'discount_amount', 0)),
                    'total': POSOrderSubmissionService._format_decimal(line_net),
                    'totalVAT': POSOrderSubmissionService._format_decimal(line_tax),
                    'taxRateId': tax_rate_id,
                    'isProduct': is_product,
                }
            )
            levy_lines.append(
                {
                    'taxableAmount': line_net,
                    'levies': mapping.get('mra_levies') or [],
                }
            )
            line_snapshots.append(
                {
                    'orderItemId': str(item.id),
                    'inventoryItemId': key,
                    'mraProductCode': str(mapping.get('mra_product_code') or '').strip(),
                    'mraProductName': line_description,
                    'taxType': tax_type,
                    'taxRate': str(POSOrderSubmissionService._money(tax_rate)),
                    'taxCalculationMethod': tax_method,
                    'discountRuleId': getattr(item, 'discount_rule_id', '') or '',
                    'discountName': getattr(item, 'discount_name', '') or '',
                    'discountType': getattr(item, 'discount_type', '') or '',
                    'discountValue': str(POSOrderSubmissionService._money(getattr(item, 'discount_value', 0))),
                    'discountAmount': str(POSOrderSubmissionService._money(getattr(item, 'discount_amount', 0))),
                    'netAmount': str(line_net),
                    'taxAmount': str(line_tax),
                    'levies': mapping.get('mra_levies') or [],
                    'grossAmount': str(line_gross),
                    'isProduct': is_product,
                    'reliefSupplyApplied': removes_standard_vat,
                    'reliefVATRemoved': str(
                        POSOrderSubmissionService._money(
                            POSOrderSubmissionService._money(item.price) * line_quantity - line_gross
                        )
                    ) if removes_standard_vat else '0.00',
                }
            )
        tax_breakdown = InvoiceService._build_tax_breakdown(order.business, payload_items)
        levy_breakdown = InvoiceService._build_levy_breakdown(order.business, levy_lines)
        levy_amount = InvoiceService._sum_levy_breakdown(levy_breakdown)
        net_amount, tax_amount, gross_amount = POSOrderSubmissionService._payload_totals(payload_items)
        invoice_total = POSOrderSubmissionService._money(gross_amount + levy_amount)
        versions = ConfigurationService.get_config_versions(order.business)
        seller_tin = ConfigurationService.get_taxpayer_tin(order.business)

        invoice_header = {
            'invoiceNumber': order.fiscal_invoice_number,
            'invoiceDateTime': (
                invoice_date_time.isoformat()
                if hasattr(invoice_date_time, 'isoformat')
                else str(invoice_date_time or order.created_at.isoformat())
            ),
            'sellerTIN': seller_tin,
            'siteId': site_id,
            'globalConfigVersion': versions['global'],
            'taxpayerConfigVersion': versions['taxpayer'],
            'terminalConfigVersion': versions['terminal'],
            'isExport': bool(getattr(order, 'is_export', False)),
            'isReliefSupply': is_relief_supply,
            'paymentMethod': POSOrderSubmissionService._normalize_payment_method_for_mra(order.payment_method),
        }
        if buyer_tin:
            invoice_header['buyerTIN'] = buyer_tin
        if buyer_name:
            invoice_header['buyerName'] = buyer_name
        buyer_authorization_code = POSOrderSubmissionService._clean_buyer_value(
            getattr(order, 'buyer_authorization_code', None),
            100,
        )
        if buyer_authorization_code:
            invoice_header['buyerAuthorizationCode'] = buyer_authorization_code
        is_b2b_sale = bool(buyer_tin or buyer_authorization_code)
        if invoice_header['isReliefSupply']:
            invoice_header['vat5CertificateDetails'] = {
                'projectNumber': POSOrderSubmissionService._clean_buyer_value(
                    getattr(order, 'vat5_project_number', None),
                    100,
                ),
                'certificateNumber': POSOrderSubmissionService._clean_buyer_value(
                    getattr(order, 'vat5_certificate_number', None),
                    100,
                ),
                'quantity': POSOrderSubmissionService._format_decimal(
                    getattr(order, 'vat5_quantity', None),
                    '0.001',
                ),
            }

        local_receipt_reference = (
            f"{order.created_at.strftime('%Y%m%d-%H%M%S')}-{int(order.order_number)}"
            if getattr(order, 'created_at', None)
            else str(order.order_number)
        )

        payload = {
            'invoiceHeader': invoice_header,
            'invoiceLineItems': payload_items,
            'invoiceSummary': {
                'taxBreakDown': tax_breakdown,
                'levyBreakDown': levy_breakdown,
                'totalVAT': POSOrderSubmissionService._format_decimal(tax_amount),
                'invoiceTotal': POSOrderSubmissionService._format_decimal(invoice_total),
                'amountTendered': POSOrderSubmissionService._format_decimal(invoice_total),
            },
            'handyPosMetadata': {
                'terminalId': terminal.mra_terminal_id,
                'terminalCode': terminal.terminal_id,
                'orderId': str(order.id),
                'orderNumber': int(order.order_number),
                'localReceiptReference': local_receipt_reference,
                'isOffline': not is_online,
                'isB2B': is_b2b_sale,
                'onlineOnly': is_b2b_sale,
                'mraServerTime': time_sync_metadata or {},
                'calculatedNetAmount': str(net_amount),
                'calculatedVAT': str(tax_amount),
                'calculatedLevyAmount': str(levy_amount),
                'calculatedGrossAmount': str(gross_amount),
                'calculatedInvoiceTotal': str(invoice_total),
                'lineSnapshots': line_snapshots,
            },
        }

        return payload

    @staticmethod
    def prepare_pos_order_submission(
        order,
        force_online: bool | None = None,
        *,
        request_device_serial: str | None = None,
        enforce_device_binding: bool = False,
    ) -> dict[str, Any]:
        if order.status in {'Voided', 'Cancelled'}:
            return {
                'order_id': str(order.id),
                'skipped': True,
                'reason': f'order_status_{order.status.lower()}',
            }

        terminal = POSOrderSubmissionService._resolve_order_terminal(
            order,
            request_device_serial=request_device_serial,
            enforce_device_binding=enforce_device_binding,
        )
        POSOrderSubmissionService._ensure_terminal_can_issue_sale(
            terminal,
            request_device_serial=request_device_serial,
            enforce_device_binding=enforce_device_binding,
            check_mra_block=True,
        )
        return POSOrderSubmissionService._prepare_pos_order_submission_atomic(
            order,
            force_online=force_online,
            request_device_serial=request_device_serial,
            enforce_device_binding=enforce_device_binding,
        )

    @staticmethod
    @transaction.atomic
    def _prepare_pos_order_submission_atomic(
        order,
        force_online: bool | None = None,
        *,
        request_device_serial: str | None = None,
        enforce_device_binding: bool = False,
    ) -> dict[str, Any]:
        if order.status in {'Voided', 'Cancelled'}:
            return {
                'order_id': str(order.id),
                'skipped': True,
                'reason': f'order_status_{order.status.lower()}',
            }

        terminal = POSOrderSubmissionService._resolve_order_terminal(
            order,
            request_device_serial=request_device_serial,
            enforce_device_binding=enforce_device_binding,
        )
        POSOrderSubmissionService._ensure_terminal_can_issue_sale(
            terminal,
            request_device_serial=request_device_serial,
            enforce_device_binding=enforce_device_binding,
            check_mra_block=False,
        )
        ConfigurationService.ensure_fresh_configuration(order.business, terminal=terminal, require_success=True)
        POSOrderSubmissionService._validate_order_items_ready_for_eis(order)

        is_online = bool(force_online) if force_online is not None else bool(terminal.is_online)
        buyer_tin, buyer_name = POSOrderSubmissionService._resolve_buyer_details(order)
        is_b2b_sale = POSOrderSubmissionService._is_b2b_order(order, buyer_tin=buyer_tin)
        always_offline_b2c = (
            bool(getattr(settings, 'MRA_EIS_ALWAYS_OFFLINE_B2C', False))
            and not is_b2b_sale
        )
        if always_offline_b2c:
            is_online = False
        POSOrderSubmissionService._enforce_b2b_online_only(order, is_online, buyer_tin=buyer_tin)
        had_fiscal_number = bool(order.fiscal_invoice_number)
        invoice_date_time = getattr(order, 'created_at', None)
        time_sync_metadata = {
            'source': 'existing_order_time',
            'invoiceDateTime': invoice_date_time.isoformat() if hasattr(invoice_date_time, 'isoformat') else str(invoice_date_time or ''),
        }

        if (
            had_fiscal_number
            and str(order.eis_status or '').upper() == 'REJECTED'
            and not POSOrderSubmissionService._fiscal_invoice_identity_matches_order_terminal(
                order.fiscal_invoice_number,
                order,
                terminal,
            )
        ):
            logger.info(
                'Regenerating rejected POS order fiscal invoice number %s because it does not match '
                'the terminal activation identity',
                order.fiscal_invoice_number,
            )
            order.fiscal_invoice_number = None
            had_fiscal_number = False

        if not had_fiscal_number:
            try:
                invoice_date_time, time_sync_metadata = TerminalService.resolve_mra_transaction_time(
                    terminal,
                    require_live_ping=is_online,
                )
            except MRAIntegrationError as exc:
                if is_online and not is_b2b_sale and force_online is not True:
                    logger.warning(
                        'Live MRA server time unavailable for order %s, attempting offline timestamp fallback: %s',
                        order.id,
                        exc,
                    )
                    terminal.is_online = False
                    terminal.save(update_fields=['is_online', 'updated_at'])
                    is_online = False
                    invoice_date_time, time_sync_metadata = TerminalService.resolve_mra_transaction_time(
                        terminal,
                        require_live_ping=False,
                    )
                else:
                    raise

        if had_fiscal_number:
            fiscal_number = str(order.fiscal_invoice_number)
        else:
            # Run policy checks before consuming the next fiscal sequence number.
            POSOrderSubmissionService._enforce_offline_limits(
                order,
                terminal,
                is_online,
                is_new_offline_issue=True,
            )
            fiscal_number = POSOrderSubmissionService._generate_fiscal_invoice_number(
                order=order,
                terminal=terminal,
                is_online=is_online,
                invoice_date_time=invoice_date_time,
            )

        sequence_number = POSOrderSubmissionService._extract_sequence_from_fiscal_number(fiscal_number)
        fiscal_julian_date = (
            POSOrderSubmissionService._extract_julian_from_fiscal_number(fiscal_number)
            or POSOrderSubmissionService._to_julian_date(invoice_date_time)
        )
        if had_fiscal_number and sequence_number > 0:
            existing_mra_invoice = MRAInvoice.objects.filter(
                terminal=terminal,
                fiscal_julian_date=fiscal_julian_date,
                invoice_number=sequence_number,
            ).order_by('-created_at').first()
            if existing_mra_invoice:
                is_online = bool(existing_mra_invoice.is_online)
                invoice_date_time = existing_mra_invoice.invoice_date
                time_sync_metadata = {
                    'source': 'existing_mra_invoice',
                    'invoiceDateTime': invoice_date_time.isoformat() if hasattr(invoice_date_time, 'isoformat') else str(invoice_date_time or ''),
                }
        POSOrderSubmissionService._enforce_b2b_online_only(order, is_online, buyer_tin=buyer_tin)
        if sequence_number <= 0:
            sequence_number = (
                terminal.online_invoice_counter if is_online else terminal.offline_invoice_counter
            )
        order.fiscal_invoice_number = fiscal_number

        # Replays of an existing offline number still need age validation, but
        # must not re-count the amount against the cumulative cap.
        POSOrderSubmissionService._enforce_offline_limits(
            order,
            terminal,
            is_online,
            is_new_offline_issue=False,
        )

        EISSaleComplianceService.validate_order_special_fields(order, terminal, buyer_tin)
        payload = POSOrderSubmissionService.build_pos_order_payload(
            order,
            terminal,
            is_online,
            buyer_tin=buyer_tin,
            buyer_name=buyer_name,
            invoice_date_time=invoice_date_time,
            time_sync_metadata=time_sync_metadata,
        )
        official_tax_breakdown = payload.get('invoiceSummary', {}).get('taxBreakDown', [])
        calculated_net_amount, calculated_tax_amount, calculated_gross_amount = (
            POSOrderSubmissionService._persist_mra_amount_snapshot(order, payload)
        )
        offline_signature = POSOrderSubmissionService._apply_offline_signature(
            payload,
            terminal,
            is_online,
        )
        offline_validation_url = str(
            (payload.get('handyPosMetadata') or {}).get('offlineValidationURL') or ''
        )
        submission_payload = POSOrderSubmissionService._mra_payload_only(payload)
        endpoint_key = 'report_sale' if is_online else 'report_sale_offline'

        client = MRAEISClient(terminal=terminal)
        try:
            if always_offline_b2c:
                result = MRACallResult(
                    ok=True,
                    dry_run=True,
                    status_code=202,
                    endpoint=client._resolve_endpoint(endpoint_key),
                    data={
                        'status': 'prepared',
                        'reason': 'b2c_offline_first_enabled',
                        'endpoint_key': endpoint_key,
                        'prepared_at': timezone.now().isoformat(),
                    },
                )
            else:
                result = client.call(endpoint_key, payload=submission_payload, method='POST', mutating=True)
        except MRAIntegrationError as exc:
            response_data = MRAEISClient._normalize_response_data(getattr(exc, 'response_data', None))
            status_code = int(getattr(exc, 'status_code', None) or 0)
            if (
                is_online
                and not had_fiscal_number
                and not is_b2b_sale
                and POSOrderSubmissionService._is_network_only_submission_failure(
                    exc,
                    status_code=status_code,
                    response_data=response_data,
                )
            ):
                original_endpoint_key = endpoint_key
                original_fiscal_number = fiscal_number
                logger.warning(
                    'POS online submission failed by network, issuing offline receipt for order %s: %s',
                    order.id,
                    exc,
                )

                terminal.is_online = False
                terminal.save(update_fields=['is_online', 'updated_at'])
                is_online = False
                endpoint_key = 'report_sale_offline'

                POSOrderSubmissionService._enforce_offline_limits(
                    order,
                    terminal,
                    is_online,
                    is_new_offline_issue=True,
                )
                # The online request failed by network before MRA could accept
                # it, so the same daily fiscal count becomes the offline
                # receipt. Do not allocate another number and skip count 1.
                order.fiscal_invoice_number = fiscal_number

                payload = POSOrderSubmissionService.build_pos_order_payload(
                    order,
                    terminal,
                    is_online,
                    buyer_tin=buyer_tin,
                    buyer_name=buyer_name,
                    invoice_date_time=invoice_date_time,
                    time_sync_metadata=time_sync_metadata,
                )
                official_tax_breakdown = payload.get('invoiceSummary', {}).get('taxBreakDown', [])
                calculated_net_amount, calculated_tax_amount, calculated_gross_amount = (
                    POSOrderSubmissionService._persist_mra_amount_snapshot(order, payload)
                )
                offline_signature = POSOrderSubmissionService._apply_offline_signature(
                    payload,
                    terminal,
                    is_online,
                )
                offline_validation_url = str(
                    (payload.get('handyPosMetadata') or {}).get('offlineValidationURL') or ''
                )
                submission_payload = POSOrderSubmissionService._mra_payload_only(payload)
                response_data = {
                    'status': 'prepared',
                    'reason': 'network_offline_fallback',
                    'error': str(exc),
                    'original_endpoint': original_endpoint_key,
                    'original_fiscal_invoice_number': original_fiscal_number,
                }
                result = MRACallResult(
                    ok=False,
                    dry_run=True,
                    status_code=0,
                    endpoint=client._resolve_endpoint(endpoint_key),
                    data=response_data,
                )
            elif (
                is_b2b_sale
                and is_online
                and POSOrderSubmissionService._is_network_only_submission_failure(
                    exc,
                    status_code=status_code,
                    response_data=response_data,
                )
            ):
                raise MRAIntegrationError(
                    'B2B EIS sales require MRA online confirmation. Connect to internet and retry.',
                    status_code=status_code,
                    endpoint=getattr(exc, 'endpoint', None) or client._resolve_endpoint(endpoint_key),
                    endpoint_key=endpoint_key,
                    response_data=response_data,
                ) from exc
            elif is_b2b_sale:
                response_inner = (
                    response_data.get('data')
                    if isinstance(response_data.get('data'), dict)
                    else {}
                )
                detail = (
                    response_data.get('remark')
                    or response_inner.get('remark')
                    or response_data.get('error')
                    or response_inner.get('error')
                    or response_data.get('raw')
                    or str(exc)
                )
                raise MRAIntegrationError(
                    f'B2B sale was not accepted by MRA: {detail}',
                    status_code=status_code,
                    endpoint=getattr(exc, 'endpoint', None) or client._resolve_endpoint(endpoint_key),
                    endpoint_key=endpoint_key,
                    response_data=response_data,
                ) from exc
            elif status_code >= 500:
                response_data = {
                    **response_data,
                    'httpStatusCode': status_code,
                    'status': 'prepared',
                    'reason': 'mra_server_error',
                    'error': str(exc),
                }
                logger.warning('POS order submission failed on MRA server, storing as prepared: %s', exc)
                result = MRACallResult(
                    ok=False,
                    dry_run=True,
                    status_code=status_code,
                    endpoint=getattr(exc, 'endpoint', None) or client._resolve_endpoint(endpoint_key),
                    data=response_data,
                )
            elif status_code >= 400:
                response_data = {**response_data, 'httpStatusCode': status_code}
                logger.warning('POS order submission rejected by MRA: %s', exc)
                result = MRACallResult(
                    ok=False,
                    dry_run=False,
                    status_code=status_code,
                    endpoint=getattr(exc, 'endpoint', None) or client._resolve_endpoint(endpoint_key),
                    data=response_data,
                )
            else:
                logger.warning('POS order submission call failed, storing as prepared: %s', exc)
                result = MRACallResult(
                    ok=False,
                    dry_run=True,
                    status_code=status_code,
                    endpoint=getattr(exc, 'endpoint', None) or client._resolve_endpoint(endpoint_key),
                    data={'status': 'prepared', 'reason': 'submission_call_failed', 'error': str(exc)},
                )
        except Exception as exc:
            if is_b2b_sale and is_online:
                raise MRAIntegrationError(
                    'B2B EIS sales require MRA online confirmation. Connect to internet and retry.'
                ) from exc
            logger.warning('POS order submission call failed, storing as prepared: %s', exc)
            result = MRACallResult(
                ok=False,
                dry_run=True,
                status_code=0,
                endpoint=client._resolve_endpoint(endpoint_key),
                data={'status': 'prepared', 'reason': 'submission_call_failed', 'error': str(exc)},
            )

        retryable_online_failure_reasons = {
            'submission_call_failed',
            'connection_error',
            'timeout',
            'network_error',
            'eis_unreachable',
            'mra_server_error',
        }
        result_data = MRAEISClient._normalize_response_data(result.data)
        result_reason = str(result_data.get('reason') or '').strip().lower()
        if (
            result.dry_run
            and is_online
            and not had_fiscal_number
            and not is_b2b_sale
            and result_reason in retryable_online_failure_reasons
        ):
            logger.warning(
                'POS online submission for order %s is retryable (%s); issuing offline receipt.',
                order.id,
                result_reason,
            )
            terminal.is_online = False
            terminal.save(update_fields=['is_online', 'updated_at'])
            is_online = False
            endpoint_key = 'report_sale_offline'

            POSOrderSubmissionService._enforce_offline_limits(
                order,
                terminal,
                is_online,
                is_new_offline_issue=True,
            )
            order.fiscal_invoice_number = fiscal_number
            payload = POSOrderSubmissionService.build_pos_order_payload(
                order,
                terminal,
                is_online,
                buyer_tin=buyer_tin,
                buyer_name=buyer_name,
                invoice_date_time=invoice_date_time,
                time_sync_metadata=time_sync_metadata,
            )
            official_tax_breakdown = payload.get('invoiceSummary', {}).get('taxBreakDown', [])
            calculated_net_amount, calculated_tax_amount, calculated_gross_amount = (
                POSOrderSubmissionService._persist_mra_amount_snapshot(order, payload)
            )
            offline_signature = POSOrderSubmissionService._apply_offline_signature(
                payload,
                terminal,
                is_online,
            )
            offline_validation_url = str(
                (payload.get('handyPosMetadata') or {}).get('offlineValidationURL') or ''
            )
            submission_payload = POSOrderSubmissionService._mra_payload_only(payload)
            result = MRACallResult(
                ok=False,
                dry_run=True,
                status_code=result.status_code,
                endpoint=client._resolve_endpoint(endpoint_key),
                data={
                    **result_data,
                    'status': 'prepared',
                    'reason': 'network_offline_fallback',
                    'original_endpoint': 'report_sale',
                },
            )

        prepared_meta = {
            'prepared': True,
            'dry_run': result.dry_run,
            'endpoint': endpoint_key,
            'prepared_at': timezone.now().isoformat(),
            'buyer_tin': buyer_tin,
            'buyer_name': buyer_name,
        }
        fallback_signature = (
            offline_signature
            or hashlib.sha256(json.dumps(submission_payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()
        )
        response_data = MRAEISClient._normalize_response_data(result.data)
        response_inner = response_data.get('data') if isinstance(response_data.get('data'), dict) else {}
        validation_url = (
            response_inner.get('validationURL')
            or response_inner.get('validationUrl')
            or response_data.get('validationURL')
            or response_data.get('validationUrl')
            or ''
        )
        response_errors = _extract_mra_response_errors(response_data)
        if (not result.dry_run) and is_online and not response_errors and not validation_url:
            response_errors.append('MRA response did not include validationURL required for fiscal receipt')

        if result.dry_run:
            order.eis_status = 'PENDING'
            order.eis_submitted_at = None
            order.eis_uuid = None
            order.qr_code_payload = offline_validation_url or json.dumps(prepared_meta)
            order.digital_signature = fallback_signature
        elif response_errors:
            order.eis_status = 'REJECTED'
            order.eis_submitted_at = timezone.now()
            order.eis_uuid = validation_url or None
            order.qr_code_payload = validation_url or json.dumps({**prepared_meta, 'errors': response_errors})
            order.digital_signature = (
                response_data.get('digitalSignature')
                or response_data.get('digital_signature')
                or fallback_signature
            )
        else:
            order.eis_status = 'SUBMITTED'
            order.eis_submitted_at = timezone.now()
            order.eis_uuid = (
                response_data.get('eisUuid')
                or response_data.get('eis_uuid')
                or response_data.get('invoiceUuid')
                or response_data.get('invoice_uuid')
                or validation_url
                or None
            )
            order.qr_code_payload = (
                response_data.get('qrCodePayload')
                or response_data.get('qr_code_payload')
                or validation_url
                or json.dumps(prepared_meta)
            )
            order.digital_signature = (
                response_data.get('digitalSignature')
                or response_data.get('digital_signature')
                or fallback_signature
            )

        if (not result.dry_run) and ConfigurationService.response_requests_latest_config(response_data):
            try:
                ConfigurationService.fetch_and_store_configuration(order.business, terminal=terminal)
            except Exception as config_exc:
                logger.warning('Latest config sync after POS order response failed: %s', config_exc)

        if (not result.dry_run) and response_inner.get('shouldBlockTerminal') is True:
            TerminalService.record_terminal_blocked(
                terminal,
                reason=(
                    response_inner.get('blockingReason')
                    or response_inner.get('blocking_reason')
                    or response_data.get('remark')
                    or 'MRA sale response requested terminal block'
                ),
                source='mra_sale_response_terminal_block',
                response_data=response_data,
            )
            try:
                TerminalService.get_terminal_blocking_message(terminal)
            except Exception as block_exc:
                logger.warning('Could not fetch MRA terminal blocking message for %s: %s', terminal.terminal_id, block_exc)

        order.save(
            update_fields=[
                'subtotal',
                'total',
                'net_amount',
                'vat_amount',
                'gross_amount',
                'tax_rate_value',
                'tax_type',
                'fiscal_invoice_number',
                'eis_status',
                'eis_submitted_at',
                'eis_uuid',
                'qr_code_payload',
                'digital_signature',
                'updated_at',
            ]
        )

        if result.dry_run and is_online:
            try:
                RetryService.queue_retry(
                    terminal,
                    'submit_pos_order',
                    {'order_id': str(order.id)},
                )
            except Exception as retry_exc:
                logger.warning('Failed to queue POS order retry for %s: %s', order.id, retry_exc)

        # Track against MRAInvoice for replay/submission readiness.
        seller_tin = str(payload.get('invoiceHeader', {}).get('sellerTIN') or order.business.tin or '').strip()
        invoice_defaults = {
            'business': order.business,
            'branch': order.branch,
            'terminal': terminal,
            'fiscal_julian_date': fiscal_julian_date,
            'seller_tin': seller_tin,
            'seller_name': order.business.name,
            'buyer_tin': buyer_tin,
            'buyer_name': buyer_name,
            'items': payload['invoiceLineItems'],
            'net_amount': calculated_net_amount,
            'tax_amount': calculated_tax_amount,
            'gross_amount': calculated_gross_amount,
            'tax_breakdown': {
                'standard': str(calculated_tax_amount),
                'zero': '0',
                'exempt': '0',
                'byRate': official_tax_breakdown,
            },
            'invoice_signature': order.digital_signature or '',
            'status': 'draft',
            'is_online': is_online,
            'invoice_date': invoice_date_time,
            'mra_response': {
                'source': 'pos_order_preparation',
                'order_id': str(order.id),
                'payload': submission_payload,
                'local_metadata': payload.get('handyPosMetadata', {}),
                'time_sync': time_sync_metadata,
                'dry_run': result.dry_run,
                'endpoint': endpoint_key,
                'terminal_online': bool(terminal.is_online),
                'terminal_has_token': bool(str(getattr(terminal, 'mra_token', '') or '').strip()),
                'response': response_data,
                'errors': response_errors,
            },
        }

        mra_invoice_status = 'draft' if result.dry_run else ('rejected' if response_errors else 'submitted')
        mra_invoice_submitted_at = None if result.dry_run else timezone.now()
        mra_invoice_id = (
            response_data.get('invoiceId')
            or response_data.get('invoice_id')
            or response_data.get('eisUuid')
            or response_data.get('eis_uuid')
            or validation_url
            or ''
        )

        mra_invoice, _ = MRAInvoice.objects.update_or_create(
            terminal=terminal,
            fiscal_julian_date=fiscal_julian_date,
            invoice_number=sequence_number,
            defaults={
                **invoice_defaults,
                'is_online': is_online,
                'status': mra_invoice_status,
                'submitted_at': mra_invoice_submitted_at,
                'mra_invoice_id': mra_invoice_id,
            },
        )

        queue_entry = None
        if (not is_online) and result.dry_run:
            # Persist offline transaction for ordered replay when connectivity returns.
            queue_entry = InvoiceService.queue_offline_invoice(mra_invoice)

        submission_diagnostic = POSOrderSubmissionService._build_sale_submission_diagnostic(
            result=result,
            endpoint_key=endpoint_key,
            is_online=is_online,
            response_data=response_data,
            response_errors=response_errors,
            queue_entry=queue_entry,
            offline_validation_url=offline_validation_url,
        )
        existing_validation_metadata = (
            order.eis_validation_metadata
            if isinstance(order.eis_validation_metadata, dict)
            else {}
        )
        order.eis_validation_metadata = {
            **existing_validation_metadata,
            'mra_submission': submission_diagnostic,
        }
        order.__class__.objects.filter(pk=order.pk).update(
            eis_validation_metadata=order.eis_validation_metadata,
            updated_at=timezone.now(),
        )

        if mra_invoice.status in {'submitted', 'offline_queued', 'offline_synced'}:
            try:
                from .receipt import ReceiptService

                ReceiptService.generate_receipt(mra_invoice)
            except Exception as receipt_exc:
                logger.warning('Failed to generate EIS receipt for POS order %s: %s', order.id, receipt_exc)

        InvoiceAuditLog.objects.create(
            mra_invoice=mra_invoice,
            action='created',
            details={
                'from_pos_order': str(order.id),
                'fiscal_invoice_number': fiscal_number,
                'dry_run': result.dry_run,
                'queued_offline': bool(queue_entry),
                'mra_response_errors': response_errors,
                'mra_submission': submission_diagnostic,
            },
        )

        return {
            'order_id': str(order.id),
            'fiscal_invoice_number': fiscal_number,
            'eis_status': order.eis_status,
            'dry_run': result.dry_run,
            'endpoint': endpoint_key,
            'response': response_data,
            'errors': response_errors,
            'offline_signature': offline_signature,
            'offline_validation_url': offline_validation_url,
            'submission_state': submission_diagnostic['state'],
            'submission_message': submission_diagnostic['message'],
            'retryable': submission_diagnostic['retryable'],
            'queued_offline': submission_diagnostic['queued_offline'],
        }

    @staticmethod
    def submit_pos_order_to_mra(pos_order, eis_uuid, qr_code_payload, digital_signature):
        """
        Backward-compatible manual finalization method.
        """
        if pos_order.eis_status == 'SUBMITTED':
            raise ValueError('Order already submitted to MRA')

        if pos_order.is_fiscal_locked:
            raise ValueError('Cannot submit locked order')

        if not all([eis_uuid, qr_code_payload, digital_signature]):
            raise ValueError('eis_uuid, qr_code_payload, and digital_signature are required')

        pos_order.eis_uuid = eis_uuid
        pos_order.qr_code_payload = qr_code_payload
        pos_order.digital_signature = digital_signature
        pos_order.eis_status = 'SUBMITTED'
        pos_order.eis_submitted_at = timezone.now()
        pos_order.save()

        return pos_order

    @staticmethod
    def get_pending_pos_orders(business=None, branch=None):
        from pos_sessions.models import Order

        queryset = Order.objects.filter(eis_status='PENDING')
        if business:
            queryset = queryset.filter(business=business)
        if branch:
            queryset = queryset.filter(branch=branch)
        return queryset

    @staticmethod
    def get_submitted_pos_orders(business=None, branch=None):
        from pos_sessions.models import Order

        queryset = Order.objects.filter(eis_status='SUBMITTED')
        if business:
            queryset = queryset.filter(business=business)
        if branch:
            queryset = queryset.filter(branch=branch)
        return queryset

    @staticmethod
    def get_locked_pos_orders(business=None, branch=None):
        from pos_sessions.models import Order

        queryset = Order.objects.filter(is_fiscal_locked=True)
        if business:
            queryset = queryset.filter(business=business)
        if branch:
            queryset = queryset.filter(branch=branch)
        return queryset

    @staticmethod
    def batch_submit_pos_orders(orders_data):
        from pos_sessions.models import Order

        results = {'success': 0, 'failed': 0, 'errors': []}

        for order_data in orders_data:
            try:
                order = Order.objects.get(id=order_data['order_id'])
                POSOrderSubmissionService.submit_pos_order_to_mra(
                    order,
                    order_data['eis_uuid'],
                    order_data['qr_code_payload'],
                    order_data['digital_signature'],
                )
                results['success'] += 1
            except Exception as exc:
                results['failed'] += 1
                results['errors'].append(
                    {
                        'order_id': order_data.get('order_id'),
                        'error': str(exc),
                    }
                )

        return results

    @staticmethod
    def prepare_pending_pos_orders(business=None, branch=None, limit=100):
        """Prepare pending orders for MRA submission pipeline without live submission."""
        queryset = POSOrderSubmissionService.get_pending_pos_orders(business=business, branch=branch)
        queryset = queryset.exclude(status__in=['Voided', 'Cancelled']).order_by('created_at')[:limit]

        prepared = 0
        failed = 0
        errors: list[dict[str, str]] = []

        for order in queryset:
            try:
                POSOrderSubmissionService.prepare_pos_order_submission(order)
                prepared += 1
            except Exception as exc:
                failed += 1
                errors.append({'order_id': str(order.id), 'error': str(exc)})

        return {
            'prepared': prepared,
            'failed': failed,
            'errors': errors,
        }


class TransactionReconciliationService:
    """Reconcile local sale state with MRA's last submitted transaction endpoints."""

    ENDPOINTS = {
        'online': 'get_last_online_transaction',
        'offline': 'get_last_offline_transaction',
    }

    @staticmethod
    def _response_inner(response_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response_data, dict):
            return {}
        data = response_data.get('data')
        return data if isinstance(data, dict) else response_data

    @staticmethod
    def _extract_invoice_number(response_data: dict[str, Any]) -> str:
        inner = TransactionReconciliationService._response_inner(response_data)
        header = inner.get('invoiceHeader') if isinstance(inner.get('invoiceHeader'), dict) else {}
        return str(
            header.get('invoiceNumber')
            or inner.get('invoiceNumber')
            or inner.get('receiptNumber')
            or ''
        ).strip()

    @staticmethod
    def _extract_validation_url(response_data: dict[str, Any]) -> str:
        inner = TransactionReconciliationService._response_inner(response_data)
        return str(
            inner.get('validationURL')
            or inner.get('validationUrl')
            or response_data.get('validationURL')
            or response_data.get('validationUrl')
            or ''
        ).strip()

    @staticmethod
    def _extract_remote_uuid(response_data: dict[str, Any], fallback: str) -> str:
        inner = TransactionReconciliationService._response_inner(response_data)
        return str(
            inner.get('eisUuid')
            or inner.get('eis_uuid')
            or inner.get('invoiceUuid')
            or inner.get('invoice_uuid')
            or inner.get('requestReference')
            or response_data.get('eisUuid')
            or response_data.get('eis_uuid')
            or response_data.get('invoiceUuid')
            or response_data.get('invoice_uuid')
            or fallback
            or ''
        ).strip()

    @staticmethod
    def _extract_submitted_at(response_data: dict[str, Any]):
        inner = TransactionReconciliationService._response_inner(response_data)
        raw_value = inner.get('dateSubmitted') or inner.get('submittedAt') or response_data.get('dateSubmitted')
        if raw_value:
            try:
                parsed = datetime.fromisoformat(str(raw_value).replace('Z', '+00:00'))
                if parsed.year > 1900:
                    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)
            except (TypeError, ValueError):
                pass
        return timezone.now()

    @staticmethod
    def _find_mra_invoice(terminal: Terminal, *, mode: str, invoice_number: str, sequence: int) -> MRAInvoice | None:
        is_online = mode == 'online'
        invoice = (
            MRAInvoice.objects.filter(
                terminal=terminal,
                is_online=is_online,
                mra_response__payload__invoiceHeader__invoiceNumber=invoice_number,
            )
            .order_by('-created_at')
            .first()
        )
        if invoice:
            return invoice
        if sequence > 0:
            return (
                MRAInvoice.objects.filter(
                    terminal=terminal,
                    is_online=is_online,
                    invoice_number=sequence,
                )
                .order_by('-created_at')
                .first()
            )
        return None

    @staticmethod
    def _find_pos_order(terminal: Terminal, invoice_number: str):
        try:
            from pos_sessions.models import Order
        except Exception:
            return None

        return (
            Order.objects.filter(
                business=terminal.business,
                fiscal_invoice_number=invoice_number,
            )
            .order_by('-created_at')
            .first()
        )

    @staticmethod
    def _advance_terminal_counter(terminal: Terminal, *, mode: str, sequence: int) -> bool:
        if sequence <= 0:
            return False
        field = 'online_invoice_counter' if mode == 'online' else 'offline_invoice_counter'
        current = int(getattr(terminal, field, 0) or 0)
        if sequence <= current:
            return False
        setattr(terminal, field, sequence)
        terminal.save(update_fields=[field, 'updated_at'])
        return True

    @staticmethod
    def _complete_retry_rows(terminal: Terminal, *, order=None, invoice: MRAInvoice | None = None) -> int:
        now = timezone.now()
        completed = 0
        retries = SyncRetryQueue.objects.filter(
            terminal=terminal,
            status__in=['pending', 'processing'],
        )
        for retry in retries:
            payload = retry.payload if isinstance(retry.payload, dict) else {}
            matches_order = order is not None and str(payload.get('order_id') or '') == str(order.id)
            matches_invoice = invoice is not None and str(payload.get('invoice_id') or '') == str(invoice.id)
            if not (matches_order or matches_invoice):
                continue
            if retry.operation_type not in {'submit_pos_order', 'submit_invoice'}:
                continue
            retry.status = 'completed'
            retry.completed_at = now
            retry.last_error = ''
            retry.save(update_fields=['status', 'completed_at', 'last_error'])
            completed += 1
        return completed

    @staticmethod
    def _mark_local_records_confirmed(
        *,
        terminal: Terminal,
        mode: str,
        invoice_number: str,
        sequence: int,
        response_data: dict[str, Any],
        endpoint_key: str,
    ) -> dict[str, Any]:
        now = timezone.now()
        submitted_at = TransactionReconciliationService._extract_submitted_at(response_data)
        validation_url = TransactionReconciliationService._extract_validation_url(response_data)
        remote_uuid = TransactionReconciliationService._extract_remote_uuid(response_data, invoice_number)
        invoice = TransactionReconciliationService._find_mra_invoice(
            terminal,
            mode=mode,
            invoice_number=invoice_number,
            sequence=sequence,
        )
        order = TransactionReconciliationService._find_pos_order(terminal, invoice_number)
        updated: list[str] = []

        if order:
            order_update = {
                'eis_status': 'SUBMITTED',
                'eis_submitted_at': submitted_at,
                'eis_uuid': remote_uuid or order.eis_uuid,
                'is_fiscal_locked': True,
                'is_dirty': False,
                'updated_at': now,
            }
            if validation_url:
                order_update['qr_code_payload'] = validation_url
            try:
                type(order).objects.filter(pk=order.pk).update(**order_update)
                updated.append('pos_order')
                order.refresh_from_db()
            except Exception as exc:
                logger.warning('Could not reconcile POS order %s from MRA last %s transaction: %s', order.id, mode, exc)

        if invoice:
            current_response = invoice.mra_response if isinstance(invoice.mra_response, dict) else {}
            invoice.status = 'offline_synced' if mode == 'offline' else 'submitted'
            invoice.submitted_at = submitted_at
            invoice.mra_invoice_id = remote_uuid or validation_url or invoice.mra_invoice_id
            invoice.mra_response = {
                **current_response,
                'response': response_data,
                'reconciliation': {
                    'mode': mode,
                    'endpoint_key': endpoint_key,
                    'invoiceNumber': invoice_number,
                    'checked_at': now.isoformat(),
                    'previous_response': current_response.get('response'),
                    'response': response_data,
                },
            }
            invoice.save(update_fields=['status', 'submitted_at', 'mra_invoice_id', 'mra_response', 'updated_at'])
            updated.append('mra_invoice')
            InvoiceAuditLog.objects.create(
                mra_invoice=invoice,
                action='synced_from_offline' if mode == 'offline' else 'accepted',
                details={
                    'source': 'last_transaction_reconciliation',
                    'mode': mode,
                    'invoiceNumber': invoice_number,
                    'endpoint_key': endpoint_key,
                },
            )

            if mode == 'offline':
                queue_updated = OfflineInvoiceQueue.objects.filter(mra_invoice=invoice).update(
                    status='synced',
                    synced_at=submitted_at,
                    last_sync_error='',
                )
                if queue_updated:
                    updated.append('offline_queue')

            try:
                from .receipt import ReceiptService

                ReceiptService.generate_receipt(invoice)
            except Exception as receipt_exc:
                logger.warning('Failed to generate receipt during reconciliation for invoice %s: %s', invoice.id, receipt_exc)

        completed_retries = TransactionReconciliationService._complete_retry_rows(
            terminal,
            order=order,
            invoice=invoice,
        )
        if completed_retries:
            updated.append('retry_queue')

        return {
            'matched': bool(order or invoice),
            'invoiceNumber': invoice_number,
            'sequence': sequence,
            'order_id': str(order.id) if order else None,
            'mra_invoice_id': str(invoice.id) if invoice else None,
            'updated': updated,
            'completed_retries': completed_retries,
            'validation_url': validation_url,
        }

    @staticmethod
    @transaction.atomic
    def reconcile_mode(terminal: Terminal, mode: str) -> dict[str, Any]:
        endpoint_key = TransactionReconciliationService.ENDPOINTS.get(mode)
        if not endpoint_key:
            raise ValueError(f'Unsupported reconciliation mode: {mode}')

        client = MRAEISClient(terminal=terminal)
        result = client.call(
            endpoint_key,
            payload=None,
            method='POST',
            mutating=False,
            send_json=False,
        )
        response_data = MRAEISClient._normalize_response_data(result.data)
        response_errors = _extract_mra_response_errors(response_data)
        invoice_number = TransactionReconciliationService._extract_invoice_number(response_data)

        if result.dry_run:
            return {
                'checked': False,
                'dry_run': True,
                'endpoint': result.endpoint,
                'endpoint_key': endpoint_key,
                'matched': False,
                'response': response_data,
            }

        if response_errors:
            return {
                'checked': True,
                'dry_run': False,
                'endpoint': result.endpoint,
                'endpoint_key': endpoint_key,
                'matched': False,
                'errors': response_errors,
                'response': response_data,
            }

        if not invoice_number:
            return {
                'checked': True,
                'dry_run': False,
                'endpoint': result.endpoint,
                'endpoint_key': endpoint_key,
                'matched': False,
                'reason': 'no_remote_invoice_number',
                'response': response_data,
            }

        sequence = POSOrderSubmissionService._extract_sequence_from_fiscal_number(invoice_number)
        counter_updated = TransactionReconciliationService._advance_terminal_counter(
            terminal,
            mode=mode,
            sequence=sequence,
        )
        reconciliation = TransactionReconciliationService._mark_local_records_confirmed(
            terminal=terminal,
            mode=mode,
            invoice_number=invoice_number,
            sequence=sequence,
            response_data=response_data,
            endpoint_key=endpoint_key,
        )
        reconciliation.update(
            {
                'checked': True,
                'dry_run': False,
                'endpoint': result.endpoint,
                'endpoint_key': endpoint_key,
                'terminal_counter_updated': counter_updated,
                'response': response_data,
            }
        )
        return reconciliation

    @staticmethod
    def reconcile_terminal(terminal: Terminal, modes: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        selected_modes = list(modes or ('online', 'offline'))
        results: dict[str, Any] = {}
        matched_count = 0
        unmatched: list[dict[str, Any]] = []

        for mode in selected_modes:
            normalized_mode = str(mode or '').strip().lower()
            if normalized_mode not in TransactionReconciliationService.ENDPOINTS:
                results[normalized_mode or mode] = {
                    'checked': False,
                    'matched': False,
                    'error': f'Unsupported reconciliation mode: {mode}',
                }
                continue
            try:
                result = TransactionReconciliationService.reconcile_mode(terminal, normalized_mode)
                results[normalized_mode] = result
                if result.get('matched'):
                    matched_count += 1
                elif result.get('checked'):
                    unmatched.append(
                        {
                            'mode': normalized_mode,
                            'invoiceNumber': result.get('invoiceNumber'),
                            'reason': result.get('reason') or result.get('errors') or 'not_found_locally',
                        }
                    )
            except Exception as exc:
                logger.warning('MRA last %s transaction reconciliation failed for terminal %s: %s', normalized_mode, terminal.terminal_id, exc)
                results[normalized_mode] = {
                    'checked': False,
                    'matched': False,
                    'error': str(exc),
                }

        terminal.last_sync_at = timezone.now()
        terminal.save(update_fields=['last_sync_at', 'updated_at'])

        return {
            'terminal_id': str(terminal.id),
            'mra_terminal_id': terminal.mra_terminal_id,
            'checked_at': terminal.last_sync_at.isoformat() if terminal.last_sync_at else timezone.now().isoformat(),
            'matched': matched_count,
            'unmatched': unmatched,
            'results': results,
        }


class ReceiptLookupService:
    """Read official MRA receipt records for certification/audit screens."""

    @staticmethod
    def _response_inner(response_data: dict[str, Any]) -> Any:
        if not isinstance(response_data, dict):
            return {}
        data = response_data.get('data')
        return data if data not in (None, '') else response_data

    @staticmethod
    def lookup_invoice_by_number(terminal: Terminal, invoice_number: str) -> dict[str, Any]:
        invoice_number = str(invoice_number or '').strip()
        if not invoice_number:
            raise MRAIntegrationError('Invoice number is required for MRA receipt lookup.')

        payload = {'invoiceNumber': invoice_number}
        result = MRAEISClient(terminal=terminal).call(
            'get_invoice_by_number',
            payload=payload,
            method='POST',
            mutating=False,
        )
        response_data = MRAEISClient._normalize_response_data(result.data)
        response_errors = _extract_mra_response_errors(response_data)
        inner = ReceiptLookupService._response_inner(response_data)

        return {
            'checked': not result.dry_run,
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'status_code': result.status_code,
            'payload': payload,
            'invoice_number': invoice_number,
            'found': not bool(response_errors) and bool(inner),
            'receipt': inner,
            'response': response_data,
            'errors': response_errors,
            'validation_url': (
                inner.get('validationURL')
                or inner.get('validationUrl')
                or response_data.get('validationURL')
                or response_data.get('validationUrl')
                or ''
            ) if isinstance(inner, dict) else '',
        }

    @staticmethod
    def get_void_receipts(
        terminal: Terminal,
        *,
        invoice_number: str = '',
        status_value: Any = None,
        start_date: str = '',
        end_date: str = '',
        page: Any = 1,
        page_size: Any = 25,
    ) -> dict[str, Any]:
        def _positive_int(value: Any, fallback: int, maximum: int = 200) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                parsed = fallback
            return min(max(parsed, 1), maximum)

        payload: dict[str, Any] = {
            'page': _positive_int(page, 1, 100000),
            'pageSize': _positive_int(page_size, 25, 200),
        }
        invoice_number = str(invoice_number or '').strip()
        if invoice_number:
            payload['invoiceNumber'] = invoice_number

        if status_value not in (None, ''):
            try:
                payload['status'] = int(status_value)
            except (TypeError, ValueError) as exc:
                raise MRAIntegrationError('Void receipt status must be a number when provided.') from exc

        start_date = str(start_date or '').strip()
        end_date = str(end_date or '').strip()
        if start_date:
            payload['startDate'] = start_date
        if end_date:
            payload['endDate'] = end_date

        result = MRAEISClient(terminal=terminal).call(
            'get_void_receipts',
            payload=payload,
            method='POST',
            mutating=False,
        )
        response_data = MRAEISClient._normalize_response_data(result.data)
        response_errors = _extract_mra_response_errors(response_data)
        inner = ReceiptLookupService._response_inner(response_data)

        items: list[Any] = []
        page_value = payload['page']
        page_size_value = payload['pageSize']
        total_count = 0
        if isinstance(inner, dict):
            raw_items = inner.get('items') or inner.get('results') or inner.get('voidReceipts') or []
            if isinstance(raw_items, list):
                items = raw_items
            page_value = inner.get('page') or page_value
            page_size_value = inner.get('pageSize') or page_size_value
            total_count = int(inner.get('totalCount') or inner.get('total') or len(items) or 0)
        elif isinstance(inner, list):
            items = inner
            total_count = len(items)

        return {
            'checked': not result.dry_run,
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'status_code': result.status_code,
            'payload': payload,
            'items': items,
            'page': page_value,
            'page_size': page_size_value,
            'total_count': total_count,
            'response': response_data,
            'errors': response_errors,
        }


class CorrectionService:
    """MRA EIS correction flows for credit notes, debit notes, and voids."""
    # MRA Swagger: /api/v1/sales/process-credit-debit-note accepts
    # InvoiceAdjustmentRequest. Keep this payload strict because the schema has
    # additionalProperties=false.
    ADJUSTMENT_REQUEST_KEYS = {'invoiceHeader', 'invoiceLineItems', 'invoiceSummary', 'reasonForAdjustment'}
    ADJUSTMENT_HEADER_KEYS = {
        'invoiceNumber',
        'invoiceDateTime',
        'sellerTIN',
        'buyerTIN',
        'buyerName',
        'buyerAuthorizationCode',
        'siteId',
        'globalConfigVersion',
        'taxpayerConfigVersion',
        'terminalConfigVersion',
        'isExport',
        'isReliefSupply',
        'vat5CertificateDetails',
        'paymentMethod',
    }
    ADJUSTMENT_VAT5_KEYS = {'id', 'projectNumber', 'certificateNumber', 'quantity'}
    ADJUSTMENT_LINE_ITEM_KEYS = {
        'id',
        'productCode',
        'description',
        'unitPrice',
        'quantity',
        'discount',
        'total',
        'totalVAT',
        'taxRateId',
        'isProduct',
    }
    ADJUSTMENT_SUMMARY_KEYS = {
        'taxBreakDown',
        'levyBreakDown',
        'totalVAT',
        'offlineSignature',
        'invoiceTotal',
        'amountTendered',
    }
    ADJUSTMENT_TAX_BREAKDOWN_KEYS = {'rateId', 'taxableAmount', 'taxAmount'}
    ADJUSTMENT_LEVY_BREAKDOWN_KEYS = {'levyTypeId', 'levyRate', 'levyAmount'}

    @staticmethod
    def _response_inner(response_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response_data, dict):
            return {}
        data = response_data.get('data')
        return data if isinstance(data, dict) else response_data

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        canonical_payload = json.dumps(payload, separators=(',', ':'), sort_keys=True, default=str)
        return hashlib.sha256(canonical_payload.encode('utf-8')).hexdigest()

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value or 0))
            return parsed if parsed.is_finite() else Decimal('0')
        except (InvalidOperation, TypeError, ValueError):
            return Decimal('0')

    @staticmethod
    def _money(value: Any) -> Decimal:
        parsed = CorrectionService._to_decimal(value)
        if parsed < 0:
            parsed = Decimal('0')
        return parsed.quantize(Decimal('0.01'))

    @staticmethod
    def _format_decimal(value: Any, places: str = '0.01') -> float:
        return POSOrderSubmissionService._format_decimal(value, places)

    @staticmethod
    def _schema_fields(source: dict[str, Any] | None, allowed_keys: set[str]) -> dict[str, Any]:
        if not isinstance(source, dict):
            return {}
        return {
            key: value
            for key, value in source.items()
            if key in allowed_keys and value is not None
        }

    @staticmethod
    def _supporting_documents_byte_string(value: Any) -> str:
        """
        MRA Swagger defines VoidReceiptCreateDto.supportingDocuments as one
        string(byte), so the outbound cancel-receipt payload must contain a
        single base64 document instead of local arrays/objects.
        """
        if value in (None, '', []):
            return ''

        if isinstance(value, dict):
            nested = value.get('supportingDocuments') or value.get('supporting_documents')
            if isinstance(nested, str):
                value = nested
            elif nested not in (None, '', []):
                value = nested

        if isinstance(value, str):
            document_text = value.replace('\r\n', '\n').replace('\r', '\n').strip()
        elif isinstance(value, list):
            lines: list[str] = []
            structured_items: list[Any] = []
            has_structured = False
            for item in value:
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
                structured_items.append(str(item))
                has_structured = True

            if has_structured:
                if lines:
                    structured_items.insert(0, {'references': lines})
                document_text = json.dumps(structured_items, sort_keys=True, separators=(',', ':'), default=str)
            else:
                document_text = '\n'.join(lines).strip()
        elif isinstance(value, dict):
            cleaned = {
                str(key): item_value
                for key, item_value in value.items()
                if item_value not in (None, '', [])
            }
            document_text = json.dumps(cleaned, sort_keys=True, separators=(',', ':'), default=str) if cleaned else ''
        else:
            document_text = str(value).strip()

        if not document_text:
            return ''
        return base64.b64encode(document_text.encode('utf-8')).decode('ascii')

    @staticmethod
    def _normalize_adjustment_tax_breakdown(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        return [
            CorrectionService._schema_fields(item, CorrectionService.ADJUSTMENT_TAX_BREAKDOWN_KEYS)
            for item in items
            if isinstance(item, dict)
        ]

    @staticmethod
    def _normalize_adjustment_levy_breakdown(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        return [
            CorrectionService._schema_fields(item, CorrectionService.ADJUSTMENT_LEVY_BREAKDOWN_KEYS)
            for item in items
            if isinstance(item, dict)
        ]

    @staticmethod
    def _normalize_invoice_adjustment_payload(
        payload: dict[str, Any],
        *,
        reason_for_adjustment: str,
    ) -> dict[str, Any]:
        """Return the exact InvoiceAdjustmentRequest shape expected by MRA Swagger."""
        header = CorrectionService._schema_fields(
            payload.get('invoiceHeader'),
            CorrectionService.ADJUSTMENT_HEADER_KEYS,
        )
        vat5_details = header.get('vat5CertificateDetails')
        if isinstance(vat5_details, dict):
            header['vat5CertificateDetails'] = CorrectionService._schema_fields(
                vat5_details,
                CorrectionService.ADJUSTMENT_VAT5_KEYS,
            )
        elif 'vat5CertificateDetails' in header:
            header.pop('vat5CertificateDetails', None)

        line_items = [
            CorrectionService._schema_fields(item, CorrectionService.ADJUSTMENT_LINE_ITEM_KEYS)
            for item in (payload.get('invoiceLineItems') or [])
            if isinstance(item, dict)
        ]
        summary = CorrectionService._schema_fields(
            payload.get('invoiceSummary'),
            CorrectionService.ADJUSTMENT_SUMMARY_KEYS,
        )
        summary['taxBreakDown'] = CorrectionService._normalize_adjustment_tax_breakdown(
            summary.get('taxBreakDown')
        )
        summary['levyBreakDown'] = CorrectionService._normalize_adjustment_levy_breakdown(
            summary.get('levyBreakDown')
        )

        missing_header = [
            key
            for key in (
                'invoiceNumber',
                'invoiceDateTime',
                'sellerTIN',
                'siteId',
                'globalConfigVersion',
                'taxpayerConfigVersion',
                'terminalConfigVersion',
            )
            if header.get(key) in (None, '')
        ]
        if missing_header:
            raise MRAIntegrationError(
                'Cannot submit MRA credit/debit note; missing invoiceHeader field(s): '
                + ', '.join(missing_header)
            )
        if not line_items:
            raise MRAIntegrationError('Cannot submit MRA credit/debit note without invoiceLineItems.')
        if not summary['taxBreakDown']:
            raise MRAIntegrationError('Cannot submit MRA credit/debit note without invoiceSummary.taxBreakDown.')

        adjusted_payload = {
            'invoiceHeader': header,
            'invoiceLineItems': line_items,
            'invoiceSummary': summary,
        }
        reason = str(reason_for_adjustment or '').strip()[:1000]
        if reason:
            adjusted_payload['reasonForAdjustment'] = reason
        return adjusted_payload

    @staticmethod
    def _assert_adjustment_direction(
        *,
        adjustment_type: str,
        original_total: Decimal,
        target_total: Decimal,
        original_vat: Decimal,
        target_vat: Decimal,
    ) -> None:
        if adjustment_type == 'credit' and not (target_total < original_total or target_vat < original_vat):
            raise MRAIntegrationError(
                'Credit note must reduce the original invoice VAT or total for MRA process-credit-debit-note.'
            )
        if adjustment_type == 'debit' and not (target_total > original_total or target_vat > original_vat):
            raise MRAIntegrationError(
                'Debit note must increase the original invoice VAT or total for MRA process-credit-debit-note.'
            )

    @staticmethod
    def _mra_status(result: MRACallResult, response_errors: list[str], response_inner: dict[str, Any]) -> str:
        if result.dry_run:
            return 'PENDING'
        if response_errors:
            return 'REJECTED'

        approval_status = str(
            response_inner.get('approvalStatus')
            or response_inner.get('status')
            or response_inner.get('statusDescription')
            or ''
        ).strip().lower()
        if approval_status in {'approved', 'accepted', 'success', 'successful'}:
            return 'ACCEPTED'
        if approval_status in {'rejected', 'declined', 'failed', 'failure'}:
            return 'REJECTED'
        return 'SUBMITTED'

    @staticmethod
    def _is_retryable_submission_error(exc: Exception) -> bool:
        """Return True only for transient correction submission failures."""
        status_code = getattr(exc, 'status_code', None)
        if status_code in (None, ''):
            return True
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            return True
        return status_code in {408, 429} or status_code >= 500

    @staticmethod
    def _api_error_type(exc: Exception) -> str:
        status_code = getattr(exc, 'status_code', None)
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            return 'connection_error'
        if status_code == 408:
            return 'timeout'
        if status_code == 429:
            return 'rate_limit'
        if status_code >= 500:
            return 'server_error'
        return 'invalid_request'

    @staticmethod
    def _resolve_endpoint_for_error(client: MRAEISClient, endpoint_key: str, exc: Exception) -> str:
        endpoint = getattr(exc, 'endpoint', None)
        if endpoint:
            return str(endpoint)
        try:
            return client._resolve_endpoint(endpoint_key)
        except Exception:
            return endpoint_key

    @staticmethod
    def _queue_correction_retry(
        *,
        terminal: Terminal,
        operation_type: str,
        payload: dict[str, Any],
        last_error: str = '',
        max_attempts: int = 5,
    ) -> SyncRetryQueue:
        retry = SyncRetryQueue.objects.filter(
            terminal=terminal,
            operation_type=operation_type,
            status__in=['pending', 'processing'],
            payload=payload,
        ).order_by('created_at').first()
        if retry:
            if last_error and retry.last_error != last_error:
                retry.last_error = last_error
                retry.save(update_fields=['last_error'])
            return retry

        retry = RetryService.queue_retry(
            terminal,
            operation_type,
            payload,
            max_attempts=max_attempts,
        )
        if last_error:
            retry.last_error = last_error
            retry.save(update_fields=['last_error'])
        return retry

    @staticmethod
    def _mark_correction_pending(
        *,
        correction,
        payload: dict[str, Any],
        endpoint: str,
        error: str,
        retry: SyncRetryQueue | None,
    ) -> dict[str, Any]:
        metadata = {
            'status': 'queued',
            'reason': 'retryable_mra_submission_failure',
            'endpoint': endpoint,
            'payload': payload,
            'error': error,
        }
        if retry:
            metadata['retry_queue_id'] = str(retry.id)

        correction.eis_status = 'PENDING'
        correction.eis_submitted_at = None
        correction.qr_code_payload = json.dumps(metadata, default=str)
        correction.digital_signature = CorrectionService._payload_hash(payload)
        correction.is_dirty = True
        correction.save(
            update_fields=[
                'eis_status',
                'eis_submitted_at',
                'qr_code_payload',
                'digital_signature',
                'is_dirty',
                'updated_at',
            ]
        )

        return {
            'dry_run': False,
            'queued': True,
            'endpoint': endpoint,
            'payload': payload,
            'response': {},
            'error': error,
            'errors': [error],
            'eis_status': 'PENDING',
            'retry_queue_id': str(retry.id) if retry else '',
        }

    @staticmethod
    def _is_original_sale_ready(order) -> bool:
        return bool(order.fiscal_invoice_number) and str(order.eis_status or '').upper() in {
            'SUBMITTED',
            'ACCEPTED',
        }

    @staticmethod
    def _ensure_original_sale_ready(order, *, force_online: bool = True) -> None:
        """
        MRA corrections can only reference an issued fiscal receipt.

        In dry-run mode a prepared fiscal number is enough for trial payload
        generation; in live HTTP mode MRA must have accepted the original sale
        submission before a correction/refund/void is sent.
        """
        if not order.fiscal_invoice_number:
            raise MRAIntegrationError('Original sale has no fiscal invoice number. Submit the sale to MRA first.')

        if not getattr(settings, 'MRA_EIS_DRY_RUN', True) and not CorrectionService._is_original_sale_ready(order):
            raise MRAIntegrationError(
                'Original sale is not confirmed by MRA yet. '
                'Correction, refund, or void cannot be submitted until the sale is fiscalized.'
            )

    @staticmethod
    def _submit_optional_void_stock_adjustments(order, reason: str) -> list[dict[str, Any]]:
        if not bool(getattr(settings, 'MRA_EIS_ADJUST_STOCK_ON_VOID', True)):
            return []

        try:
            from inventory.models import InventoryItem
        except Exception as exc:
            logger.warning('Could not import inventory model for EIS void stock adjustment: %s', exc)
            return [{'submitted': False, 'error': str(exc)}]

        results: list[dict[str, Any]] = []
        for order_item in order.items.all():
            inventory_item = InventoryItem.objects.filter(
                id=str(order_item.inventory_item_id),
                business=order.business,
                branch=order.branch,
            ).first()
            if not inventory_item:
                results.append({
                    'submitted': False,
                    'skipped': True,
                    'reason': 'inventory_item_not_found',
                    'order_item_id': str(order_item.id),
                    'inventory_item_id': str(order_item.inventory_item_id),
                })
                continue

            result = StockReceivingService.submit_inventory_item_adjustment(
                business=order.business,
                branch=order.branch,
                inventory_item=inventory_item,
                quantity=order_item.quantity,
                adjustment_type='Increase',
                reason='Void sale stock restoration',
                remarks=f"Void receipt {order.fiscal_invoice_number}: {reason}"[:500],
            )
            result['order_item_id'] = str(order_item.id)
            results.append(result)
        return results

    @staticmethod
    def _scale_line_items(
        *,
        business,
        line_items: list[dict[str, Any]],
        target_net: Decimal,
        target_vat: Decimal,
    ) -> list[dict[str, Any]]:
        if not line_items:
            return []

        original_net = sum(CorrectionService._money(item.get('total')) for item in line_items)
        original_vat = sum(CorrectionService._money(item.get('totalVAT')) for item in line_items)
        remaining_net = target_net
        remaining_vat = target_vat
        adjusted_items: list[dict[str, Any]] = []

        for index, item in enumerate(line_items):
            is_last = index == len(line_items) - 1
            item_net = CorrectionService._money(item.get('total'))
            item_vat = CorrectionService._money(item.get('totalVAT'))

            if is_last:
                adjusted_net = remaining_net
                adjusted_vat = remaining_vat
            else:
                net_ratio = item_net / original_net if original_net > 0 else Decimal('1') / Decimal(len(line_items))
                vat_ratio = item_vat / original_vat if original_vat > 0 else Decimal('1') / Decimal(len(line_items))
                adjusted_net = CorrectionService._money(target_net * net_ratio)
                adjusted_vat = CorrectionService._money(target_vat * vat_ratio)
                remaining_net -= adjusted_net
                remaining_vat -= adjusted_vat

            adjusted_net = max(Decimal('0.00'), adjusted_net)
            adjusted_vat = max(Decimal('0.00'), adjusted_vat)
            quantity = CorrectionService._to_decimal(item.get('quantity')) or Decimal('1')
            if quantity <= 0:
                quantity = Decimal('1')
            unit_price = adjusted_net / quantity

            adjusted = dict(item)
            adjusted['unitPrice'] = CorrectionService._format_decimal(unit_price)
            adjusted['total'] = CorrectionService._format_decimal(adjusted_net)
            adjusted['totalVAT'] = CorrectionService._format_decimal(adjusted_vat)
            adjusted_items.append(adjusted)

        return adjusted_items

    @staticmethod
    def _scale_levy_breakdown(
        levy_breakdown: list[dict[str, Any]],
        *,
        original_net: Decimal,
        target_net: Decimal,
    ) -> list[dict[str, Any]]:
        if not levy_breakdown:
            return []
        ratio = (target_net / original_net) if original_net > 0 else Decimal('0')
        scaled_rows: list[dict[str, Any]] = []
        remaining_amount = sum(CorrectionService._money(row.get('levyAmount')) for row in levy_breakdown)
        remaining_amount = CorrectionService._money(remaining_amount * ratio)

        for index, row in enumerate(levy_breakdown):
            is_last = index == len(levy_breakdown) - 1
            if is_last:
                levy_amount = remaining_amount
            else:
                levy_amount = CorrectionService._money(CorrectionService._money(row.get('levyAmount')) * ratio)
                remaining_amount -= levy_amount
            scaled_rows.append(
                {
                    'levyTypeId': str(row.get('levyTypeId') or '').strip(),
                    'levyRate': CorrectionService._format_decimal(row.get('levyRate') or 0),
                    'levyAmount': CorrectionService._format_decimal(max(Decimal('0.00'), levy_amount)),
                }
            )

        return [row for row in scaled_rows if row['levyTypeId']]

    @staticmethod
    def _build_adjustment_payload(
        *,
        note,
        adjustment_type: str,
        net_delta: Decimal,
        vat_delta: Decimal,
        reason: str,
    ) -> tuple[dict[str, Any], Terminal]:
        order = note.original_order
        CorrectionService._ensure_original_sale_ready(order)

        terminal = POSOrderSubmissionService._resolve_order_terminal(order)
        buyer_tin, buyer_name = POSOrderSubmissionService._resolve_buyer_details(order)
        base_payload = POSOrderSubmissionService.build_pos_order_payload(
            order,
            terminal,
            is_online=True,
            buyer_tin=buyer_tin,
            buyer_name=buyer_name,
        )
        payload = POSOrderSubmissionService._mra_payload_only(base_payload)

        original_net = CorrectionService._money(order.net_amount or order.subtotal)
        original_vat = CorrectionService._money(order.vat_amount)
        original_total = original_net + original_vat
        net_delta = CorrectionService._money(net_delta)
        vat_delta = CorrectionService._money(vat_delta)

        if adjustment_type == 'credit':
            target_net = max(Decimal('0.00'), original_net - net_delta)
            target_vat = max(Decimal('0.00'), original_vat - vat_delta)
        elif adjustment_type == 'debit':
            target_net = original_net + net_delta
            target_vat = original_vat + vat_delta
        else:
            raise ValueError(f'Unknown adjustment type: {adjustment_type}')

        target_total = target_net + target_vat
        CorrectionService._assert_adjustment_direction(
            adjustment_type=adjustment_type,
            original_total=original_total,
            target_total=target_total,
            original_vat=original_vat,
            target_vat=target_vat,
        )

        adjusted_items = CorrectionService._scale_line_items(
            business=order.business,
            line_items=payload.get('invoiceLineItems') or [],
            target_net=target_net,
            target_vat=target_vat,
        )
        levy_breakdown = CorrectionService._scale_levy_breakdown(
            payload.get('invoiceSummary', {}).get('levyBreakDown') or [],
            original_net=original_net,
            target_net=target_net,
        )
        target_levy = InvoiceService._sum_levy_breakdown(levy_breakdown)
        target_total_with_levy = CorrectionService._money(target_total + target_levy)
        payload['invoiceLineItems'] = adjusted_items
        payload['invoiceSummary'] = {
            **(payload.get('invoiceSummary') or {}),
            'taxBreakDown': InvoiceService._build_tax_breakdown(order.business, adjusted_items),
            'levyBreakDown': levy_breakdown,
            'totalVAT': CorrectionService._format_decimal(target_vat),
            'offlineSignature': None,
            'invoiceTotal': CorrectionService._format_decimal(target_total_with_levy),
            'amountTendered': CorrectionService._format_decimal(target_total_with_levy),
        }
        payload = CorrectionService._normalize_invoice_adjustment_payload(
            payload,
            reason_for_adjustment=reason,
        )
        return payload, terminal

    @staticmethod
    def _save_note_result(
        *,
        note,
        result: MRACallResult,
        payload: dict[str, Any],
        response_data: dict[str, Any],
        fiscal_field: str,
        local_number_field: str,
    ) -> dict[str, Any]:
        response_inner = CorrectionService._response_inner(response_data)
        response_errors = _extract_mra_response_errors(response_data)
        eis_status = CorrectionService._mra_status(result, response_errors, response_inner)
        validation_url = (
            response_inner.get('validationURL')
            or response_inner.get('validationUrl')
            or response_data.get('validationURL')
            or response_data.get('validationUrl')
            or ''
        )
        fiscal_number = (
            response_inner.get('invoiceNumber')
            or response_inner.get('noteNumber')
            or response_data.get('invoiceNumber')
            or response_data.get('noteNumber')
            or getattr(note, local_number_field)
        )
        eis_uuid = (
            response_inner.get('eisUuid')
            or response_inner.get('invoiceUuid')
            or response_inner.get('invoiceNumber')
            or response_data.get('eisUuid')
            or response_data.get('invoiceUuid')
            or fiscal_number
            or ''
        )
        original_invoice_number = (
            response_inner.get('originalInvoiceNumber')
            or response_inner.get('original_invoice_number')
            or response_data.get('originalInvoiceNumber')
            or response_data.get('original_invoice_number')
            or ''
        )
        note_type = (
            response_inner.get('noteType')
            or response_inner.get('note_type')
            or response_data.get('noteType')
            or response_data.get('note_type')
            or ''
        )
        response_metadata = {
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'fiscal_invoice_number': fiscal_number,
            'original_invoice_number': original_invoice_number,
            'note_type': note_type,
            'response': response_data,
        }

        setattr(note, fiscal_field, str(fiscal_number or '')[:100])
        note.eis_uuid = str(eis_uuid or '')[:100]
        note.eis_status = eis_status
        note.eis_submitted_at = None if result.dry_run else timezone.now()
        note.qr_code_payload = validation_url or json.dumps(response_metadata, default=str)
        note.digital_signature = (
            response_data.get('digitalSignature')
            or response_data.get('digital_signature')
            or CorrectionService._payload_hash(payload)
        )
        note.is_dirty = eis_status in {'PENDING', 'REJECTED'}
        note.save(
            update_fields=[
                fiscal_field,
                'eis_uuid',
                'eis_status',
                'eis_submitted_at',
                'qr_code_payload',
                'digital_signature',
                'is_dirty',
                'updated_at',
            ]
        )

        if response_errors and not result.dry_run:
            raise MRAIntegrationError(f"MRA rejected correction: {'; '.join(response_errors)}")

        return {
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'payload': payload,
            'response': response_data,
            'errors': response_errors,
            'eis_status': eis_status,
            'fiscal_invoice_number': str(fiscal_number or ''),
            'original_invoice_number': str(original_invoice_number or ''),
            'note_type': str(note_type or ''),
        }

    @staticmethod
    def submit_credit_note(
        credit_note,
        *,
        queue_on_dry_run: bool = True,
        queue_on_failure: bool = True,
    ) -> dict[str, Any]:
        reason = f"{credit_note.get_reason_display()}: {credit_note.description}"
        payload, terminal = CorrectionService._build_adjustment_payload(
            note=credit_note,
            adjustment_type='credit',
            net_delta=credit_note.credit_amount,
            vat_delta=credit_note.vat_amount,
            reason=reason,
        )

        client = MRAEISClient(terminal=terminal)
        try:
            result = client.call('process_credit_debit_note', payload=payload, method='POST', mutating=True)
        except Exception as exc:
            error_message = str(exc)
            MRAAPIError.objects.create(
                terminal=terminal,
                error_type=CorrectionService._api_error_type(exc),
                error_message=error_message,
                error_code=str(getattr(exc, 'status_code', '') or ''),
            )
            if queue_on_failure and CorrectionService._is_retryable_submission_error(exc):
                endpoint = CorrectionService._resolve_endpoint_for_error(
                    client,
                    'process_credit_debit_note',
                    exc,
                )
                retry = CorrectionService._queue_correction_retry(
                    terminal=terminal,
                    operation_type='submit_credit_note',
                    payload={'credit_note_id': str(credit_note.id)},
                    last_error=error_message,
                )
                logger.warning('Queued credit note %s for MRA retry: %s', credit_note.id, error_message)
                return CorrectionService._mark_correction_pending(
                    correction=credit_note,
                    payload=payload,
                    endpoint=endpoint,
                    error=error_message,
                    retry=retry,
                )
            raise

        response_data = result.data or {}
        service_result = CorrectionService._save_note_result(
            note=credit_note,
            result=result,
            payload=payload,
            response_data=response_data,
            fiscal_field='fiscal_credit_number',
            local_number_field='credit_note_number',
        )

        if result.dry_run and queue_on_dry_run:
            try:
                CorrectionService._queue_correction_retry(
                    terminal=terminal,
                    operation_type='submit_credit_note',
                    payload={'credit_note_id': str(credit_note.id)},
                    last_error='dry_run_submission_prepared',
                )
            except Exception as retry_exc:
                logger.warning('Failed to queue credit note retry for %s: %s', credit_note.id, retry_exc)

        return service_result

    @staticmethod
    def submit_debit_note(
        debit_note,
        *,
        queue_on_dry_run: bool = True,
        queue_on_failure: bool = True,
    ) -> dict[str, Any]:
        payload, terminal = CorrectionService._build_adjustment_payload(
            note=debit_note,
            adjustment_type='debit',
            net_delta=debit_note.additional_amount,
            vat_delta=debit_note.vat_amount,
            reason=debit_note.description,
        )

        client = MRAEISClient(terminal=terminal)
        try:
            result = client.call('process_credit_debit_note', payload=payload, method='POST', mutating=True)
        except Exception as exc:
            error_message = str(exc)
            MRAAPIError.objects.create(
                terminal=terminal,
                error_type=CorrectionService._api_error_type(exc),
                error_message=error_message,
                error_code=str(getattr(exc, 'status_code', '') or ''),
            )
            if queue_on_failure and CorrectionService._is_retryable_submission_error(exc):
                endpoint = CorrectionService._resolve_endpoint_for_error(
                    client,
                    'process_credit_debit_note',
                    exc,
                )
                retry = CorrectionService._queue_correction_retry(
                    terminal=terminal,
                    operation_type='submit_debit_note',
                    payload={'debit_note_id': str(debit_note.id)},
                    last_error=error_message,
                )
                logger.warning('Queued debit note %s for MRA retry: %s', debit_note.id, error_message)
                return CorrectionService._mark_correction_pending(
                    correction=debit_note,
                    payload=payload,
                    endpoint=endpoint,
                    error=error_message,
                    retry=retry,
                )
            raise

        response_data = result.data or {}
        service_result = CorrectionService._save_note_result(
            note=debit_note,
            result=result,
            payload=payload,
            response_data=response_data,
            fiscal_field='fiscal_debit_number',
            local_number_field='debit_note_number',
        )

        if result.dry_run and queue_on_dry_run:
            try:
                CorrectionService._queue_correction_retry(
                    terminal=terminal,
                    operation_type='submit_debit_note',
                    payload={'debit_note_id': str(debit_note.id)},
                    last_error='dry_run_submission_prepared',
                )
            except Exception as retry_exc:
                logger.warning('Failed to queue debit note retry for %s: %s', debit_note.id, retry_exc)

        return service_result

    @staticmethod
    def submit_void_transaction(
        void_transaction,
        *,
        queue_on_dry_run: bool = True,
        queue_on_failure: bool = True,
    ) -> dict[str, Any]:
        order = void_transaction.original_order
        CorrectionService._ensure_original_sale_ready(order)
        terminal = POSOrderSubmissionService._resolve_order_terminal(order)
        reason = str(void_transaction.reason_description or void_transaction.get_void_reason_display() or '').strip()
        payload = {
            'receiptNumber': str(order.fiscal_invoice_number),
            'reason': reason[:1000],
        }
        supporting_documents = CorrectionService._supporting_documents_byte_string(
            getattr(void_transaction, 'supporting_documents', None)
        )
        if supporting_documents:
            payload['supportingDocuments'] = supporting_documents

        client = MRAEISClient(terminal=terminal)
        try:
            result = client.call('cancel_receipt', payload=payload, method='POST', mutating=True)
        except Exception as exc:
            error_message = str(exc)
            MRAAPIError.objects.create(
                terminal=terminal,
                error_type=CorrectionService._api_error_type(exc),
                error_message=error_message,
                error_code=str(getattr(exc, 'status_code', '') or ''),
            )
            if queue_on_failure and CorrectionService._is_retryable_submission_error(exc):
                endpoint = CorrectionService._resolve_endpoint_for_error(client, 'cancel_receipt', exc)
                retry = CorrectionService._queue_correction_retry(
                    terminal=terminal,
                    operation_type='submit_void_transaction',
                    payload={'void_transaction_id': str(void_transaction.id)},
                    last_error=error_message,
                )
                logger.warning('Queued void transaction %s for MRA retry: %s', void_transaction.id, error_message)
                queued_result = CorrectionService._mark_correction_pending(
                    correction=void_transaction,
                    payload=payload,
                    endpoint=endpoint,
                    error=error_message,
                    retry=retry,
                )
                queued_result['stock_adjustments'] = []
                return queued_result
            raise

        response_data = result.data or {}
        response_inner = CorrectionService._response_inner(response_data)
        response_errors = _extract_mra_response_errors(response_data)
        eis_status = CorrectionService._mra_status(result, response_errors, response_inner)
        fiscal_void_number = (
            response_inner.get('invoiceNumber')
            or response_inner.get('receiptNumber')
            or response_data.get('invoiceNumber')
            or response_data.get('receiptNumber')
            or void_transaction.void_number
        )
        eis_uuid = (
            response_inner.get('requestReference')
            or response_inner.get('invoiceNumber')
            or response_data.get('requestReference')
            or response_data.get('invoiceNumber')
            or fiscal_void_number
            or ''
        )

        stock_adjustments = []
        if not response_errors and (eis_status in {'SUBMITTED', 'ACCEPTED'} or result.dry_run):
            stock_adjustments = CorrectionService._submit_optional_void_stock_adjustments(order, reason)

        qr_payload = {
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'response': response_data,
        }
        if stock_adjustments:
            qr_payload['stock_adjustments'] = stock_adjustments

        void_transaction.fiscal_void_number = str(fiscal_void_number or '')[:100]
        void_transaction.eis_uuid = str(eis_uuid or '')[:100]
        void_transaction.eis_status = eis_status
        void_transaction.eis_submitted_at = None if result.dry_run else timezone.now()
        void_transaction.qr_code_payload = json.dumps(qr_payload, default=str)
        void_transaction.digital_signature = (
            response_data.get('digitalSignature')
            or response_data.get('digital_signature')
            or CorrectionService._payload_hash(payload)
        )
        void_transaction.is_dirty = eis_status in {'PENDING', 'REJECTED'}
        void_transaction.save(
            update_fields=[
                'fiscal_void_number',
                'eis_uuid',
                'eis_status',
                'eis_submitted_at',
                'qr_code_payload',
                'digital_signature',
                'is_dirty',
                'updated_at',
            ]
        )

        if response_errors and not result.dry_run:
            raise MRAIntegrationError(f"MRA rejected void receipt: {'; '.join(response_errors)}")

        if result.dry_run and queue_on_dry_run:
            try:
                CorrectionService._queue_correction_retry(
                    terminal=terminal,
                    operation_type='submit_void_transaction',
                    payload={'void_transaction_id': str(void_transaction.id)},
                    last_error='dry_run_submission_prepared',
                )
            except Exception as retry_exc:
                logger.warning('Failed to queue void retry for %s: %s', void_transaction.id, retry_exc)

        return {
            'dry_run': result.dry_run,
            'endpoint': result.endpoint,
            'payload': payload,
            'response': response_data,
            'errors': response_errors,
            'eis_status': eis_status,
            'stock_adjustments': stock_adjustments,
        }
