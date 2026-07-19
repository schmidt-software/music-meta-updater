#!/bin/bash
#
# entrypoint.sh - Container entrypoint with recurring execution support
#
# Modes:
#   1. One-shot (no SCHEDULE): Run update_music_metadata.sh once and exit
#   2. Recurring (SCHEDULE set): Use supercronic to run on schedule
#
# Environment variables:
#   SCHEDULE: cron expression (e.g., "0 2 * * *" for 2 AM daily)
#             Leave empty or unset for one-shot mode
#   MUSIC_DIR: Path to music folder (default: /music)
#   WORK_DIR: Working directory for databases/logs (default: /data)
#   Other: passed to update_music_metadata.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MUSIC_DIR="${MUSIC_DIR:-/music}"
WORK_DIR="${WORK_DIR:-/data}"
SCHEDULE="${SCHEDULE:-}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Music Metadata Updater - Entrypoint"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] MUSIC_DIR: $MUSIC_DIR"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] WORK_DIR: $WORK_DIR"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] SCHEDULE: ${SCHEDULE:-<not set, one-shot mode>}"

# Create work directory if it doesn't exist
mkdir -p "$WORK_DIR"

if [ -z "$SCHEDULE" ]; then
  # One-shot mode: run once and exit
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running in one-shot mode"
  exec "$SCRIPT_DIR/update_music_metadata.sh"
else
  # Recurring mode: setup supercronic
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running in recurring mode with schedule: $SCHEDULE"

  # Validate the cron expression before it gets interpolated into the
  # crontab file below - an invalid/malformed SCHEDULE (wrong field count,
  # stray tokens) must be rejected here with a clear error, not silently
  # written into the crontab and left for supercronic's own parser (or
  # worse, misinterpreted as extra fields/commands) to discover later.
  if ! PYTHONPATH="$SCRIPT_DIR" python3 -c '
import sys
import schedule_utils as su

valid, error = su.validate_cron_expression(sys.argv[1])
if not valid:
    print(f"Invalid SCHEDULE cron expression: {error}", file=sys.stderr)
    sys.exit(1)
' "$SCHEDULE"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: SCHEDULE is not a valid 5-field cron expression: '$SCHEDULE'" >&2
    exit 1
  fi

  # Create a crontab file for supercronic
  CRONTAB_FILE="$WORK_DIR/crontab"
  cat > "$CRONTAB_FILE" <<EOF
# Crontab for music metadata updater
# Schedule: $SCHEDULE
$SCHEDULE $SCRIPT_DIR/update_music_metadata.sh
EOF
  
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Crontab file created at $CRONTAB_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting supercronic..."
  
  # Run supercronic in foreground (required for container)
  exec supercronic "$CRONTAB_FILE"
fi
