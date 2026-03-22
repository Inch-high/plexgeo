import ipaddress
import logging
import httpx
from app import database

logger = logging.getLogger(__name__)

# ipapi.co — free tier, HTTPS, 1000 req/day, no key needed
IPAPI_URL = "https://ipapi.co/{ip}/json/"

# Internal geo stubs — never sent to external API
_GEO_LOOPBACK = {"countryCode": "LO", "country": "Loopback",     "city": "localhost",   "lat": 0.0, "lon": 0.0}
_GEO_LAN      = {"countryCode": "LO", "country": "Local Network", "city": "LAN",         "lat": 0.0, "lon": 0.0}
_GEO_LINK     = {"countryCode": "LO", "country": "Link-Local",    "city": "169.254.x.x", "lat": 0.0, "lon": 0.0}
_GEO_UNKNOWN  = {"countryCode": "XX", "country": "Unknown",       "city": "",            "lat": 0.0, "lon": 0.0}


def classify_ip(ip: str) -> tuple[bool, dict | None]:
    """
    Returns (is_internal, geo_stub).
    If is_internal is True, geo_stub is ready to use — do NOT query external API.
    If is_internal is False, geo_stub is None and the IP should be resolved externally.

    Covers all internal ranges without any external calls:
      - 127.x.x.x          loopback
      - 10.x.x.x           RFC 1918 private
      - 172.16-31.x.x      RFC 1918 private
      - 192.168.x.x        RFC 1918 private
      - 169.254.x.x        link-local (APIPA)
      - ::1                IPv6 loopback
      - fc00::/7           IPv6 unique local
      - fe80::/10          IPv6 link-local
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        logger.warning(f"Could not parse IP address: {ip!r} — treating as internal")
        return True, _GEO_UNKNOWN

    if addr.is_loopback:
        logger.debug(f"IP {ip} is loopback — skipping geo lookup")
        return True, _GEO_LOOPBACK

    if addr.is_link_local:
        logger.debug(f"IP {ip} is link-local — skipping geo lookup")
        return True, _GEO_LINK

    if addr.is_private:
        logger.debug(f"IP {ip} is private/LAN — skipping geo lookup")
        return True, _GEO_LAN

    if addr.is_unspecified or addr.is_reserved or addr.is_multicast:
        logger.debug(f"IP {ip} is reserved/multicast — skipping geo lookup")
        return True, _GEO_UNKNOWN

    return False, None


def _parse_ipapi_co(data: dict) -> dict:
    return {
        "countryCode": data.get("country_code", "XX"),
        "country":     data.get("country_name", "Unknown"),
        "city":        data.get("city", ""),
        "lat":         data.get("latitude", 0.0),
        "lon":         data.get("longitude", 0.0),
    }


async def resolve_ip(ip: str) -> dict:
    # Always classify first — internal IPs never leave the machine
    is_internal, stub = classify_ip(ip)
    if is_internal:
        return stub

    # Check cache before hitting external API
    cached = await database.get_cached_ip(ip)
    if cached:
        return {
            "countryCode": cached["country_code"],
            "country":     cached["country_name"],
            "city":        cached["city"],
            "lat":         cached["latitude"],
            "lon":         cached["longitude"],
        }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(IPAPI_URL.format(ip=ip))
            resp.raise_for_status()
            data = resp.json()

        if "country_code" in data:
            geo = _parse_ipapi_co(data)
            await database.cache_ip(ip, geo)
            return geo
    except Exception as e:
        logger.warning(f"GeoIP lookup failed for {ip}: {e}")

    return _GEO_UNKNOWN


async def resolve_batch(ips: list[str]) -> dict[str, dict]:
    """Resolve a list of IPs, using internal classification and cache first.
    ipapi.co has no batch endpoint on the free tier so we resolve sequentially."""
    result = {}
    for ip in ips:
        result[ip] = await resolve_ip(ip)
    return result
