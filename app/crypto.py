"""
Fernet symmetric encryption for sensitive settings.

The encryption key is read from the ENCRYPTION_KEY environment variable.
If not set, a transient key is generated in memory (will not survive restarts,
meaning encrypted settings will become unreadable after a container restart).
"""
import os
import base64
import hashlib
import logging
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_key: bytes | None = None


def _get_key() -> bytes:
    global _key
    if _key is not None:
        return _key

    env_key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if env_key:
        # Derive a valid 32-byte Fernet key from any input string (e.g. openssl rand -base64 32)
        raw = hashlib.sha256(env_key.encode()).digest()
        _key = base64.urlsafe_b64encode(raw)
    else:
        _key = Fernet.generate_key()
        logger.warning(
            "ENCRYPTION_KEY not set — using an ephemeral key. "
            "Encrypted settings will be LOST on restart. "
            "Generate a permanent key with: openssl rand -base64 32"
        )
    return _key


def _fernet() -> Fernet:
    return Fernet(_get_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a string and return a base64 token string."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token string back to plaintext. Returns '' on failure."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:
        logger.warning("Failed to decrypt a settings value — key may have changed")
        return ""
