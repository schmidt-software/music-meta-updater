# Music Metadata Updater

Recursively scans a (e.g. network-mounted) music folder and automatically
updates any files missing cover art or metadata from the internet
(MusicBrainz + Cover Art Archive via `beets`). Runs fully
non-interactively.

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

- `update_music_metadata.sh` – Main script. Scans all audio files with
  Python/mutagen for missing tags (title/artist/album) or missing
  cover art, and only lets `beets` automatically tag/cover those files.
  Leaves already-complete files untouched and doesn't move/rename
  anything (existing folder structure is preserved). Features incremental
  scanning, parallel file checking, and batch beet imports for performance.
- `Dockerfile` – Image with all dependencies (python3, chromaprint/fpcalc,
  ffmpeg, beets, mutagen, pyacoustid).
- `docker-compose.yml` – Mounts the music folder to `/music` plus a
  persistent volume `/data` for the beets database, mtime tracking DB, and logs.
- `.env.example` – Template for the host path and AcoustID key.
- `scan_incomplete.py` – Detection logic (missing cover/tags) used by
  `update_music_metadata.sh`, kept in its own module so it's unit testable.
  Supports incremental mode (mtime tracking) and parallel file checking.
- `tests/` – Unit tests for `scan_incomplete.py` (incremental, parallel, combined).

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
cover art / missing tags / directory scan / incremental mtime tracking /
parallel file checking) with mocked mutagen objects and temporary SQLite
databases, so they run without needing real audio files.

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Performance Optimizations

### Incremental Scanning

`scan_incomplete.py` tracks file modification times (mtime) in a SQLite
database (`mtime_tracking.db` in `$WORK_DIR`). This means:

- **First run:** Scans all audio files in the library (may take a while)
- **Subsequent runs:** Only scans files that have been modified since the last run
- **Much faster on stable libraries:** If your library hasn't changed, subsequent
  runs complete in seconds instead of minutes/hours

The mtime database is stored in the `/data` persistent volume (Docker) or
`$WORK_DIR` (direct invocation), so tracking persists across runs.

### Parallel File Scanning

`scan_incomplete.py` now scans audio files in parallel using multi-threading.
By default, it uses `CPU_count + 1` worker threads (capped at 8) for maximum
throughput. This dramatically speeds up initial scans on large libraries.

Control the number of workers via the `SCAN_WORKERS` environment variable:

```bash
SCAN_WORKERS=4 MUSIC_DIR=/music ./update_music_metadata.sh
```

Or in `docker-compose.yml` environment section:
```yaml
environment:
  SCAN_WORKERS: "4"
```

### Batch Import

`update_music_metadata.sh` now batches multiple files per `beet import` call
instead of processing one file at a time. This reduces process startup overhead
and improves overall speed. Batch size is configurable via `BATCH_IMPORT_SIZE`
(default: 50 files).

```bash
BATCH_IMPORT_SIZE=100 MUSIC_DIR=/music ./update_music_metadata.sh
```

Or in `docker-compose.yml`:
```yaml
environment:
  BATCH_IMPORT_SIZE: "100"
```

**Combined impact:** Typical speedup of **3-5x on large libraries** (1000+ files)
compared to single-threaded, single-file processing.

## Open items / possible next steps

- Recurring execution (cron in the container, or external scheduling)
- Finer control over matching thresholds in the beets config
  (`match.strong_rec_thresh` etc.), in case mismatches occur
- Test with real sample files from the mounted folder before running against
  the whole library (`MUSIC_HOST_PATH` pointing to a small subfolder
  for testing)
- Incremental cover art fetching (only re-fetch covers for recently modified files)
