# Music Metadata Updater

Recursively scans a (e.g. network-mounted) music folder and automatically
updates any files missing cover art or metadata from the internet
(MusicBrainz + Cover Art Archive via `beets`). Runs fully
non-interactively.

## Files

- `update_music_metadata.sh` – Main script. Scans all audio files with
  Python/mutagen for missing tags (title/artist/album) or missing
  cover art, and only lets `beets` automatically tag/cover those files.
  Leaves already-complete files untouched and doesn't move/rename
  anything (existing folder structure is preserved).
- `Dockerfile` – Image with all dependencies (python3, chromaprint/fpcalc,
  ffmpeg, beets, mutagen, pyacoustid).
- `docker-compose.yml` – Mounts the music folder to `/music` plus a
  persistent volume `/data` for the beets database and logs.
- `.env.example` – Template for the host path and AcoustID key.
- `scan_incomplete.py` – Detection logic (missing cover/tags) used by
  `update_music_metadata.sh`, kept in its own module so it's unit testable.
- `tests/` – Unit tests for `scan_incomplete.py`.

## Setup

```bash
cp .env.example .env
# edit .env: MUSIC_HOST_PATH=<mounted music folder path>, ACOUSTID_API_KEY=<key>
docker compose up --build
```

`ACOUSTID_API_KEY` is optional but recommended (free at
acoustid.org) – without a key, files with completely missing tags can
only be guessed from the filename, which is much less reliable.

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

## Tests

Unit tests cover the detection logic in `scan_incomplete.py` (missing
cover art / missing tags / directory scan) with mocked mutagen objects,
so they run without needing real audio files.

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Open items / possible next steps

- Recurring execution (cron in the container, or external scheduling)
- Finer control over matching thresholds in the beets config
  (`match.strong_rec_thresh` etc.), in case mismatches occur
- More precise error handling/reporting (currently just a log file at
  `/data/update.log` and `/data/beets_import.log`)
- Test with real sample files from the mounted folder before running against
  the whole library (`MUSIC_HOST_PATH` pointing to a small subfolder
  for testing)
