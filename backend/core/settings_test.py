"""
Test settings for local MRA EIS dry-run verification.

This profile intentionally forces:
- SQLite database (no PostgreSQL dependency)
- TEST mode + dry-run safety
"""

from __future__ import annotations

import os

os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('ENVIRONMENT', 'development')
os.environ.setdefault('MRA_EIS_MODE', 'TEST')
os.environ.setdefault('MRA_EIS_DRY_RUN', 'True')
os.environ.setdefault('MRA_EIS_ALLOW_LIVE_SUBMISSION', 'False')
os.environ.setdefault('MRA_EIS_ENABLE_HTTP_CALLS', 'True')
os.environ.setdefault('MRA_EIS_BASE_URL', 'https://dev-eis-api.mra.mw')
os.environ.setdefault('MRA_EIS_VERIFY_SSL', 'True')
os.environ.setdefault('MRA_EIS_STRICT_PRODUCT_CODES', 'True')
os.environ.setdefault('MRA_EIS_REQUIRE_REMOTE_SEQUENCE_RECOVERY_FOR_SALES', 'False')
os.environ.setdefault('MRA_EIS_ENFORCE_TERMINAL_DEVICE_BINDING', 'False')
os.environ.setdefault('MRA_EIS_CREDENTIAL_ENCRYPTION_KEY', 'handy-pos-test-eis-credential-key')

from .settings import *  # noqa: F401,F403

DEBUG = True
IS_PRODUCTION = False
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.test.sqlite3',
    }
}

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
