import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import database
from app import config as cfg
from app.plex_poller import poll_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def _poll_interval() -> int:
    import asyncio
    # Sync wrapper — APScheduler calls this synchronously so we read env as fallback
    return int(os.environ.get("POLL_INTERVAL", "30"))


async def _reschedule(interval: int):
    """Update the poller interval without restarting the app."""
    scheduler.reschedule_job("plex_poll", trigger="interval", seconds=interval)
    logger.info(f"Poller rescheduled to every {interval}s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    await cfg.init_settings_table()
    logger.info("Database initialised")

    interval = int((await cfg.get("poll_interval")) or "30")
    scheduler.add_job(poll_sessions, "interval", seconds=interval, id="plex_poll")
    scheduler.start()
    logger.info(f"Plex poller started (every {interval}s)")

    await poll_sessions()

    yield

    scheduler.shutdown()


app = FastAPI(title="PlexGeo", lifespan=lifespan)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(static_dir, "index.html"))


# ── Data API ─────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    return await database.api_get_stats()


@app.get("/api/sessions")
async def get_sessions(limit: int = 100, offset: int = 0):
    return await database.api_get_sessions(limit, offset)


@app.get("/api/users")
async def get_users():
    return await database.api_get_users()


@app.get("/api/user/{username}/history")
async def get_user_history(username: str):
    return await database.api_get_user_history(username)


@app.get("/api/alerts")
async def get_alerts(include_acked: bool = False):
    return await database.api_get_alerts(include_acked)


@app.post("/api/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int):
    await database.api_acknowledge_alert(alert_id)
    return {"ok": True}


@app.get("/api/map")
async def get_map_data():
    return await database.api_get_country_counts()


# ── Settings API ──────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    """Return settings with sensitive values masked."""
    return await cfg.get_for_api()


class SettingsUpdate(BaseModel):
    plex_url:             str | None = None
    plex_token:           str | None = None
    plex_verify_ssl:      str | None = None
    poll_interval:        str | None = None
    outlier_threshold:    str | None = None
    outlier_min_sessions: str | None = None


@app.post("/api/settings")
async def save_settings(body: SettingsUpdate):
    updates: dict[str, str] = {}

    for key, value in body.model_dump().items():
        if value is None:
            continue
        # Don't overwrite a real token with the masked placeholder
        if value == "••••••••":
            continue
        updates[key] = value

    if not updates:
        return {"ok": True, "message": "No changes"}

    await cfg.set_many(updates)

    # If poll_interval changed, reschedule the poller immediately
    if "poll_interval" in updates:
        try:
            await _reschedule(int(updates["poll_interval"]))
        except Exception as e:
            logger.warning(f"Could not reschedule poller: {e}")

    # Trigger an immediate poll to validate new Plex credentials
    if "plex_url" in updates or "plex_token" in updates or "plex_verify_ssl" in updates:
        await poll_sessions()

    return {"ok": True, "message": f"Saved {len(updates)} setting(s)"}


@app.post("/api/settings/test")
async def test_connection():
    """Attempt a Plex connection with current settings and report the result."""
    import requests as req_lib
    import urllib3
    from plexapi.server import PlexServer
    from plexapi.exceptions import Unauthorized

    settings   = await cfg.get_all()
    plex_url   = settings.get("plex_url", "")
    plex_token = settings.get("plex_token", "")
    verify_ssl = settings.get("plex_verify_ssl", "true").lower() not in ("false", "0", "no")

    if not plex_url or not plex_token:
        return {"ok": False, "message": "Plex URL and Token must be configured first"}

    try:
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session = req_lib.Session()
        session.verify = verify_ssl
        plex = PlexServer(plex_url, plex_token, session=session, timeout=8)
        friendly = plex.friendlyName
        version  = plex.version
        return {"ok": True, "message": f"Connected to \"{friendly}\" (Plex Media Server {version})"}
    except Unauthorized:
        return {"ok": False, "message": "Authentication failed — check your Plex Token"}
    except Exception as e:
        return {"ok": False, "message": f"Connection failed: {e}"}


@app.get("/health")
async def health():
    return {"status": "ok"}
