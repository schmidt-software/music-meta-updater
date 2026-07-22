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
# Exit codes:
#   0 = success (all files processed, no critical errors)
#   1 = critical error (music folder not found, python not available, etc.)
#   2 = partial success (some files processed, but errors occurred or no files needed updating)
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
# Environment variables (optional):
#   SCAN_WORKERS       -> parallel file-checking threads (default: 8)
#   WEBHOOK_URL        -> POST JSON health check to this URL on completion
#                         (must be https://, must not point at a
#                         loopback/private/link-local address)
#   TAGGING_MODE       -> default|strict|aggressive|cover_only (default: default)
#   STRONG_REC_THRESH  -> beets matching confidence threshold 0.0-1.0,
#                         overrides TAGGING_MODE's preset if set
#   COVER_SOURCES      -> comma-separated cover art fallback chain, e.g.
#                         "musicbrainz,amazon,discogs" (default chain if unset)
#
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------- Configuration --------------------------------

# Path to the (network-mounted) music folder. Can be overridden via ENV.
MUSIC_DIR="${MUSIC_DIR:-/path/to/music}"

# Optional AcoustID API key for audio fingerprinting (recommended).
ACOUSTID_API_KEY="${ACOUSTID_API_KEY:-}"

# Parallel scanning: number of worker threads (read directly from the
# environment by scan_incomplete.py; default 8 if unset).

# Optional webhook for health check notifications
WEBHOOK_URL="${WEBHOOK_URL:-}"

# Tagging mode / matching confidence (optional)
TAGGING_MODE="${TAGGING_MODE:-default}"
STRONG_REC_THRESH="${STRONG_REC_THRESH:-}"

# Cover art fallback chain (optional)
COVER_SOURCES="${COVER_SOURCES:-}"

# Working directory for venv, beets config and logs.
WORK_DIR="${WORK_DIR:-$HOME/.music-metadata-tool}"
VENV_DIR="$WORK_DIR/venv"
BEETS_CONFIG="$WORK_DIR/beets_config.yaml"
BEETS_LIBRARY="$WORK_DIR/library.db"
LOG_FILE="$WORK_DIR/update.log"
INCOMPLETE_LIST="$WORK_DIR/incomplete_files.lst"
BEETS_IMPORT_LOG="$WORK_DIR/beets_import.log"
METRICS_FILE="$WORK_DIR/metrics.json"

mkdir -p "$WORK_DIR"

# ----------------------------- Metrics tracking ----------------------------

START_TIME=$(date +%s%N)
TOTAL_FILES=0
INCOMPLETE_FILES=0
PROCESSED_FILES=0
TAGGED_FILES=0
ERROR_COUNT=0
WARNING_MESSAGES=()

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Like log(), but for ERROR/WARNING messages: goes to stderr instead of
# stdout, in addition to LOG_FILE.
err() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE" >&2
  ((ERROR_COUNT++))
  WARNING_MESSAGES+=("$*")
}

# Runs "$@", streaming its stdout/stderr live to the console (debug output
# and errors stay visible, not just hidden in a log file) while also
# appending both streams to LOG_FILE for later inspection.
run_logged() {
  "$@" \
    > >(stdbuf -oL tee -a "$LOG_FILE") \
    2> >(stdbuf -oL tee -a "$LOG_FILE" >&2)
}

