# Music Metadata Updater

Recursively scans a (e.g. network-mounted) music folder and automatically
updates any files missing cover art or metadata from the internet
(MusicBrainz + Cover Art Archive via `beets`). Runs fully
non-interactively.

Scanning checks multiple files concurrently (`SCAN_WORKERS`, default 8 -
tune this up on high-latency network mounts where each file check is a
network round trip), and each file gets tagged/cover-art-updated the
moment it's found incomplete, rather than waiting for the whole scan to
finish first.

## Setup

```bash
cp .env.example .env
# edit .env: MUSIC_HOST_PATH=<mounted music folder path>, ACOUSTID_API_KEY=<key>
docker compose up --build
```

`ACOUSTID_API_KEY` is optional but recommended (free at
acoustid.org) – without a key, files with completely missing tags can
only be guessed from the filename, which is much less reliable.

## Files

- `update_music_metadata.sh` – Main script. Sets up the Python venv and
  beets config (including tagging-mode/threshold and cover-source
  settings), then hands off to `scan_incomplete.py` for scanning and
  tagging, and emits exit codes / JSON metrics / an optional webhook
  notification on completion. Leaves already-complete files untouched
  and doesn't move/rename anything (existing folder structure is
  preserved).
- `Dockerfile` – Image with all dependencies (python3, chromaprint/fpcalc,
  ffmpeg, beets, mutagen, pyacoustid).
- `docker-compose.yml` – Mounts the music folder to `/music` plus a
  persistent volume `/data` for the beets database, metrics, and logs.
- `.env.example` – Template for the host path and AcoustID key.
- `scan_incomplete.py` – Scans all audio files with Python/mutagen for
  missing tags (title/artist/album) or missing cover art (`has_cover`,
  `has_basic_tags`), checking multiple files concurrently via a thread
  pool. The moment a file is found incomplete, it's handed to a single
  update worker thread that runs `beet import` (tagging) then `beet
  fetchart`/`embedart` scoped to just that file (`path:<file>`) - so
  updates start immediately instead of waiting for the whole scan to
  finish. Kept in its own module so the logic is unit testable.
- `tagging_modes.py` / `schedule_utils.py` / `cover_sources.py` –
  validation and config-generation helpers for `TAGGING_MODE`/
  `STRONG_REC_THRESH`, `SCHEDULE` (cron), and `COVER_SOURCES`
  respectively, used by `update_music_metadata.sh`/`entrypoint.sh`.
- `metadata_fallback.py` – Best-effort artist/album extraction from
  folder names, for future use as a fallback when beets can't find a
  confident match.
- `entrypoint.sh` – Container entrypoint: runs once (one-shot) or sets
  up `supercronic` for recurring execution when `SCHEDULE` is set.
- `tests/` – Unit tests for the modules above.

## Supported mount types

The tool only relies on standard POSIX file operations (directory
listing, read, write-in-place) on the folder given as `MUSIC_DIR` /
`MUSIC_HOST_PATH` – it doesn't care how that folder got mounted. It
works with any mount that shows up as a regular directory, e.g.:

- Object storage mounted as a filesystem (S3 via `s3fs`, `goofys`,
  `rclone mount`, etc.)
- NFS
- SFTP (via `sshfs`)
- SMB/CIFS
- Local disks / regular directories

The mount itself must be set up on the Docker **host** before running
`docker compose up` (see `docker-compose.yml`) – the container only
bind-mounts the already-mounted host path into `/music`, it never
performs the mount itself. This keeps the container simple and avoids
needing extra privileges (e.g. `SYS_ADMIN` / `/dev/fuse`) that
FUSE-based mounts (s3fs, sshfs, rclone) would otherwise require inside
the container.

Transient I/O errors while scanning – e.g. a flaky network mount
timing out while reading a file or listing a directory – are logged as
warnings and skipped; they don't abort the whole run.

## Direct invocation (without Docker)

### Installing requirements

System dependencies (Debian/Ubuntu example):

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ffmpeg chromaprint ca-certificates
```

The script creates its own Python virtual environment under `$WORK_DIR/venv`
(default `~/.music-metadata-tool/venv`) on first run and installs the
required Python packages (`beets`, `mutagen`, `requests`, `pyacoustid`) into
it automatically – no manual `pip install` needed.

### Running

```bash
MUSIC_DIR=/mnt/music ACOUSTID_API_KEY=abcd1234efgh5678 ./update_music_metadata.sh
```

## Example

```bash
# .env
MUSIC_HOST_PATH=/mnt/music
ACOUSTID_API_KEY=abcd1234efgh5678

