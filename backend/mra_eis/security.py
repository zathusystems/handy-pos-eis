"""Small, shared safeguards for data retained by the MRA EIS integration."""

from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEYS = {
    'accesskey',
    'accesstoken',
    'apikey',
    'authorization',
    'credential',
    'credentials',
    'jwt',
    'jwttoken',
    'mraapikey',
    'mratoken',
    'password',
    'secret',
    'secretkey',
    'tac',
    'taccode',
    'token',
}
_SENSITIVE_TEXT = re.compile(
    r'(?i)\b(authorization|access[ _-]?key|api[ _-]?key|jwt|token|secret(?:[ _-]?key)?|password|tac(?:[ _-]?code)?)\b\s*[:=]\s*(?:bearer\s+)?([^\s,;]+)'
)


def _normalise_key(key: Any) -> str:
    return re.sub(r'[^a-z0-9]', '', str(key).lower())


def redact_sensitive_data(value: Any) -> Any:
    """Remove credentials from structured audit data before it is persisted."""
    if isinstance(value, dict):
        return {
            key: '[redacted]' if _normalise_key(key) in _SENSITIVE_KEYS
            else redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    return value


def redact_sensitive_text(value: Any) -> str:
    """Keep API errors useful without retaining credential values."""
    return _SENSITIVE_TEXT.sub(lambda match: f'{match.group(1)}=[redacted]', str(value or ''))
