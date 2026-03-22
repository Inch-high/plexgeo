"""
Fernet symmetric encryption for sensitive settings.

The encryption key is generated once and stored at KEY_PATH (default /data/secret.key).
In dev, KEY_PATH follows DB_PATH so the key lives alongside dev.db.
"""
import os
import stat
import logging
from pathlib import Path
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# Derive key path from DB_PATH so they always live together
_db_path = os.environ.get("DB_PATH", "/data/plexgeo.db")
KEY_PATH = os.environ.get("KEY_PATH", str(Path(_db_path).parent / "secret.key"))


def _load_or_create_key() -> bytes:
    path = Path(KEY_PATH)
    if path.exists():
        return path.read_bytes().strip()

    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    # Restrict to owner read/write only
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    logger.info(f"Generated new encryption key at {KEY_PATH}")
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


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
