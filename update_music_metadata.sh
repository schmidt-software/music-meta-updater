#!/usr/bin/env bash
#
# update_music_metadata.sh
# ---------------------------------------------------------------------------
# Recursively scans a (e.g. network-mounted) music folder and automatically
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------- Configuration --------------------------------

# Path to the (network-mounted) music folder. Can be overridden via ENV.
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

# Like log(), but for ERROR/WARNING messages: goes to stderr instead of
# stdout, in addition to LOG_FILE.
err() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE" >&2
}

# Runs "$@", streaming its stdout/stderr live to the console (debug output
# and errors stay visible, not just hidden in a log file) while also
# appending both streams to LOG_FILE for later inspection.
run_logged() {
  "$@" \
    > >(tee -a "$LOG_FILE") \
    2> >(tee -a "$LOG_FILE" >&2)
}

# ----------------------------- Pre-checks -----------------------------------

if [ ! -d "$MUSIC_DIR" ]; then
  err "ERROR: Music folder '$MUSIC_DIR' does not exist or is not mounted."
  exit 1
fi

log "Starting metadata/cover update for: $MUSIC_DIR"

# ----------------------------- Dependencies ---------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  err "ERROR: python3 is required but not installed."
  exit 1
fi

# Try to automatically install fpcalc (Chromaprint) for audio fingerprinting,
# if possible (Debian/Ubuntu only, non-interactive).
if ! command -v fpcalc >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    log "fpcalc (chromaprint) not found - attempting automatic install..."
    export DEBIAN_FRONTEND=noninteractive
    (run_logged sudo -n apt-get update -y && run_logged sudo -n apt-get install -y chromaprint ffmpeg) \
      || err "WARNING: Could not automatically install chromaprint (missing sudo rights?). Fingerprinting will be disabled."
  else
    err "WARNING: fpcalc not found and apt-get not available. Fingerprinting will be disabled."
  fi
fi

# Create Python venv with required packages (idempotent).
if [ ! -d "$VENV_DIR" ]; then
  log "Creating Python venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
log "Installing/updating Python dependencies..."
run_logged pip install --upgrade pip
run_logged pip install --upgrade beets mutagen requests pyacoustid

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

run_logged python3 "$SCRIPT_DIR/scan_incomplete.py" "$MUSIC_DIR" "$INCOMPLETE_LIST"

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
  run_logged beet -c "$BEETS_CONFIG" import -q -s "$file" \
    || err "WARNING: Could not automatically tag '$file' (no confident match found)."
done < "$INCOMPLETE_LIST"

# ----------------------------- Fetch cover art -------------------------------
# fetchart/embedart run with "force: no" -> only albums/files without an
# existing cover are touched.

log "Fetching missing cover art..."
run_logged beet -c "$BEETS_CONFIG" fetchart -q || true
run_logged beet -c "$BEETS_CONFIG" embedart -q || true

deactivate

log "Done. Details in the log: $WORK_DIR/beets_import.log"
