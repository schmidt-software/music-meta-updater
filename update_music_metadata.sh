#!/usr/bin/env bash
#
# update_music_metadata.sh
# ---------------------------------------------------------------------------
# Recursively scans a (e.g. S3-mounted) music folder and automatically
# updates any files missing cover art or basic metadata
# (title/artist/album) from the internet (MusicBrainz + Cover Art Archive,
# via "beets").
#
# Runs completely non-interactively (no prompts, no confirmations).
#
# Requires:
#   - python3 + pip
#   - beets (https://beets.io) incl. plugins: fetchart, embedart, chroma
#   - chromaprint/fpcalc (for audio fingerprinting via AcoustID) - optional,
#     but STRONGLY recommended, since otherwise only filename/foldername
#     matching is possible, which is much less reliable.
#   - A free AcoustID API key (https://acoustid.org/new-application),
#     passed as the ACOUSTID_API_KEY environment variable.
#
# Usage:
#   MUSIC_DIR=/path/to/music ACOUSTID_API_KEY=xxxxx ./update_music_metadata.sh
#
# ---------------------------------------------------------------------------

set -euo pipefail

# ----------------------------- Configuration --------------------------------

# Path to the (S3-mounted) music folder. Can be overridden via ENV.
MUSIC_DIR="${MUSIC_DIR:-/path/to/music}"

# Optional AcoustID API key for audio fingerprinting (recommended).
ACOUSTID_API_KEY="${ACOUSTID_API_KEY:-}"

# Working directory for venv, beets config and logs.
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

# ----------------------------- Pre-checks -----------------------------------

if [ ! -d "$MUSIC_DIR" ]; then
  log "ERROR: Music folder '$MUSIC_DIR' does not exist or is not mounted."
  exit 1
fi

log "Starting metadata/cover update for: $MUSIC_DIR"

# ----------------------------- Dependencies ---------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  log "ERROR: python3 is required but not installed."
  exit 1
fi

# Try to automatically install fpcalc (Chromaprint) for audio fingerprinting,
# if possible (Debian/Ubuntu only, non-interactive).
if ! command -v fpcalc >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    log "fpcalc (chromaprint) not found - attempting automatic install..."
    export DEBIAN_FRONTEND=noninteractive
    (sudo -n apt-get update -y && sudo -n apt-get install -y chromaprint ffmpeg) \
      >>"$LOG_FILE" 2>&1 || log "WARNING: Could not automatically install chromaprint (missing sudo rights?). Fingerprinting will be disabled."
  else
    log "WARNING: fpcalc not found and apt-get not available. Fingerprinting will be disabled."
  fi
fi

# Create Python venv with required packages (idempotent).
if [ ! -d "$VENV_DIR" ]; then
  log "Creating Python venv at $VENV_DIR ..."
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

# ----------------------------- beets configuration ---------------------------
#
# Important:
#   - move/copy: no  -> files stay exactly in place (no reorganization
#                        of the existing folder structure)
#   - write: yes     -> tags are written into the files
#   - quiet: yes / quiet_fallback: asis -> no prompts, falls back to the
#                        best automatic guess when uncertain
#   - fetchart/embedart: automatically fetch & embed cover art (only if
#                        no cover exists yet)
#   - chroma: audio fingerprinting via AcoustID for reliable detection
#             even with completely missing metadata (only if API key is set)

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
  log "Audio fingerprinting (AcoustID/Chromaprint) enabled."
else
  log "No AcoustID key/fpcalc found - detection will rely only on existing tags/filenames (less reliable)."
fi

# ----------------------------- Find files with missing data -----------------

log "Scanning $MUSIC_DIR for files without cover art or metadata..."

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
            print(f"WARN: could not read {path} ({e})", file=sys.stderr)
            continue
        if mf is None:
            print(f"WARN: unknown/corrupt format: {path}", file=sys.stderr)
            continue
        missing_cover = not has_cover(mf)
        missing_tags = not has_basic_tags(mf)
        if missing_cover or missing_tags:
            incomplete.append(path)

with open(OUT_FILE, "w") as f:
    for p in incomplete:
        f.write(p + "\n")

print(f"{total} audio files checked, {len(incomplete)} incomplete.")
PYEOF

INCOMPLETE_COUNT=$(wc -l < "$INCOMPLETE_LIST" | tr -d ' ')

if [ "$INCOMPLETE_COUNT" -eq 0 ]; then
  log "All files already have cover art and metadata. Nothing to do."
  deactivate
  exit 0
fi

log "$INCOMPLETE_COUNT file(s) without cover/metadata found. Starting automatic tagging..."

# ----------------------------- Tagging via beets -----------------------------
#
# Singleton mode (-s), since individual files (not whole albums) are
# processed - this way already correctly tagged files in the same folder
# stay untouched.

while IFS= read -r file; do
  [ -f "$file" ] || continue
  log "Processing: $file"
  beet -c "$BEETS_CONFIG" import -q -s "$file" >>"$WORK_DIR/beets_import.log" 2>&1 \
    || log "WARNING: Could not automatically tag '$file' (no confident match found)."
done < "$INCOMPLETE_LIST"

# ----------------------------- Fetch cover art -------------------------------
# fetchart/embedart run with "force: no" -> only albums/files without an
# existing cover are touched.

log "Fetching missing cover art..."
beet -c "$BEETS_CONFIG" fetchart -q >>"$WORK_DIR/beets_import.log" 2>&1 || true
beet -c "$BEETS_CONFIG" embedart -q >>"$WORK_DIR/beets_import.log" 2>&1 || true

deactivate

log "Done. Details in the log: $WORK_DIR/beets_import.log"
