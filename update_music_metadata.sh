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
#   BATCH_IMPORT_SIZE  -> files per beet import call (default: 50)
#   SCAN_WORKERS       -> parallel file checking threads (default: CPU count + 1, max 8)
#   WEBHOOK_URL        -> POST JSON health check to this URL on completion
#                         (must be https://, must not point at a
#                         loopback/private/link-local address)
#
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ----------------------------- Configuration --------------------------------

# Path to the (network-mounted) music folder. Can be overridden via ENV.
MUSIC_DIR="${MUSIC_DIR:-/path/to/music}"

# Optional AcoustID API key for audio fingerprinting (recommended).
ACOUSTID_API_KEY="${ACOUSTID_API_KEY:-}"

# Parallel scanning: number of worker threads (optional, auto-detected if not set)
SCAN_WORKERS="${SCAN_WORKERS:-}"

# Batch import: files per beet import call
BATCH_IMPORT_SIZE="${BATCH_IMPORT_SIZE:-50}"

# Optional webhook for health check notifications
WEBHOOK_URL="${WEBHOOK_URL:-}"

# Working directory for venv, beets config and logs.
WORK_DIR="${WORK_DIR:-$HOME/.music-metadata-tool}"
VENV_DIR="$WORK_DIR/venv"
BEETS_CONFIG="$WORK_DIR/beets_config.yaml"
BEETS_LIBRARY="$WORK_DIR/library.db"
MTIME_DB="$WORK_DIR/mtime_tracking.db"
ERROR_DB="$WORK_DIR/error_tracking.db"
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
    > >(tee -a "$LOG_FILE") \
    2> >(tee -a "$LOG_FILE" >&2)
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

log "Scanning $MUSIC_DIR for files without cover art or metadata (parallel mode)..."

# Build scan_incomplete.py arguments. Order must match scan_incomplete.py's
# main(): music_dir, out_file, mtime_db, error_db, [num_workers].
SCAN_ARGS=("$MUSIC_DIR" "$INCOMPLETE_LIST" "$MTIME_DB" "$ERROR_DB")
if [ -n "$SCAN_WORKERS" ]; then
  SCAN_ARGS+=("$SCAN_WORKERS")
fi

# Run the scan exactly once, capturing its stdout to a temp file (in
# addition to the log) so the summary line can be parsed afterwards.
# Do NOT re-run the scan a second time to "capture the final line" - by
# the time a second scan would run, mtime tracking has already been
# updated for every file the first scan just looked at, so a second
# invocation would see everything as "unchanged since last scan" and
# report ~0 incomplete files, silently skipping the tagging step below.
SCAN_OUTPUT_FILE="$WORK_DIR/scan_output.tmp"
python3 "$SCRIPT_DIR/scan_incomplete.py" "${SCAN_ARGS[@]}" \
  > >(tee -a "$LOG_FILE" "$SCAN_OUTPUT_FILE") \
  2> >(tee -a "$LOG_FILE" >&2)

SCAN_SUMMARY_LINE=$(grep "audio files checked" "$SCAN_OUTPUT_FILE" | tail -1)
rm -f "$SCAN_OUTPUT_FILE"
if [[ $SCAN_SUMMARY_LINE =~ ([0-9]+)\ audio\ files\ checked,\ ([0-9]+)\ incomplete ]]; then
  TOTAL_FILES="${BASH_REMATCH[1]}"
fi
# The incomplete count comes from the actual list scan_incomplete.py
# wrote, not from re-parsing log text - it's the authoritative source
# and can't drift out of sync with what the tagging step below reads.
INCOMPLETE_FILES=$(wc -l < "$INCOMPLETE_LIST" | tr -d ' ')
log "Scan complete: $TOTAL_FILES files checked, $INCOMPLETE_FILES incomplete"

if [ "$INCOMPLETE_FILES" -eq 0 ]; then
  log "All files already have cover art and metadata. Nothing to do."
  deactivate
  emit_metrics 0
  exit 0
fi

log "$INCOMPLETE_FILES file(s) without cover/metadata found. Starting automatic tagging (batch mode)..."

# ----------------------------- Tagging via beets (batch mode) ----------------
#
# Batch import: collect BATCH_IMPORT_SIZE files and pass them together to beet.
# This is much faster than single-file imports. Singleton mode (-s) ensures
# already correctly tagged files in the same folder stay untouched.

BATCH_FILES=()

while IFS= read -r file; do
  [ -f "$file" ] || continue
  BATCH_FILES+=("$file")

  # When batch reaches BATCH_IMPORT_SIZE, process it
  if [ ${#BATCH_FILES[@]} -ge "$BATCH_IMPORT_SIZE" ]; then
    log "Processing batch of ${#BATCH_FILES[@]} files..."
    if run_logged beet -c "$BEETS_CONFIG" import -q -s "${BATCH_FILES[@]}"; then
      TAGGED_FILES=$((TAGGED_FILES + ${#BATCH_FILES[@]}))
    else
      err "WARNING: Some files in batch could not be automatically tagged (no confident match)."
    fi
    PROCESSED_FILES=$((PROCESSED_FILES + ${#BATCH_FILES[@]}))
    BATCH_FILES=()
  fi
done < "$INCOMPLETE_LIST"

# Process any remaining files in final batch
if [ ${#BATCH_FILES[@]} -gt 0 ]; then
  log "Processing final batch of ${#BATCH_FILES[@]} files..."
  if run_logged beet -c "$BEETS_CONFIG" import -q -s "${BATCH_FILES[@]}"; then
    TAGGED_FILES=$((TAGGED_FILES + ${#BATCH_FILES[@]}))
  else
    err "WARNING: Some files in final batch could not be automatically tagged (no confident match)."
  fi
  PROCESSED_FILES=$((PROCESSED_FILES + ${#BATCH_FILES[@]}))
fi

# ----------------------------- Fetch cover art -------------------------------
# fetchart/embedart run with "force: no" -> only albums/files without an
# existing cover are touched.

log "Fetching missing cover art..."
run_logged beet -c "$BEETS_CONFIG" fetchart -q || true
run_logged beet -c "$BEETS_CONFIG" embedart -q || true

deactivate

log "Done. Details in the log: $BEETS_IMPORT_LOG"

# ----------------------------- Emit metrics and exit -------------------------

EXIT_CODE=0
if [ $ERROR_COUNT -gt 0 ]; then
  EXIT_CODE=2  # Partial success (some warnings/errors occurred)
fi

emit_metrics $EXIT_CODE
exit $EXIT_CODE
