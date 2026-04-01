FROM python:3.12-slim

LABEL maintainer="Inch-high"
LABEL org.opencontainers.image.title="PlexGeo"
LABEL org.opencontainers.image.description="Stream intelligence dashboard for Plex — live map, session logs, anomaly alerts"
LABEL org.opencontainers.image.source="https://github.com/Inch-high/plexgeo"
LABEL org.opencontainers.image.url="https://github.com/Inch-high/plexgeo"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV DB_PATH=/data/plexgeo.db

EXPOSE 7842

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7842/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7842"]
