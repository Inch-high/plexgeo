import logging
from app import database
from app import config as cfg

logger = logging.getLogger(__name__)


async def check_for_outlier(
    username: str,
    ip: str,
    geo: dict,
    session_id: int,
):
    country_code = geo.get("countryCode", "XX")
    country_name = geo.get("country", "Unknown")

    if country_code in ("XX", "LO"):
        return

    settings = await cfg.get_all()
    threshold    = float(settings.get("outlier_threshold", "0.10"))
    min_sessions = int(settings.get("outlier_min_sessions", "5"))

    distribution = await database.get_user_country_distribution(username)
    total = sum(distribution.values())

    if total < min_sessions:
        return

    country_count = distribution.get(country_code, 0)
    fraction = country_count / total

    if fraction < threshold:
        usual_country = max(distribution, key=distribution.get)

        if await database.has_alert_today(username, country_code):
            return

        logger.warning(
            f"OUTLIER: {username} streaming from {country_name} ({country_code}) "
            f"— usual country: {usual_country} "
            f"(fraction: {fraction:.1%}, threshold: {threshold:.0%})"
        )

        await database.create_alert(
            username=username,
            ip_address=ip,
            country_code=country_code,
            country_name=country_name,
            usual_country=usual_country,
            session_id=session_id,
        )
