"""Database field helpers for sensitive MRA EIS terminal credentials."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


_PREFIX = 'mra-eis:v1:'


def _credential_fernet() -> Fernet:
    configured_key = str(
        getattr(settings, 'MRA_EIS_CREDENTIAL_ENCRYPTION_KEY', '') or ''
    ).strip()
    is_live = bool(getattr(settings, 'MRA_EIS_IS_LIVE', False))
    if not configured_key and is_live:
        raise ImproperlyConfigured(
            'MRA_EIS_CREDENTIAL_ENCRYPTION_KEY is required when MRA_EIS_MODE=LIVE.'
        )

    key_material = configured_key or f'{settings.SECRET_KEY}:mra-eis-credentials'
    derived_key = base64.urlsafe_b64encode(
        hashlib.sha256(key_material.encode('utf-8')).digest()
    )
    return Fernet(derived_key)


def encrypt_mra_credential(value: str | None) -> str | None:
    if value in (None, ''):
        return value
    text = str(value)
    if text.startswith(_PREFIX):
        return text
    return _PREFIX + _credential_fernet().encrypt(text.encode('utf-8')).decode('utf-8')


def decrypt_mra_credential(value: str | None) -> str | None:
    if value in (None, ''):
        return value
    text = str(value)
    if not text.startswith(_PREFIX):
        # Supports a safe rollout: the accompanying migration converts these
        # records, but a terminal is never silently locked out if a migration
        # was interrupted before its data step completed.
        return text
    try:
        return _credential_fernet().decrypt(text[len(_PREFIX):].encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            'Unable to decrypt an MRA EIS terminal credential. Check '
            'MRA_EIS_CREDENTIAL_ENCRYPTION_KEY has not changed.'
        ) from exc


class EncryptedTextField(models.TextField):
    """A TextField encrypted at rest while remaining transparent to callers."""

    description = 'Encrypted text'

    def from_db_value(self, value, expression, connection):
        return decrypt_mra_credential(value)

    def to_python(self, value):
        if value is None:
            return value
        return str(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt_mra_credential(value)
