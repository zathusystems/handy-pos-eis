from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

from ..models import Terminal, TerminalAuditLog
from .common import MRACallResult, MRAIntegrationError


class MRAEISClient:
    """Thin HTTP/signing wrapper around official MRA EIS endpoints."""


    def __init__(self, terminal: Terminal | None = None):
        self.terminal = terminal
        self.base_url = settings.MRA_EIS_BASE_URL.rstrip('/')
        self.timeout = settings.MRA_EIS_TIMEOUT_SECONDS
        self.verify_ssl = bool(getattr(settings, 'MRA_EIS_VERIFY_SSL', True))
        self.endpoints: dict[str, str] = settings.MRA_EIS_ENDPOINTS

    @property
    def http_enabled(self) -> bool:
        return bool(getattr(settings, 'MRA_EIS_ENABLE_HTTP_CALLS', False))

    @property
    def dry_run(self) -> bool:
        return bool(getattr(settings, 'MRA_EIS_DRY_RUN', True))

    @property
    def allow_live_submission(self) -> bool:
        return bool(getattr(settings, 'MRA_EIS_ALLOW_LIVE_SUBMISSION', False))

    def _resolve_endpoint(self, key: str) -> str:
        path = self.endpoints.get(key)
        if not path:
            raise MRAIntegrationError(f"MRA endpoint '{key}' is not configured")
        path = path if path.startswith('/') else f'/{path}'
        return f"{self.base_url}{path}"

    @staticmethod
    def _canonical_json(payload: dict[str, Any] | None) -> str:
        if payload is None:
            return '{}'
        return json.dumps(payload, separators=(',', ':'), sort_keys=True, default=str)

    @staticmethod
    def _compact_json(payload: dict[str, Any] | None) -> str:
        if payload is None:
            return '{}'
        return json.dumps(payload, separators=(',', ':'), sort_keys=False, default=str)

    @staticmethod
    def _raw_json(payload: dict[str, Any] | None) -> str:
        if payload is None:
            return '{}'
        return json.dumps(payload, sort_keys=False, default=str)

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    @staticmethod
    def _hmac_sha512_base64(message: str, secret: str) -> str:
        if message is None or not secret:
            return ''
        digest = hmac.new(
            secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha512,
        ).digest()
        return base64.b64encode(digest).decode('utf-8')

    @staticmethod
    def _requires_authorization(endpoint_key: str) -> bool:
        # Initial TAC activation is unauthenticated. MRA environments can require
        # the freshly returned terminal token on activation confirmation.
        return endpoint_key != 'activate_terminal'

    @staticmethod
    def _requires_message_hash(endpoint_key: str) -> bool:
        # MRA requires x-eis-message-hash on post-activation payload calls.
        # Confirmation is covered by x-signature.
        return endpoint_key not in {'activate_terminal', 'confirm_terminal'}

    def _terminal_secret(self) -> str:
        terminal_secret = str(getattr(self.terminal, 'mra_api_key', '') or '').strip()
        if terminal_secret:
            return terminal_secret
        return str(getattr(settings, 'MRA_EIS_SECRET_KEY', '') or '').strip()

    def _message_hash_input_mode(self) -> str:
        mode = str(getattr(settings, 'MRA_EIS_MESSAGE_HASH_INPUT_MODE', 'canonical_json') or '').strip().lower()
        if mode in {'canonical', 'canonical-json', 'canonical_json'}:
            return 'canonical_json'
        if mode in {'compact', 'compact-json', 'compact_json'}:
            return 'compact_json'
        if mode in {'raw', 'raw-json', 'raw_json'}:
            return 'raw_json'
        return 'canonical_json'

    def _message_hash_input(self, payload: dict[str, Any] | None, message_hash_text: str | None) -> tuple[str, str]:
        if message_hash_text is not None:
            return message_hash_text, 'explicit_message_hash_text'
        mode = self._message_hash_input_mode()
        if mode == 'compact_json':
            return self._compact_json(payload), mode
        if mode == 'raw_json':
            return self._raw_json(payload), mode
        return self._canonical_json(payload), mode

    def _build_message_hash(self, payload: dict[str, Any] | None, message_hash_text: str | None) -> str:
        secret = self._terminal_secret()
        if not secret:
            return ''
        message, _source = self._message_hash_input(payload, message_hash_text)
        return self._hmac_sha512_base64(message, secret)

    def _json_request_body(self, payload: dict[str, Any] | None) -> str:
        body, _source = self._message_hash_input(payload, None)
        return body

    def _build_headers(
        self,
        endpoint_key: str,
        payload: dict[str, Any] | None,
        *,
        x_signature_text: str | None = None,
        message_hash_text: str | None = None,
    ) -> dict[str, str]:
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'text/plain',
        }

        if x_signature_text:
            signature = self._hmac_sha512_base64(x_signature_text, self._terminal_secret())
            if signature:
                headers['x-signature'] = signature

        if self._requires_message_hash(endpoint_key):
            message_hash = self._build_message_hash(payload, message_hash_text)
            if message_hash:
                headers['x-eis-message-hash'] = message_hash

        if self._requires_authorization(endpoint_key) and self.terminal and self.terminal.mra_token:
            authorization = self._authorization_header_value(self.terminal.mra_token)
            if authorization:
                headers['Authorization'] = authorization

        return headers

    @staticmethod
    def _payload_shape(payload: Any) -> Any:
        if isinstance(payload, dict):
            return {
                str(key): MRAEISClient._payload_shape(value)
                for key, value in payload.items()
            }
        if isinstance(payload, list):
            return f'list[{len(payload)}]'
        return type(payload).__name__

    @staticmethod
    def _request_header_evidence(headers: dict[str, str]) -> dict[str, Any]:
        authorization = str(headers.get('Authorization') or '').strip()
        message_hash = str(headers.get('x-eis-message-hash') or '').strip()
        signature = str(headers.get('x-signature') or '').strip()
        access_key = str(headers.get('x-access-key') or '').strip()
        return {
            'authorization_present': bool(authorization),
            'authorization_scheme': 'Bearer' if authorization.lower().startswith('bearer ') else '',
            'x_access_key_present': bool(access_key),
            'x_signature_present': bool(signature),
            'x_signature_sha256': MRAEISClient._sha256_text(signature) if signature else '',
            'x_eis_message_hash_present': bool(message_hash),
            'x_eis_message_hash_sha256': MRAEISClient._sha256_text(message_hash) if message_hash else '',
            'x_eis_message_hash_length': len(message_hash),
        }

    def _build_message_hash_evidence(
        self,
        endpoint_key: str,
        endpoint: str,
        method_name: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        *,
        message_hash_text: str | None,
        request_body_text: str | None = None,
    ) -> dict[str, Any]:
        message, source = self._message_hash_input(payload, message_hash_text)
        evidence = {
            'endpoint_key': endpoint_key,
            'endpoint': endpoint,
            'method': method_name,
            'hash_algorithm': 'HMAC-SHA512',
            'hash_encoding': 'base64',
            'hash_input_source': source,
            'hash_input_mode': self._message_hash_input_mode(),
            'hash_input_sha256': self._sha256_text(message),
            'hash_input_length': len(message),
            'hash_input_confirmed_by_mra': bool(
                getattr(settings, 'MRA_EIS_MESSAGE_HASH_INPUT_CONFIRMED_BY_MRA', False)
            ),
            'payload_sha256': self._sha256_text(self._canonical_json(payload)),
            'payload_shape': self._payload_shape(payload or {}),
            'headers': self._request_header_evidence(headers),
            'requires_mra_confirmation': source != 'explicit_message_hash_text',
        }
        if request_body_text is not None:
            evidence['request_body_sha256'] = self._sha256_text(request_body_text)
            evidence['request_body_length'] = len(request_body_text)
            evidence['request_body_matches_hash_input'] = hmac.compare_digest(request_body_text, message)
        if bool(getattr(settings, 'MRA_EIS_LOG_MESSAGE_HASH_INPUT', False)):
            evidence['hash_input_text'] = message
        else:
            evidence['hash_input_preview'] = message[:160]
        return evidence

    def _record_hash_evidence(
        self,
        evidence: dict[str, Any],
        *,
        status_code: int | None = None,
        ok: bool | None = None,
        error: str = '',
    ) -> None:
        if not self.terminal or not getattr(self.terminal, 'pk', None):
            return
        if not bool(getattr(settings, 'MRA_EIS_RECORD_MESSAGE_HASH_EVIDENCE', True)):
            return
        details = dict(evidence)
        if status_code is not None:
            details['status_code'] = status_code
        if ok is not None:
            details['ok'] = bool(ok)
        if error:
            details['error'] = str(error)[:1000]
        try:
            TerminalAuditLog.objects.create(
                terminal=self.terminal,
                action='mra_request_signed',
                details=details,
            )
        except Exception:
            # Signing evidence must never mask the actual MRA request result.
            pass

    @staticmethod
    def _authorization_header_value(token: Any) -> str:
        token_value = str(token or '').strip()
        if token_value.lower().startswith('authorization:'):
            token_value = token_value.split(':', 1)[1].strip()
        while token_value.lower().startswith('bearer '):
            token_value = token_value.split(' ', 1)[1].strip()
        return f'Bearer {token_value}' if token_value else ''

    def _validate_security_requirements(
        self,
        endpoint_key: str,
        *,
        x_signature_text: str | None = None,
    ) -> None:
        if endpoint_key == 'activate_terminal':
            return

        if endpoint_key == 'confirm_terminal':
            if not x_signature_text:
                raise MRAIntegrationError(
                    "MRA activation confirmation requires x-signature text from the TAC.",
                    endpoint_key=endpoint_key,
                )
            if not self._terminal_secret():
                raise MRAIntegrationError(
                    "MRA activation confirmation requires the terminal secret key returned by activation.",
                    endpoint_key=endpoint_key,
                )
            if not self._authorization_header_value(getattr(self.terminal, 'mra_token', '') if self.terminal else ''):
                raise MRAIntegrationError(
                    "MRA activation confirmation requires the terminal authorization token returned by activation.",
                    endpoint_key=endpoint_key,
                )
            return

        token = self._authorization_header_value(getattr(self.terminal, 'mra_token', '') if self.terminal else '')
        if not token:
            raise MRAIntegrationError(
                f"MRA endpoint '{endpoint_key}' requires a terminal Bearer authorization token.",
                endpoint_key=endpoint_key,
            )

        if self._requires_message_hash(endpoint_key) and not self._terminal_secret():
            raise MRAIntegrationError(
                f"MRA endpoint '{endpoint_key}' requires the terminal secret key for x-eis-message-hash.",
                endpoint_key=endpoint_key,
            )

    def _dry_run_result(
        self,
        endpoint_key: str,
        payload: dict[str, Any] | None,
        *,
        reason: str,
    ) -> MRACallResult:
        endpoint = self._resolve_endpoint(endpoint_key)
        return MRACallResult(
            ok=True,
            dry_run=True,
            status_code=202,
            endpoint=endpoint,
            data={
                'status': 'prepared',
                'reason': reason,
                'endpoint_key': endpoint_key,
                'payload': payload or {},
                'prepared_at': timezone.now().isoformat(),
            },
        )

    def _record_connectivity(self, is_online: bool) -> None:
        if not self.terminal or not getattr(self.terminal, 'pk', None):
            return
        try:
            update_fields = ['updated_at']
            if self.terminal.is_online != is_online:
                self.terminal.is_online = is_online
                update_fields.append('is_online')
            if is_online:
                self.terminal.last_sync_at = timezone.now()
                update_fields.append('last_sync_at')
            self.terminal.save(update_fields=update_fields)
        except Exception:
            # Connectivity tracking must not mask the actual MRA request result.
            pass

    @staticmethod
    def _normalize_response_data(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if value in (None, ''):
            return {}
        return {'raw': value}

    @staticmethod
    def _safe_error_body_summary(value: Any) -> str:
        raw = str(value or '').strip()
        if not raw:
            return ''
        lower_raw = raw.lower()
        if '<html' in lower_raw or '<!doctype' in lower_raw:
            return 'MRA EIS is temporarily unavailable.'
        return raw[:500]

    def call(
        self,
        endpoint_key: str,
        payload: dict[str, Any] | None = None,
        *,
        method: str = 'POST',
        mutating: bool = True,
        x_signature_text: str | None = None,
        message_hash_text: str | None = None,
        params: dict[str, Any] | None = None,
        send_json: bool = True,
        record_connectivity: bool = True,
    ) -> MRACallResult:
        """
        Execute a request against MRA EIS.

        Mutating calls are guarded by dry-run + live-submission flags.
        """
        if not self.http_enabled:
            return self._dry_run_result(endpoint_key, payload, reason='http_calls_disabled')

        if self.dry_run:
            return self._dry_run_result(endpoint_key, payload, reason='dry_run_enabled')

        if mutating and not self.allow_live_submission:
            return self._dry_run_result(endpoint_key, payload, reason='live_submission_disabled')

        endpoint = self._resolve_endpoint(endpoint_key)
        self._validate_security_requirements(endpoint_key, x_signature_text=x_signature_text)
        method_name = method.upper()
        request_body_text = None
        if method_name not in {'GET', 'HEAD'} and (send_json or payload is not None):
            request_body_text = self._json_request_body(payload or {})
        effective_message_hash_text = message_hash_text
        if effective_message_hash_text is None and request_body_text is None and payload is None:
            effective_message_hash_text = ''
        headers = self._build_headers(
            endpoint_key,
            payload,
            x_signature_text=x_signature_text,
            message_hash_text=effective_message_hash_text,
        )
        hash_evidence = self._build_message_hash_evidence(
            endpoint_key,
            endpoint,
            method_name,
            payload,
            headers,
            message_hash_text=effective_message_hash_text,
            request_body_text=request_body_text,
        )
        request_kwargs: dict[str, Any] = {
            'method': method_name,
            'url': endpoint,
            'headers': headers,
            'timeout': self.timeout,
            'verify': self.verify_ssl,
        }
        if params:
            request_kwargs['params'] = params
        if request_body_text is not None:
            request_kwargs['data'] = request_body_text

        try:
            response = requests.request(**request_kwargs)
            if record_connectivity:
                self._record_connectivity(True)
            response_data: dict[str, Any] = {}
            if response.content:
                try:
                    response_data = self._normalize_response_data(response.json())
                except ValueError:
                    response_data = {'raw': response.text}

            if not response.ok:
                self._record_hash_evidence(hash_evidence, status_code=response.status_code, ok=False)
                failure_reason = response.headers.get('x-failure-reason', '')
                body_summary = response_data.get('raw') or response_data or ''
                body_summary = self._safe_error_body_summary(body_summary)
                details = []
                if failure_reason:
                    details.append(str(failure_reason))
                if body_summary:
                    details.append(str(body_summary)[:1000])
                detail_text = f": {' | '.join(details)}" if details else ''
                raise MRAIntegrationError(
                    f"MRA request failed ({endpoint_key}): "
                    f"{response.status_code} {response.reason} for url: {endpoint}{detail_text}",
                    status_code=response.status_code,
                    endpoint=endpoint,
                    endpoint_key=endpoint_key,
                    response_data=response_data,
                )

            self._record_hash_evidence(hash_evidence, status_code=response.status_code, ok=True)
            return MRACallResult(
                ok=True,
                dry_run=False,
                status_code=response.status_code,
                endpoint=endpoint,
                data=response_data,
                headers={str(key): str(value) for key, value in response.headers.items()},
            )
        except requests.RequestException as exc:
            if record_connectivity:
                self._record_connectivity(False)
            self._record_hash_evidence(hash_evidence, ok=False, error=str(exc))
            raise MRAIntegrationError(f"MRA request failed ({endpoint_key}): {exc}") from exc


__all__ = ['MRAEISClient']
