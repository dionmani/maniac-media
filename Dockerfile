# syntax=docker/dockerfile:1.7
#
# Maniac Media – image (published for linux/amd64).
#   docker build -t maniac-media .
#   docker run -p 8193:8000 -v /mnt/user/appdata/maniac-media:/config -v /mnt/user/data:/data maniac-media

# ---------- stage 1: build the SPA (runs natively on the build host, output is arch-independent) ----------
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# type-checking runs in CI; the image build only needs the bundle
RUN npx vite build

# ---------- stage 2: python dependencies ----------
FROM python:3.13-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
RUN python -m venv /venv \
    && /venv/bin/pip install -r /tmp/requirements.txt \
    && /venv/bin/pip install "psycopg[binary]" PyMySQL pyodbc

# ---------- stage 3: ffprobe binary ----------
FROM mwader/static-ffmpeg:7.0.2 AS ffmpeg

# ---------- stage 4: runtime ----------
FROM python:3.13-slim

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Maniac Media" \
      org.opencontainers.image.description="Unified movie & TV automation (Sonarr + Radarr in one)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="MIT" \
      net.unraid.docker.webui="http://[IP]:[PORT:8000]/" \
      net.unraid.docker.icon="https://raw.githubusercontent.com/dionmani/maniac-media/main/frontend/public/logo.png"

# unixodbc: MSSQL driver runtime · tini: signal handling · util-linux: setpriv (drop privileges)
RUN apt-get update \
    && apt-get install -y --no-install-recommends unixodbc tini util-linux \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/config \
    MANIAC_DATA_DIR=/config \
    MANIAC_KEY_FILE=/config/keys/master.key \
    MANIAC_HOST=0.0.0.0 \
    MANIAC_PORT=8000 \
    MANIAC_LOG_FORMAT=console \
    MANIAC_MOVIES_DIR=/data/media/movies \
    MANIAC_TV_DIR=/data/media/tv \
    MANIAC_DOWNLOADS_DIR=/data/downloads/complete \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    MANIAC_VERSION=${VERSION} \
    MANIAC_REVISION=${REVISION}

WORKDIR /app
COPY --from=ffmpeg /ffprobe /usr/local/bin/
COPY --from=deps /venv /venv
COPY backend/ backend/
COPY run.py alembic.ini ./
COPY --from=frontend /src/frontend/dist frontend/dist
COPY docker/entrypoint.sh /entrypoint.sh
# strip CRLF in case the repo was checked out on Windows, then make executable
RUN sed -i 's/\r$//' /entrypoint.sh && chmod 755 /entrypoint.sh && mkdir -p /config /data

VOLUME ["/config"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ.get('MANIAC_PORT','8000')+'/api/health', timeout=4)"

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
