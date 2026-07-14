#!/usr/bin/env bash
#
# update_music_metadata.sh
# ---------------------------------------------------------------------------
# Durchsucht rekursiv einen (z.B. via S3 gemounteten) Musik-Ordner und
# aktualisiert bei allen Dateien, denen Cover-Art oder grundlegende
# Metadaten (Titel/Interpret/Album) fehlen, diese Informationen automatisch
# aus dem Internet (MusicBrainz + Cover Art Archive, via "beets").
#
# Läuft komplett nicht-interaktiv (keine Rückfragen, keine Prompts).
#
# Benötigt:
#   - python3 + pip
#   - beets (https://beets.io) inkl. Plugins: fetchart, embedart, chroma
#   - chromaprint/fpcalc (für Audio-Fingerprinting via AcoustID) - optional,
#     aber DRINGEND empfohlen, da sonst nur nach Dateiname/Ordnername
#     gesucht werden kann, was deutlich unzuverlässiger ist.
#   - Ein kostenloser AcoustID API-Key (https://acoustid.org/new-application),
#     als Umgebungsvariable ACOUSTID_API_KEY übergeben.
#
# Aufruf:
#   MUSIC_DIR=/path/to/music ACOUSTID_API_KEY=xxxxx ./update_music_metadata.sh
#
# ---------------------------------------------------------------------------

set -euo pipefail

# ----------------------------- Konfiguration --------------------------------

# Pfad zum (S3-gemounteten) Musik-Ordner. Kann per ENV überschrieben werden.
MUSIC_DIR="${MUSIC_DIR:-/path/to/music}"

# Optionaler AcoustID API-Key für Audio-Fingerprinting (empfohlen).
ACOUSTID_API_KEY="${ACOUSTID_API_KEY:-}"

# Arbeitsverzeichnis für venv, beets-Config und Logs.
WORK_DIR="${WORK_DIR:-$HOME/.music-metadata-tool}"
VENV_DIR="$WORK_DIR/venv"
BEETS_CONFIG="$WORK_DIR/beets_config.yaml"
BEETS_LIBRARY="$WORK_DIR/library.db"
LOG_FILE="$WORK_DIR/update.log"
INCOMPLETE_LIST="$WORK_DIR/incomplete_files.lst"

mkdir -p "$WORK_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# ----------------------------- Vorprüfungen ---------------------------------

if [ ! -d "$MUSIC_DIR" ]; then
  log "FEHLER: Musik-Ordner '$MUSIC_DIR' existiert nicht oder ist nicht gemountet."
  exit 1
fi

log "Starte Metadaten-/Cover-Update für: $MUSIC_DIR"

# ----------------------------- Abhängigkeiten -------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  log "FEHLER: python3 wird benötigt, ist aber nicht installiert."
  exit 1
fi

# fpcalc (Chromaprint) für Audio-Fingerprinting, falls möglich automatisch
# installieren (nur Debian/Ubuntu, nicht-interaktiv).
if ! command -v fpcalc >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    log "fpcalc (chromaprint) nicht gefunden - versuche automatische Installation..."
    export DEBIAN_FRONTEND=noninteractive
    (sudo -n apt-get update -y && sudo -n apt-get install -y chromaprint ffmpeg) \
      >>"$LOG_FILE" 2>&1 || log "WARNUNG: Konnte chromaprint nicht automatisch installieren (sudo-Rechte fehlen?). Fingerprinting wird deaktiviert."
  else
    log "WARNUNG: fpcalc nicht gefunden und kein apt-get verfügbar. Fingerprinting wird deaktiviert."
  fi
fi

# Python venv mit benötigten Paketen anlegen (idempotent).
if [ ! -d "$VENV_DIR" ]; then
  log "Erstelle Python venv unter $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip >>"$LOG_FILE" 2>&1
pip install --quiet --upgrade beets mutagen requests pyacoustid >>"$LOG_FILE" 2>&1

USE_CHROMA="no"
if command -v fpcalc >/dev/null 2>&1 && [ -n "$ACOUSTID_API_KEY" ]; then
  USE_CHROMA="yes"
fi

# ----------------------------- beets-Konfiguration --------------------------
#
# Wichtig:
#   - move/copy: no  -> Dateien bleiben exakt an ihrem Platz (keine
#                        Reorganisation der bestehenden Ordnerstruktur)
#   - write: yes     -> Tags werden in die Dateien geschrieben
#   - quiet: yes / quiet_fallback: asis -> keine Rückfragen, bei Unsicherheit
#                        wird die beste automatische Vermutung übernommen
#   - fetchart/embedart: holen & betten Cover automatisch ein (nur wenn
#                        noch kein Cover vorhanden ist)
#   - chroma: Audio-Fingerprinting über AcoustID für zuverlässige Erkennung
#             auch bei komplett fehlenden Metadaten (nur falls API-Key da)

cat > "$BEETS_CONFIG" <<EOF
directory: $MUSIC_DIR
library: $BEETS_LIBRARY

