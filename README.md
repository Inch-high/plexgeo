# PlexGeo — Stream Intelligence Dashboard

Self-hosted Docker app that monitors your Plex server, logs every user stream by IP and country, graphs trends over time, and raises **anomaly alerts** when a user streams from an unusual location (e.g. normally UK, now AUS).

## Features

- **Live world map** — choropleth + glowing dots for active streams
- **Stream log** — full history table with IP, country, media, platform
- **Per-user drill-down** — country distribution doughnut, timeline chart, session history
- **Anomaly alerts** — sidebar panel flags location outliers with one-click acknowledge
- **Auto-refresh** — polls Plex every 30 s; dashboard updates every 30 s
- **GeoIP** — uses [ip-api.com](https://ip-api.com) (free, no key needed, results cached in SQLite)
- **Persistent SQLite** database mounted as a Docker volume

---

## Quick Start

### 1. Get your Plex token

Open Plex Web → any media item → `···` → Get Info → View XML.
The URL will contain `X-Plex-Token=XXXX` — copy that value.

Or follow: https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

### 2. Generate an encryption key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save the output — you'll need it for the `ENCRYPTION_KEY` variable.

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your PLEX_URL, PLEX_TOKEN, and ENCRYPTION_KEY
```

### 4. Run

```bash
docker compose up -d
```

Dashboard will be at **http://localhost:7842**

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PLEX_URL` | — | URL of your Plex server, e.g. `http://192.168.1.50:32400` |
| `PLEX_TOKEN` | — | Your Plex authentication token |
| `ENCRYPTION_KEY` | — | Fernet key for encrypting sensitive settings at rest (see above) |
| `PORT` | `7842` | Host port for the dashboard |
| `POLL_INTERVAL` | `30` | Seconds between Plex polls |
| `OUTLIER_THRESHOLD` | `0.10` | A country must account for <10% of sessions to be flagged |
| `OUTLIER_MIN_SESSIONS` | `5` | Minimum sessions before outlier detection kicks in |

> If `ENCRYPTION_KEY` is not set, a temporary key is generated in memory. Encrypted settings (e.g. your Plex token stored via the UI) will be lost on container restart.

---

## Docker Image

Pre-built images are published to GHCR on every push to `main`:

```bash
docker pull ghcr.io/inch-high/plexgeo:latest
```

Available for `linux/amd64` and `linux/arm64`.

---

## Unraid

1. In the Unraid Docker tab, click **Add Container**
2. Set **Repository** to `ghcr.io/inch-high/plexgeo:latest`
3. Add a **Port** mapping: host `7842` → container `7842`
4. Add a **Path** mapping: host `/mnt/user/appdata/plexgeo` → container `/data`
5. Add **Variables**:
   - `PLEX_URL` = your Plex server URL
   - `PLEX_TOKEN` = your Plex token
   - `ENCRYPTION_KEY` = your generated Fernet key
6. Click **Apply**

---

## Outlier Detection Logic

For each new session:

1. Look up the user's **country distribution** over the last 90 days
2. If the user has ≥ `OUTLIER_MIN_SESSIONS` sessions **and** the current country accounts for < `OUTLIER_THRESHOLD` (10%) of their history → create an alert
3. Deduplication: only one alert per user+country per 24 hours
4. Alerts appear in the sidebar and can be acknowledged with one click

**Example:** `alice` has 50 sessions, 48 from GB (96%) and 2 from US (4%). A new session from AU triggers an alert — AU = 0% of history, well below the 10% threshold.

---

## Data & Privacy

- All data stored locally in a Docker volume (`plexgeo_data`)
- IP addresses are sent to [ip-api.com](https://ip-api.com) for geolocation, then cached in SQLite (subsequent lookups don't re-query)
- Local/private IPs (192.168.x.x etc.) are detected and never sent externally — labelled "Local Network"
- Sensitive settings (Plex token) are encrypted at rest using Fernet symmetric encryption
- No external telemetry

---

## Project Structure

```
plexgeo/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── app/
    ├── main.py          # FastAPI app + scheduler
    ├── config.py        # Settings management + encryption
    ├── crypto.py        # Fernet encryption (key from ENCRYPTION_KEY env)
    ├── database.py      # SQLite schema + queries
    ├── plex_poller.py   # Plex session polling
    ├── geo.py           # ip-api.com geolocation + cache
    ├── alerts.py        # Outlier detection
    └── static/
        └── index.html   # Dashboard (D3 map + Chart.js)
```
