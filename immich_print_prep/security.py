"""Password hashing, session tokens and encryption of stored Immich API keys."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# scrypt parameters. n=2**15 with r=8 costs ~32 MB and a few tens of ms, which
# is a sensible ceiling for a container that also decodes JPEGs.
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32

MIN_PASSWORD_LENGTH = 8


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    """scrypt via `cryptography`.

    `hashlib.scrypt` is missing on interpreters linked against LibreSSL (the
    stock macOS python, for one), and this has to behave the same everywhere.
    """
    return Scrypt(salt=salt, length=length, n=n, r=r, p=p).derive(password.encode("utf-8"))


class PasswordError(ValueError):
    """Raised when a proposed password is not acceptable."""


def hash_password(password: str) -> str:
    """Return a self-describing `scrypt$n$r$p$salt$hash` string."""
    validate_password(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _KEY_BYTES)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: Optional[str]) -> bool:
    """Constant-time check of a password against a stored hash."""
    if not encoded or not password:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = _scrypt(password, salt, int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            "password must be at least %d characters" % MIN_PASSWORD_LENGTH
        )
    if len(password) > 1024:
        raise PasswordError("password is too long")


def new_session_token() -> Tuple[str, str]:
    """Return (token for the cookie, hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_session_token(token)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SecretBox:
    """Symmetric encryption for the Immich API keys held in the database.

    The key is derived from the server secret, so a copy of the database on its
    own does not hand over everyone's Immich credentials.
    """

    def __init__(self, secret_key: str):
        material = _scrypt(secret_key, b"immich-print-prep/apikey", 1 << 14, 8, 1, 32)
        self._fernet = Fernet(base64.urlsafe_b64encode(material))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            # Wrong or rotated secret key: treat the stored key as absent so the
            # user is asked for it again rather than hitting a 500.
            return None
