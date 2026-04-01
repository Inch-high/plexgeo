import logging
import os
import secrets
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from app import database
from app import config as cfg
from app.plex_poller import poll_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# Paths that never require auth
AUTH_EXEMPT = {"/health", "/api/auth/check", "/api/auth/login"}


# ── Auth middleware ───────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    """Require password on /api/* routes when dashboard_password is set."""
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path not in AUTH_EXEMPT:
            password = await cfg.get("dashboard_password")
            if password:
                auth = request.headers.get("authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                if not token or not secrets.compare_digest(token, password):
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ── Security headers middleware ──────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response


def _poll_interval() -> int:
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

    password = await cfg.get("dashboard_password")
    if password:
        logger.info("Dashboard password is set — authentication enabled")
    else:
        logger.warning("No dashboard password set — dashboard is open to anyone on the network")

    await poll_sessions()

    yield

    scheduler.shutdown()


app = FastAPI(title="PlexGeo", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(static_dir, "index.html"))


# ── Auth API ─────────────────────────────────────────────────────────────────

@app.get("/api/auth/check")
async def auth_check():
    """Check whether auth is required (no auth needed to call this)."""
    password = await cfg.get("dashboard_password")
    return {"auth_required": bool(password)}


class LoginBody(BaseModel):
    password: str


@app.post("/api/auth/login")
async def auth_login(body: LoginBody):
    """Verify password (no auth needed to call this)."""
    stored = await cfg.get("dashboard_password")
    if not stored:
        return {"ok": True}
    if secrets.compare_digest(body.password, stored):
        return {"ok": True}
    return JSONResponse({"ok": False, "message": "Incorrect password"}, status_code=401)


# ── Data API ─────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    return await database.api_get_stats()


@app.get("/api/sessions")
async def get_sessions(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
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
    dashboard_password:   str | None = None

    @field_validator("plex_url")
    @classmethod
    def validate_plex_url(cls, v):
        if v is not None and v != "" and not v.startswith(("http://", "https://")):
            raise ValueError("Must start with http:// or https://")
        return v

    @field_validator("poll_interval")
    @classmethod
    def validate_poll_interval(cls, v):
        if v is not None:
            try:
                n = int(v)
            except ValueError:
                raise ValueError("Must be a number")
            if n < 5 or n > 3600:
                raise ValueError("Must be between 5 and 3600")
        return v

    @field_validator("outlier_threshold")
    @classmethod
    def validate_outlier_threshold(cls, v):
        if v is not None:
            try:
                n = float(v)
            except ValueError:
                raise ValueError("Must be a number")
            if n < 0.01 or n > 1.0:
                raise ValueError("Must be between 0.01 and 1.0")
        return v

    @field_validator("outlier_min_sessions")
    @classmethod
    def validate_outlier_min_sessions(cls, v):
        if v is not None:
            try:
                n = int(v)
            except ValueError:
                raise ValueError("Must be a number")
            if n < 1 or n > 1000:
                raise ValueError("Must be between 1 and 1000")
        return v


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
        logger.error(f"Plex connection test failed: {e}")
        return {"ok": False, "message": "Connection failed — check your Plex URL and network"}


@app.get("/health")
async def health():
    return {"status": "ok"}
