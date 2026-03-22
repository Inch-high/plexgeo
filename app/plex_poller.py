import logging
import urllib3
import requests
from plexapi.server import PlexServer
from plexapi.exceptions import Unauthorized
from app import database
from app import config as cfg
from app.geo import resolve_ip
from app.alerts import check_for_outlier

logger = logging.getLogger(__name__)


async def poll_sessions():
    settings = await cfg.get_all()

    plex_url   = settings.get("plex_url", "")
    plex_token = settings.get("plex_token", "")
    verify_ssl = settings.get("plex_verify_ssl", "true").lower() not in ("false", "0", "no")

    if not plex_token:
        logger.warning("plex_token not configured — skipping poll. Set it in Settings.")
        return

    if not plex_url:
        logger.warning("plex_url not configured — skipping poll. Set it in Settings.")
        return

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        session = requests.Session()
        session.verify = verify_ssl
        plex = PlexServer(plex_url, plex_token, session=session, timeout=10)
    except Unauthorized:
        logger.error("Plex auth failed — check your Plex Token in Settings")
        return
    except Exception as e:
        logger.error(f"Could not connect to Plex at {plex_url}: {e}")
        return

    try:
        sessions = plex.sessions()
    except Exception as e:
        logger.error(f"Failed to fetch Plex sessions: {e}")
        return

    active_keys = []

    for session in sessions:
        try:
            username = session.usernames[0] if session.usernames else "Unknown"
            players = session.players
            if not players:
                continue

            player = players[0]
            ip = player.address
            session_key = str(session.sessionKey)
            media_title = getattr(session, "title", "Unknown")
            media_type  = getattr(session, "type", "unknown")
            platform    = getattr(player, "platform", "Unknown")

            active_keys.append(session_key)

            geo = await resolve_ip(ip)

            session_id = await database.upsert_session(
                session_key=session_key,
                username=username,
                ip_address=ip,
                country_code=geo.get("countryCode", "XX"),
                country_name=geo.get("country", "Unknown"),
                city=geo.get("city", ""),
                latitude=geo.get("lat", 0.0),
                longitude=geo.get("lon", 0.0),
                media_title=media_title,
                media_type=media_type,
                player_platform=platform,
            )

            await check_for_outlier(username, ip, geo, session_id)

        except Exception as e:
            logger.exception(f"Error processing session: {e}")

    await database.mark_sessions_inactive(active_keys)
    logger.debug(f"Polled Plex: {len(sessions)} active session(s)")