docker compose up --build
```

## Logging / console output

The script prints its current status (dependency install, scanning,
tagging, fetching cover art, ...) to the console as it progresses. Debug
output from sub-processes (`pip`, `apt-get`, `beet`) is streamed live
too instead of being hidden away, so you can watch what's happening;
errors and warnings go to stderr, everything else to stdout. All of it
is additionally appended to `$WORK_DIR/update.log`
(default `~/.music-metadata-tool/update.log`, `/data/update.log` in the
container) for later inspection.

## Tests

Unit tests cover the detection logic in `scan_incomplete.py` (missing
cover art / missing tags, the per-file check/update/heartbeat helpers,
and a `scan_and_update` integration test) with mocked mutagen objects
and a fake `subprocess.run`, so they run without needing real audio
files or beets installed.

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Performance

`scan_incomplete.py` checks audio files concurrently using a pool of
worker threads (I/O-bound work, so concurrency helps a lot on network
mounts). By default it uses 8 worker threads; the moment a file is
found incomplete, it's handed off to a single dedicated update worker
thread that runs `beet import`/`fetchart`/`embedart` scoped to just
that file - so tagging starts immediately instead of waiting for the
whole scan to finish first. A background heartbeat prints progress
every few seconds so long-running scans on large/slow mounts don't
look stuck.

New: UPDATER_WORKERS (default: 1) controls the number of updater threads.
These threads can perform per-file pre-processing (e.g., fallback tag
application) concurrently, while all beets subprocess invocations remain
serialized to protect the beets/SQLite DB. Use a value >1 to reduce idle
preparation time when SCAN_WORKERS is high and your host has CPU/I/O to
spare. Example:

```bash
SCAN_WORKERS=32 UPDATER_WORKERS=4 MUSIC_DIR=/music ./update_music_metadata.sh
```

Tune UPDATER_WORKERS gradually; default 1 preserves prior safe behavior.

Control the number of scan workers via the `SCAN_WORKERS` environment variable:

```bash
SCAN_WORKERS=4 MUSIC_DIR=/music ./update_music_metadata.sh
```

Or in `docker-compose.yml` environment section:
```yaml
environment:
  SCAN_WORKERS: "4"
```

## Monitoring & Integration

### Exit Codes

The script returns meaningful exit codes for automation/orchestration:

- **0** = Success: All files processed, no errors
- **1** = Critical error: Music folder not found, Python not installed, etc.
- **2** = Partial success: Some files processed, but warnings/errors occurred

### Health Check Output (JSON)

After each run, metrics are written to `$WORK_DIR/metrics.json`:

```json
{
  "timestamp": "2024-07-18T21:54:00Z",
  "status": "success",
  "exit_code": 0,
  "music_dir": "/music",
  "total_files_scanned": 1247,
  "incomplete_files_found": 45,
  "files_processed": 45,
  "tags_updated": 45,
  "errors": 0,
  "warnings": [],
  "duration_seconds": 23.45,
  "log_file": "/data/update.log"
}
```

### Webhook Notifications

Optionally send health check metrics to a webhook URL on completion:

```bash
WEBHOOK_URL=https://your-monitoring.example.com/webhook MUSIC_DIR=/music ./update_music_metadata.sh
```

Or in `docker-compose.yml`:
```yaml
environment:
  WEBHOOK_URL: "https://your-monitoring.example.com/webhook"
```

The JSON metrics are POSTed to the URL with `Content-Type: application/json`.
Useful for Kubernetes Events, Alerting Systems, or Custom Dashboards.

## Fallback metadata (automatic)

When beets cannot confidently match a file, the tool can optionally attempt a path-based fallback to infer artist and album from the folder structure and apply those tags directly into the file before retrying beets import. This improves client-side grouping (e.g., Navidrome) for single-track "Unknown Album" cases.

Environment variables:
- FALLBACK_APPLY (default: true) — when true, apply path-based fallback tags automatically after a beets match failure. Set to "false" to disable automatic fallback tagging.
- FAILED_MATCH_DB (optional) — path to an SQLite DB file where failed (unmatched) files are recorded. If set, failures are recorded atomically in a failed_matches table for later inspection.

Notes:
- Automatic tagging uses Mutagen to write tags in-place. Back up your music before enabling wide-scale runs if you are concerned about incorrect tags.
- The fallback heuristic is best-effort and conservative; it uses folder patterns like "Artist/Album" and "Artist - Album" and applies Unicode NFC normalization.

## Open items / possible next steps

- Recurring execution (cron in the container, or external scheduling)
- Finer control over matching thresholds in the beets config
  (`match.strong_rec_thresh` etc.), in case mismatches occur
- Test with real sample files from the mounted folder before running against
  the whole library (`MUSIC_HOST_PATH` pointing to a small subfolder
  for testing)
- Incremental cover art fetching (only re-fetch covers for recently modified files)
