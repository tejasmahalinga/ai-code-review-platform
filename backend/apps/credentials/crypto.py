"""Symmetric encryption for secrets stored in the database.

Uses ``MultiFernet`` (AES-128-CBC + HMAC-SHA256) with keys from ``REVIEWBOT_ENCRYPTION_KEYS``.
The first key encrypts; every key can decrypt, which allows master-key rotation:
prepend a new key, run ``manage.py rotate_encryption_key``, then drop the old key.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class DecryptionError(Exception):
    pass


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    keys = settings.REVIEWBOT_ENCRYPTION_KEYS
    if not keys:
        raise ImproperlyConfigured(
            "REVIEWBOT_ENCRYPTION_KEYS is not set. Generate one with: "
            'python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
        )
    try:
        return MultiFernet([Fernet(key.encode()) for key in keys])
    except ValueError as exc:
        raise ImproperlyConfigured(
            "REVIEWBOT_ENCRYPTION_KEYS contains an invalid key (expected 32 url-safe base64 bytes)."
        ) from exc


def reset_cache() -> None:
    _fernet.cache_clear()


def check_configuration() -> None:
    """Raises ImproperlyConfigured if encryption keys are missing or malformed."""
    _fernet()


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "Could not decrypt a stored secret. Was REVIEWBOT_ENCRYPTION_KEYS changed without rotation?"
        ) from exc


def rotate(token: str) -> str:
    """Re-encrypts a token with the current primary key."""
    try:
        return _fernet().rotate(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError("Could not decrypt a stored secret during rotation.") from exc
