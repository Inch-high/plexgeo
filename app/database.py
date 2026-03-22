import os
import aiosqlite
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "/data/plexgeo.db")


async def get_db():
    return await aiosqlite.connect(DB_PATH)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key     TEXT NOT NULL,
                username        TEXT NOT NULL,
                ip_address      TEXT NOT NULL,
                country_code    TEXT NOT NULL DEFAULT 'XX',
                country_name    TEXT NOT NULL DEFAULT 'Unknown',
                city            TEXT,
                latitude        REAL,
                longitude       REAL,
                media_title     TEXT,
                media_type      TEXT,
                player_platform TEXT,
                started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_active       INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions(username);
            CREATE INDEX IF NOT EXISTS idx_sessions_session_key ON sessions(session_key);
            CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_country_code ON sessions(country_code);

            CREATE TABLE IF NOT EXISTS ip_cache (
                ip_address   TEXT PRIMARY KEY,
                country_code TEXT NOT NULL,
                country_name TEXT NOT NULL,
                city         TEXT,
                latitude     REAL,
                longitude    REAL,
                resolved_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL,
                ip_address    TEXT NOT NULL,
                country_code  TEXT NOT NULL,
                country_name  TEXT NOT NULL,
                usual_country TEXT NOT NULL,
                session_id    INTEGER REFERENCES sessions(id),
                created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                acknowledged  INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_username ON alerts(username);
            CREATE INDEX IF NOT EXISTS idx_alerts_acknowledged ON alerts(acknowledged);
        """)
        await db.commit()


async def get_cached_ip(ip: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM ip_cache WHERE ip_address = ?", (ip,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def cache_ip(ip: str, geo: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO ip_cache
               (ip_address, country_code, country_name, city, latitude, longitude)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                ip,
                geo.get("countryCode", "XX"),
                geo.get("country", "Unknown"),
                geo.get("city"),
                geo.get("lat"),
                geo.get("lon"),
            ),
        )
        await db.commit()


async def upsert_session(
    session_key: str,
    username: str,
    ip_address: str,
    country_code: str,
    country_name: str,
    city: str,
    latitude: float,
    longitude: float,
    media_title: str,
    media_type: str,
    player_platform: str,
) -> int:
    """Insert or update a session, returns the session id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if active session with this key exists
        async with db.execute(
            "SELECT id FROM sessions WHERE session_key = ? AND is_active = 1",
            (session_key,),
        ) as cur:
            row = await cur.fetchone()

        if row:
            await db.execute(
                "UPDATE sessions SET last_seen = ?, is_active = 1 WHERE id = ?",
                (now, row[0]),
            )
            await db.commit()
            return row[0]
        else:
            cur = await db.execute(
                """INSERT INTO sessions
                   (session_key, username, ip_address, country_code, country_name,
                    city, latitude, longitude, media_title, media_type, player_platform,
                    started_at, last_seen, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    session_key, username, ip_address, country_code, country_name,
                    city, latitude, longitude, media_title, media_type, player_platform,
                    now, now,
                ),
            )
            await db.commit()
            return cur.lastrowid


async def mark_sessions_inactive(active_keys: list[str]):
    """Mark any sessions not in active_keys as inactive."""
    async with aiosqlite.connect(DB_PATH) as db:
        if active_keys:
            placeholders = ",".join("?" * len(active_keys))
            await db.execute(
                f"UPDATE sessions SET is_active = 0 WHERE is_active = 1 AND session_key NOT IN ({placeholders})",
                active_keys,
            )
        else:
            await db.execute("UPDATE sessions SET is_active = 0 WHERE is_active = 1")
        await db.commit()


async def get_user_country_distribution(username: str) -> dict:
    """Returns {country_code: count} for a user's recent history (last 90 days)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT country_code, COUNT(*) as cnt FROM sessions
               WHERE username = ?
               AND started_at >= datetime('now', '-90 days')
               GROUP BY country_code""",
            (username,),
        ) as cur:
            rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


async def has_alert_today(username: str, country_code: str) -> bool:
    """Prevent duplicate alerts for same user+country on same day."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id FROM alerts
               WHERE username = ? AND country_code = ?
               AND created_at >= datetime('now', '-1 day')""",
            (username, country_code),
        ) as cur:
            return await cur.fetchone() is not None


async def create_alert(
    username: str,
    ip_address: str,
    country_code: str,
    country_name: str,
    usual_country: str,
    session_id: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO alerts
               (username, ip_address, country_code, country_name, usual_country, session_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (username, ip_address, country_code, country_name, usual_country, session_id),
        )
        await db.commit()


# ── API query helpers ────────────────────────────────────────────────────────

def _row_to_dict(row, cursor):
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


async def api_get_sessions(limit: int = 100, offset: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT id, session_key, username, ip_address, country_code, country_name,
                      city, latitude, longitude, media_title, media_type,
                      player_platform, started_at, last_seen, is_active
               FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ) as cur:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]


async def api_get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM sessions WHERE is_active = 1"
        ) as cur:
            active_streams = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(DISTINCT username) FROM sessions"
        ) as cur:
            total_users = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(DISTINCT country_code) FROM sessions WHERE country_code != 'XX'"
        ) as cur:
            total_countries = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM alerts WHERE acknowledged = 0"
        ) as cur:
            unacked_alerts = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at >= datetime('now', '-24 hours')"
        ) as cur:
            sessions_24h = (await cur.fetchone())[0]

    return {
        "active_streams": active_streams,
        "total_users": total_users,
        "total_countries": total_countries,
        "unacked_alerts": unacked_alerts,
        "sessions_24h": sessions_24h,
    }


async def api_get_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT
                 username,
                 COUNT(*) as total_sessions,
                 COUNT(DISTINCT country_code) as country_count,
                 MAX(started_at) as last_seen,
                 SUM(is_active) as currently_active
               FROM sessions
               GROUP BY username
               ORDER BY last_seen DESC"""
        ) as cur:
            cols = [d[0] for d in cur.description]
            users = [dict(zip(cols, r)) for r in await cur.fetchall()]

        # For each user, get their top country + country timeline
        for user in users:
            async with db.execute(
                """SELECT country_code, country_name, COUNT(*) as cnt
                   FROM sessions WHERE username = ?
                   GROUP BY country_code ORDER BY cnt DESC""",
                (user["username"],),
            ) as cur:
                countries = [{"code": r[0], "name": r[1], "count": r[2]} for r in await cur.fetchall()]
            user["countries"] = countries
            user["primary_country"] = countries[0] if countries else None

    return users


async def api_get_user_history(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # Sessions over time grouped by day + country
        async with db.execute(
            """SELECT date(started_at) as day, country_code, country_name, COUNT(*) as cnt
               FROM sessions WHERE username = ?
               GROUP BY day, country_code ORDER BY day""",
            (username,),
        ) as cur:
            timeline = [
                {"day": r[0], "country_code": r[1], "country_name": r[2], "count": r[3]}
                for r in await cur.fetchall()
            ]

        # Recent sessions
        async with db.execute(
            """SELECT ip_address, country_code, country_name, city,
                      media_title, media_type, player_platform,
                      started_at, last_seen, is_active
               FROM sessions WHERE username = ?
               ORDER BY started_at DESC LIMIT 50""",
            (username,),
        ) as cur:
            cols = [d[0] for d in cur.description]
            sessions = [dict(zip(cols, r)) for r in await cur.fetchall()]

    return {"timeline": timeline, "sessions": sessions}


async def api_get_alerts(include_acked: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        where = "" if include_acked else "WHERE acknowledged = 0"
        async with db.execute(
            f"""SELECT id, username, ip_address, country_code, country_name,
                       usual_country, created_at, acknowledged
                FROM alerts {where} ORDER BY created_at DESC LIMIT 200"""
        ) as cur:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]


async def api_acknowledge_alert(alert_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,)
        )
        await db.commit()


async def api_get_country_counts():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT country_code, country_name, COUNT(*) as cnt
               FROM sessions WHERE country_code != 'XX'
               GROUP BY country_code ORDER BY cnt DESC"""
        ) as cur:
            return [{"code": r[0], "name": r[1], "count": r[2]} for r in await cur.fetchall()]
