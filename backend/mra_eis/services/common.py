from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class MRAIntegrationError(Exception):
    """Raised when MRA integration operations fail."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        endpoint: str | None = None,
        endpoint_key: str | None = None,
        response_data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint
        self.endpoint_key = endpoint_key
        self.response_data = response_data


@dataclass
class MRACallResult:
    ok: bool
    dry_run: bool
    status_code: int
    endpoint: str
    data: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class OfflineLimitPolicy:
    max_transaction_age_hours: int | None = None
    max_cumulative_amount: Decimal | None = None
    source: str | None = None


def extract_mra_response_errors(response_data: Any) -> list[str]:
    """Return MRA business/validation errors from an otherwise successful HTTP response."""
    if not isinstance(response_data, dict):
        return []

    extracted: list[str] = []
    raw_status_code = response_data.get('statusCode', response_data.get('status_code'))
    raw_http_status_code = response_data.get('httpStatusCode', response_data.get('http_status_code'))
    status_indicates_failure = False
    try:
        status_indicates_failure = int(raw_status_code) < 0
    except (TypeError, ValueError):
        normalized_status = str(raw_status_code or response_data.get('status') or '').strip().lower()
        status_indicates_failure = normalized_status in {'error', 'failed', 'failure', 'rejected'}
    if not status_indicates_failure:
        try:
            status_indicates_failure = int(raw_http_status_code) >= 400
        except (TypeError, ValueError):
            pass

    def add_error(raw_error: Any) -> None:
        if raw_error in (None, ''):
            return
        if isinstance(raw_error, dict):
            message = (
                raw_error.get('errorMessage')
                or raw_error.get('message')
                or raw_error.get('remark')
                or raw_error.get('fieldName')
                or raw_error
            )
            extracted.append(str(message))
            return
        extracted.append(str(raw_error))

    raw_errors = response_data.get('errors')
    if isinstance(raw_errors, list):
        for raw_error in raw_errors:
            add_error(raw_error)
    else:
        add_error(raw_errors)

    add_error(response_data.get('error'))
    if status_indicates_failure:
        for key in ('remark', 'message', 'statusDescription', 'status_description', 'raw'):
            add_error(response_data.get(key))

    for key in ('errorMessage', 'error_message'):
        add_error(response_data.get(key))

    inner_data = response_data.get('data') if isinstance(response_data.get('data'), dict) else {}
    validation_errors = (
        inner_data.get('validationErrors')
        or inner_data.get('validation_errors')
        or response_data.get('validationErrors')
        or response_data.get('validation_errors')
    )
    if isinstance(validation_errors, list):
        for validation_error in validation_errors:
            add_error(validation_error)
    else:
        add_error(validation_errors)

    return extracted


_extract_mra_response_errors = extract_mra_response_errors

__all__ = [
    'MRAIntegrationError',
    'MRACallResult',
    'OfflineLimitPolicy',
    'extract_mra_response_errors',
    '_extract_mra_response_errors',
]
