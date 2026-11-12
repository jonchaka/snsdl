FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PYTHONPATH=/data/lib HOME=/home/appuser

LABEL org.opencontainers.image.source="https://github.com/jonchaka/snsdl" \
      org.opencontainers.image.description="Self-hosted web UI for yt-dlp: paste a link, pick a quality, download on your LAN" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The container starts as root: app.launcher re-owns /data to PUID:PGID (default 1000) and
# drops to that user itself, so changing PUID needs no rebuild. /home/appuser is the HOME.
RUN useradd --system --uid 1000 --create-home appuser \
 && mkdir -p /data && chown -R appuser /data

# Build identity, shown in the footer and /api/status. CI passes the tag or "latest" and the sha;
# the Makefile passes the local commit; a plain docker build shows "dev".
ARG APP_VERSION=dev
ARG APP_COMMIT=
ENV APP_VERSION=$APP_VERSION APP_COMMIT=$APP_COMMIT

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD python -m app.healthcheck
CMD ["python", "-m", "app.launcher"]
