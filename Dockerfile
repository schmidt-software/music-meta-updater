# Dockerfile für update_music_metadata.sh
# Enthält alle Abhängigkeiten: python3, chromaprint/fpcalc, ffmpeg, beets etc.

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Systemabhängigkeiten:
# - chromaprint  -> liefert fpcalc für Audio-Fingerprinting (AcoustID)
# - ffmpeg       -> Audio-Decoding für Fingerprinting/mutagen
# - python3-venv -> venv-Modul (wird vom Skript verwendet)
# - ca-certificates -> für HTTPS-Zugriffe auf MusicBrainz/Cover Art Archive
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromaprint \
        ffmpeg \
        python3-venv \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY update_music_metadata.sh /app/update_music_metadata.sh
RUN chmod +x /app/update_music_metadata.sh

# Innerhalb des Containers ist der Musik-Ordner unter /music gemountet,
# persistente Daten (beets-Datenbank, Logs) liegen unter /data.
ENV MUSIC_DIR=/music \
    WORK_DIR=/data

VOLUME ["/music", "/data"]

ENTRYPOINT ["/app/update_music_metadata.sh"]