# Generate JSON health check output
emit_metrics() {
  local exit_code=$1
  local end_time=$(date +%s%N)
  local duration_ms=$(( (end_time - START_TIME) / 1000000 ))
  local duration_sec=$(echo "scale=2; $duration_ms / 1000" | bc)
  local status_str="success"
  
  if [ $exit_code -eq 1 ]; then
    status_str="error"
  elif [ $exit_code -eq 2 ]; then
    status_str="partial_success"
  fi

  # Build warnings JSON array
  local warnings_json="[]"
  if [ ${#WARNING_MESSAGES[@]} -gt 0 ]; then
    warnings_json=$(printf '%s\n' "${WARNING_MESSAGES[@]}" | jq -R . | jq -s . || echo "[]")
  fi

  cat > "$METRICS_FILE" <<EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "status": "$status_str",
  "exit_code": $exit_code,
  "music_dir": "$MUSIC_DIR",
  "total_files_scanned": $TOTAL_FILES,
  "incomplete_files_found": $INCOMPLETE_FILES,
  "files_processed": $PROCESSED_FILES,
  "tags_updated": $TAGGED_FILES,
  "errors": $ERROR_COUNT,
  "warnings": $warnings_json,
  "duration_seconds": $duration_sec,
  "log_file": "$LOG_FILE"
}
EOF

  log "Metrics written to: $METRICS_FILE"
  cat "$METRICS_FILE"

  # Send to webhook if configured
  if [ -n "$WEBHOOK_URL" ]; then
    if ! is_safe_webhook_url "$WEBHOOK_URL"; then
      err "WARNING: WEBHOOK_URL rejected - must be https:// and must not point at a loopback/private/link-local address. Not sending webhook."
    elif command -v curl >/dev/null 2>&1; then
      log "Sending metrics to webhook: $WEBHOOK_URL"
      curl -s -X POST "$WEBHOOK_URL" \
        --max-time 10 --connect-timeout 5 \
        -H "Content-Type: application/json" \
        -d @"$METRICS_FILE" \
        || err "WARNING: Failed to send webhook notification"
    else
      err "WARNING: curl not available, cannot send webhook notification"
    fi
  fi
}

# Reject webhook destinations that are obviously unsafe to POST internal
# file paths / error text to: non-https schemes, and loopback/link-local/
# private-range hosts (a basic SSRF guard - WEBHOOK_URL should be treated
# as a trusted, operator-only setting, but this adds defense in depth).
is_safe_webhook_url() {
  local url="$1"
  if [[ ! "$url" =~ ^https:// ]]; then
    return 1
  fi
  local host="${url#https://}"
  host="${host%%/*}"
  host="${host%%@*}"  # drop any userinfo@
  host="${host##*@}"
  host="${host%%:*}"  # drop port
  case "$host" in
    localhost|127.*|0.0.0.0|169.254.*|10.*|192.168.*|"")
      return 1
      ;;
  esac
  if [[ "$host" =~ ^172\.(1[6-9]|2[0-9]|3[0-1])\. ]]; then
    return 1
  fi
  return 0
}

# ----------------------------- Pre-checks -----------------------------------

if [ ! -d "$MUSIC_DIR" ]; then
  err "ERROR: Music folder '$MUSIC_DIR' does not exist or is not mounted."
  emit_metrics 1
  exit 1
fi

log "Starting metadata/cover update for: $MUSIC_DIR"

# ----------------------------- Dependencies ---------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  err "ERROR: python3 is required but not installed."
  emit_metrics 1
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

# Resolve the beets match.strong_rec_thresh value: TAGGING_MODE selects a
# preset (default/strict/aggressive/cover_only), STRONG_REC_THRESH - if
# set - overrides it. Both are validated up front and fail the run
# loudly on bad input, since this is user-editable config.
RESOLVED_STRONG_REC_THRESH=$(PYTHONPATH="$SCRIPT_DIR" python3 -c "
import sys
import tagging_modes as tm
import schedule_utils as su

mode = sys.argv[1]
override = sys.argv[2]

if not tm.validate_mode(mode):
    print(f'Unknown TAGGING_MODE: {mode}. Valid modes: {list(tm.TAGGING_MODES.keys())}', file=sys.stderr)
    sys.exit(1)

threshold = tm.get_mode_config(mode).get('strong_rec_thresh', 0.85)

if override:
    valid, value_or_error = su.validate_threshold(override)
    if not valid:
        print(f'Invalid STRONG_REC_THRESH: {value_or_error}', file=sys.stderr)
        sys.exit(1)
    threshold = value_or_error

print(threshold)
" "$TAGGING_MODE" "$STRONG_REC_THRESH") || {
  err "ERROR: Invalid TAGGING_MODE/STRONG_REC_THRESH configuration."
  emit_metrics 1
  exit 1
}
log "Matching confidence threshold: $RESOLVED_STRONG_REC_THRESH (mode: $TAGGING_MODE)"

# Resolve the beets fetchart config section from COVER_SOURCES (falls
# back to the default musicbrainz/amazon/discogs chain if unset).
FETCHART_CONFIG=$(PYTHONPATH="$SCRIPT_DIR" python3 -c "
import sys
import cover_sources as cs

sources, error = cs.parse_cover_sources_string(sys.argv[1])
if error:
    print(f'Invalid COVER_SOURCES: {error}', file=sys.stderr)
    sys.exit(1)
sys.stdout.write(cs.generate_beets_fetchart_config(sources))
" "$COVER_SOURCES") || {
  err "ERROR: Invalid COVER_SOURCES configuration."
  emit_metrics 1
  exit 1
}

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
  log: $BEETS_IMPORT_LOG

match:
  strong_rec_thresh: $RESOLVED_STRONG_REC_THRESH
  preferred:
    media: ['CD', 'Digital Media']

plugins: fetchart embedart$( [ "$USE_CHROMA" = "yes" ] && echo " chroma" )

$FETCHART_CONFIG
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

# ----------------------------- Scan + update ---------------------------------
#
# scan_incomplete.py checks files for missing cover art/tags with a pool of
# worker threads (SCAN_WORKERS, default 8 - I/O-bound, so concurrency helps
# a lot on network mounts) and, the moment a file is found incomplete,
# hands it to beets (import -s, then fetchart/embedart scoped to just that
# item) right away instead of waiting for the whole scan to finish first.

log "Scanning $MUSIC_DIR for files without cover art or metadata, updating as they're found..."

# Capture stdout to a temp file (in addition to the log) so the summary
# line can be parsed afterwards for metrics.
SCAN_OUTPUT_FILE="$WORK_DIR/scan_output.tmp"
python3 "$SCRIPT_DIR/scan_incomplete.py" "$MUSIC_DIR" "$INCOMPLETE_LIST" "$BEETS_CONFIG" "$MTIME_DB" \
  > >(stdbuf -oL tee -a "$LOG_FILE" "$SCAN_OUTPUT_FILE") \
  2> >(stdbuf -oL tee -a "$LOG_FILE" >&2)

SCAN_SUMMARY_LINE=$(grep "audio files checked" "$SCAN_OUTPUT_FILE" | tail -1)
rm -f "$SCAN_OUTPUT_FILE"
if [[ $SCAN_SUMMARY_LINE =~ ([0-9]+)\ audio\ files\ checked,\ ([0-9]+)\ incomplete,\ ([0-9]+)\ updated,\ ([0-9]+)\ failed ]]; then
  TOTAL_FILES="${BASH_REMATCH[1]}"
  INCOMPLETE_FILES="${BASH_REMATCH[2]}"
  PROCESSED_FILES="${BASH_REMATCH[2]}"
  TAGGED_FILES="${BASH_REMATCH[3]}"
  FAILED_FILES="${BASH_REMATCH[4]}"
  log "Scan complete: $TOTAL_FILES checked, $INCOMPLETE_FILES incomplete, $TAGGED_FILES tagged, $FAILED_FILES failed"
  if [ "$FAILED_FILES" -gt 0 ]; then
    err "WARNING: $FAILED_FILES file(s) could not be automatically tagged (no confident match) - see log for details."
  fi
fi

deactivate

log "Done. Details in the log: $BEETS_IMPORT_LOG"

# ----------------------------- Emit metrics and exit -------------------------

EXIT_CODE=0
if [ $ERROR_COUNT -gt 0 ]; then
  EXIT_CODE=2  # Partial success (some warnings/errors occurred)
fi

emit_metrics $EXIT_CODE
exit $EXIT_CODE