import:
  move: no
  copy: no
  write: yes
  autotag: yes
  quiet: yes
  quiet_fallback: asis
  resume: no
  log: $WORK_DIR/beets_import.log

match:
  preferred:
    media: ['CD', 'Digital Media']

plugins: fetchart embedart$( [ "$USE_CHROMA" = "yes" ] && echo " chroma" )

fetchart:
  auto: yes
  force: no
  enforce_ratio: no

embedart:
  auto: yes
  ifempty: yes
EOF

if [ "$USE_CHROMA" = "yes" ]; then
  cat >> "$BEETS_CONFIG" <<EOF

acoustid:
  apikey: $ACOUSTID_API_KEY

chroma:
  auto: yes
EOF
  log "Audio-Fingerprinting (AcoustID/Chromaprint) aktiviert."
else
  log "Kein AcoustID-Key/fpcalc gefunden - Erkennung erfolgt nur über vorhandene Tags/Dateinamen (weniger zuverlässig)."
fi

# ----------------------------- Dateien mit fehlenden Daten finden -----------

log "Durchsuche $MUSIC_DIR nach Dateien ohne Cover oder ohne Metadaten..."

python3 - "$MUSIC_DIR" "$INCOMPLETE_LIST" <<'PYEOF'
import sys, os
from mutagen import File as MutagenFile

MUSIC_DIR, OUT_FILE = sys.argv[1], sys.argv[2]
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aac"}

def has_cover(mf):
    try:
        if hasattr(mf, "tags") and mf.tags is not None:
            # ID3 (mp3)
            if any(k.startswith("APIC") for k in mf.tags.keys()):
                return True
            # MP4/M4A
            if "covr" in mf.tags:
                return True
            # FLAC
            if hasattr(mf, "pictures") and mf.pictures:
                return True
            # Vorbis/Opus (embedded as base64 in metadata_block_picture)
            if "metadata_block_picture" in mf.tags:
                return True
    except Exception:
        pass
    return False

def has_basic_tags(mf):
    try:
        easy = MutagenFile(mf.filename, easy=True) if hasattr(mf, "filename") else None
    except Exception:
        easy = None
    tags = easy.tags if easy is not None else getattr(mf, "tags", None)
    if not tags:
        return False
    def get(key):
        try:
            val = tags.get(key)
            if isinstance(val, list):
                return val[0] if val else None
            return val
        except Exception:
            return None
    title = get("title")
    artist = get("artist")
    album = get("album")
    return bool(title) and bool(artist) and bool(album)

incomplete = []
total = 0
for root, _dirs, files in os.walk(MUSIC_DIR):
    for fname in files:
        ext = os.path.splitext(fname)[1].lower()
        if ext not in AUDIO_EXTS:
            continue
        total += 1
        path = os.path.join(root, fname)
        try:
            mf = MutagenFile(path)
        except Exception as e:
            print(f"WARN: konnte {path} nicht lesen ({e})", file=sys.stderr)
            continue
        if mf is None:
            print(f"WARN: unbekanntes/kaputtes Format: {path}", file=sys.stderr)
            continue
        missing_cover = not has_cover(mf)
        missing_tags = not has_basic_tags(mf)
        if missing_cover or missing_tags:
            incomplete.append(path)

with open(OUT_FILE, "w") as f:
    for p in incomplete:
        f.write(p + "\n")

print(f"{total} Audiodateien geprüft, {len(incomplete)} unvollständig.")
PYEOF

INCOMPLETE_COUNT=$(wc -l < "$INCOMPLETE_LIST" | tr -d ' ')

if [ "$INCOMPLETE_COUNT" -eq 0 ]; then
  log "Alle Dateien haben bereits Cover und Metadaten. Nichts zu tun."
  deactivate
  exit 0
fi

log "$INCOMPLETE_COUNT Datei(en) ohne Cover/Metadaten gefunden. Starte automatisches Tagging..."

# ----------------------------- Tagging über beets ---------------------------
#
# Singleton-Modus (-s), da einzelne Dateien (nicht ganze Alben) behandelt
# werden - so bleiben bereits korrekt getaggte Dateien im selben Ordner
# unangetastet.

while IFS= read -r file; do
  [ -f "$file" ] || continue
  log "Verarbeite: $file"
  beet -c "$BEETS_CONFIG" import -q -s "$file" >>"$WORK_DIR/beets_import.log" 2>&1 \
    || log "WARNUNG: Konnte '$file' nicht automatisch taggen (kein sicherer Treffer gefunden)."
done < "$INCOMPLETE_LIST"

# ----------------------------- Cover nachladen -------------------------------
# fetchart/embedart laufen mit "force: no" -> es werden nur Alben/Dateien
# ohne vorhandenes Cover angefasst.

log "Hole fehlende Cover-Art nach..."
beet -c "$BEETS_CONFIG" fetchart -q >>"$WORK_DIR/beets_import.log" 2>&1 || true
beet -c "$BEETS_CONFIG" embedart -q >>"$WORK_DIR/beets_import.log" 2>&1 || true

deactivate

log "Fertig. Details im Log: $WORK_DIR/beets_import.log"
