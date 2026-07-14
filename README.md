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

## Setup

```bash
cp .env.example .env
# edit .env: MUSIC_HOST_PATH=<mounted music folder path>, ACOUSTID_API_KEY=<key>
docker compose up --build
```

`ACOUSTID_API_KEY` is optional but recommended (free at
acoustid.org) – without a key, files with completely missing tags can
only be guessed from the filename, which is much less reliable.

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

## Open items / possible next steps

- Recurring execution (cron in the container, or external scheduling)
- Finer control over matching thresholds in the beets config
  (`match.strong_rec_thresh` etc.), in case mismatches occur
- More precise error handling/reporting (currently just a log file at
  `/data/update.log` and `/data/beets_import.log`)
- Test with real sample files from the mounted folder before running against
  the whole library (`MUSIC_HOST_PATH` pointing to a small subfolder
  for testing)
