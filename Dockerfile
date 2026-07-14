# Dockerfile for update_music_metadata.sh
# Contains all dependencies: python3, chromaprint/fpcalc, ffmpeg, beets etc.

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# System dependencies:
# - chromaprint  -> provides fpcalc for audio fingerprinting (AcoustID)
# - ffmpeg       -> audio decoding for fingerprinting/mutagen
# - python3-venv -> venv module (used by the script)
# - ca-certificates -> for HTTPS access to MusicBrainz/Cover Art Archive
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromaprint \
        ffmpeg \
        python3-venv \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY update_music_metadata.sh /app/update_music_metadata.sh
RUN chmod +x /app/update_music_metadata.sh

# Inside the container, the music folder is mounted at /music,
# persistent data (beets database, logs) lives under /data.
ENV MUSIC_DIR=/music \
    WORK_DIR=/data

VOLUME ["/music", "/data"]

ENTRYPOINT ["/app/update_music_metadata.sh"]
