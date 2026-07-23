# Dockerfile for update_music_metadata.sh
# Contains all dependencies: python3, chromaprint/fpcalc, ffmpeg, beets, supercronic etc.

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# System dependencies:
# - chromaprint  -> provides fpcalc for audio fingerprinting (AcoustID)
# - ffmpeg       -> audio decoding for fingerprinting/mutagen
# - python3-venv -> venv module (used by the script)
# - ca-certificates -> for HTTPS access to MusicBrainz/Cover Art Archive
# - supercronic  -> cron-like scheduler for recurring execution
# - curl         -> for webhook notifications
RUN apt-get update && apt-get install -y --no-install-recommends \
        libchromaprint-tools \
        ffmpeg \
        python3-venv \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install supercronic (lightweight cron scheduler for containers)
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.29/supercronic-linux-amd64 \
    SUPERCRONIC=supercronic-linux-amd64 \
    SUPERCRONIC_SHA1=cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b

RUN curl -fsSLO "$SUPERCRONIC_URL" \
    && echo "${SUPERCRONIC_SHA1}  ${SUPERCRONIC}" | sha1sum -c - \
    && chmod +x "$SUPERCRONIC" \
    && mv "$SUPERCRONIC" /usr/local/bin/supercronic

WORKDIR /app

COPY update_music_metadata.sh /app/update_music_metadata.sh
COPY entrypoint.sh /app/entrypoint.sh
COPY scan_incomplete.py /app/scan_incomplete.py
COPY tagging_modes.py /app/tagging_modes.py
COPY metadata_fallback.py /app/metadata_fallback.py
# Ensure utility modules required by the entrypoint and scripts are copied
COPY schedule_utils.py /app/schedule_utils.py
COPY cover_sources.py /app/cover_sources.py
RUN chmod +x /app/update_music_metadata.sh /app/entrypoint.sh

# Inside the container, the music folder is mounted at /music,
# persistent data (beets database, logs) lives under /data.
ENV MUSIC_DIR=/music \
    WORK_DIR=/data \
    SCHEDULE=""

VOLUME ["/music", "/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
