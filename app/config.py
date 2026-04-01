"""
Single source of truth for runtime settings.

Priority order:
  1. Database (set via Settings UI)
  2. Environment variables (bootstrap / Docker)
  3. Hardcoded defaults

Call get() anywhere — it always reads fresh from the DB so changes
take effect on the next poll without a restart.
"""
import os
import logging
import aiosqlite
from app.crypto import encrypt, decrypt

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/plexgeo.db")

# Settings definitions: key -> (default_value, sensitive, env_var)
# sensitive=True means the value is encrypted at rest
SETTINGS_SCHEMA: dict[str, tuple[str, bool, str | None]] = {
    "plex_url":             ("",      False, "PLEX_URL"),
    "plex_token":           ("",      True,  "PLEX_TOKEN"),
    "plex_verify_ssl":      ("true",  False, "PLEX_VERIFY_SSL"),
    "poll_interval":        ("30",    False, "POLL_INTERVAL"),
    "outlier_threshold":    ("0.10",  False, "OUTLIER_THRESHOLD"),
    "outlier_min_sessions": ("5",     False, "OUTLIER_MIN_SESSIONS"),
    "dashboard_password":   ("",      True,  None),
}


async def init_settings_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL DEFAULT '',
                sensitive  INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

        # Seed any env-var values that aren't yet in the DB
        for key, (default, sensitive, env_var) in SETTINGS_SCHEMA.items():
            async with db.execute("SELECT key FROM settings WHERE key = ?", (key,)) as cur:
                exists = await cur.fetchone()
            if not exists:
                env_val = os.environ.get(env_var, "") if env_var else ""
                raw = env_val or default
                stored = encrypt(raw) if (sensitive and raw) else raw
                await db.execute(
                    "INSERT INTO settings (key, value, sensitive) VALUES (?, ?, ?)",
                    (key, stored, 1 if sensitive else 0),
                )
        await db.commit()


async def get_all() -> dict[str, str]:
    """Return all settings as plaintext dict."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, value, sensitive FROM settings") as cur:
            rows = await cur.fetchall()

    result = {}
    for key, value, sensitive in rows:
        result[key] = decrypt(value) if sensitive else value

    # Fill any missing keys with defaults
    for key, (default, sensitive, env_var) in SETTINGS_SCHEMA.items():
        if key not in result:
            result[key] = os.environ.get(env_var, default) if env_var else default

    return result


async def get(key: str) -> str:
    """Get a single setting as plaintext."""
    schema = SETTINGS_SCHEMA.get(key)
    if not schema:
        return ""
    default, sensitive, env_var = schema

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value, sensitive FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()

    if row:
        value, is_sensitive = row
        return decrypt(value) if is_sensitive else value

    # Fallback to env / default
    return os.environ.get(env_var, default) if env_var else default


async def set_many(updates: dict[str, str]):
    """Save multiple settings. Encrypts sensitive values automatically."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        for key, plaintext in updates.items():
            schema = SETTINGS_SCHEMA.get(key)
            if not schema:
                logger.warning(f"Ignoring unknown setting key: {key!r}")
                continue
            _, sensitive, _ = schema
            stored = encrypt(plaintext) if (sensitive and plaintext) else plaintext
            await db.execute(
                """INSERT INTO settings (key, value, sensitive, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, stored, 1 if sensitive else 0, now),
            )
        await db.commit()


async def get_for_api() -> dict:
    """
    Return settings safe for the API/UI.
    Sensitive fields are returned as masked strings if set, empty string if not.
    """
    all_settings = await get_all()
    result = {}
    for key, value in all_settings.items():
        _, sensitive, _ = SETTINGS_SCHEMA.get(key, ("", False, None))
        if sensitive:
            result[key] = "••••••••" if value else ""
        else:
            result[key] = value
    return result
