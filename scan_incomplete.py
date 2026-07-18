#!/usr/bin/env python3
"""Scans a music folder for audio files missing cover art or basic tags.

Used by update_music_metadata.sh; kept as a standalone module so the
detection logic can be unit tested independently of the shell script.

Supports:
- Incremental scanning via mtime tracking: only rescans files modified since last run
- Parallel file checking: uses ThreadPoolExecutor for fast multi-core scanning
"""
import sys
import os
import sqlite3
import time
from mutagen import File as MutagenFile
from concurrent.futures import ThreadPoolExecutor, as_completed

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aac"}
# Default worker threads for parallel file checking (can be overridden)
DEFAULT_WORKERS = min(8, (os.cpu_count() or 1) + 1)


def init_mtime_db(db_path):
    """Initialize or open the mtime tracking database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_mtime_tracking (
            filepath TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            last_processed REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_tracked_mtime(conn, filepath):
    """Get the stored mtime for a file, or None if not tracked."""
    cursor = conn.execute(
        "SELECT mtime FROM file_mtime_tracking WHERE filepath = ?",
        (filepath,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def update_mtime_tracking(conn, filepath, mtime):
    """Update or insert the mtime tracking record."""
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO file_mtime_tracking (filepath, mtime, last_processed) VALUES (?, ?, ?)",
        (filepath, mtime, now)
    )
    conn.commit()


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


def _on_walk_error(err):
    # Network mounts (NFS/SFTP/S3 etc.) can throw transient errors while
    # listing a directory; report them instead of silently skipping the
    # subtree (os.walk's default behavior).
    print(f"WARN: could not list directory ({err})", file=sys.stderr)


def _check_file(path, mtime_db_path=None):
    """Check a single file for completeness. Returns (path, is_incomplete, should_track_mtime)
    
    Should be called in a worker thread. Opens its own DB connection if
    mtime_db_path is provided (sqlite3 connections are NOT thread-safe).
    """
    # Incremental check: skip if mtime hasn't changed
    if mtime_db_path:
        try:
            current_mtime = os.path.getmtime(path)
            conn = init_mtime_db(mtime_db_path)
            tracked_mtime = get_tracked_mtime(conn, path)
            conn.close()
            if tracked_mtime is not None and current_mtime == tracked_mtime:
                # File hasn't changed; skip it
                return (path, False, False)  # not incomplete, don't track
        except OSError as e:
            print(f"WARN: could not stat {path} ({e})", file=sys.stderr)
            return (path, False, False)

    # Try to read and check the file
    try:
        mf = MutagenFile(path)
    except Exception as e:
        print(f"WARN: could not read {path} ({e})", file=sys.stderr)
        return (path, False, True)  # skip but still update mtime tracking

    if mf is None:
        print(f"WARN: unknown/corrupt format: {path}", file=sys.stderr)
        return (path, False, True)

    missing_cover = not has_cover(mf)
    missing_tags = not has_basic_tags(mf)
    is_incomplete = missing_cover or missing_tags

    return (path, is_incomplete, True)


def find_incomplete(music_dir, mtime_db_path=None, num_workers=None):
    """Walks music_dir and returns (total_checked, [incomplete_paths]).
    
    If mtime_db_path is provided, only scans files that have been modified
    since last processing (incremental mode). Files are tracked in a SQLite DB.
    
    Uses parallel file checking with ThreadPoolExecutor (num_workers threads).
    If num_workers is None, defaults to CPU count + 1, capped at 8.
    """
    if num_workers is None:
        num_workers = DEFAULT_WORKERS

    # First pass: collect all audio files to check
    files_to_check = []
    for root, _dirs, files in os.walk(music_dir, onerror=_on_walk_error):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            path = os.path.join(root, fname)
            files_to_check.append(path)

    total = len(files_to_check)
    incomplete = []

    # Second pass: check files in parallel
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(_check_file, path, mtime_db_path): path
            for path in files_to_check
        }

        # Batch update mtime tracking (once per batch to avoid thread contention)
        mtime_updates = {}

        for future in as_completed(futures):
            try:
                path, is_incomplete, should_track = future.result()
                if is_incomplete:
                    incomplete.append(path)

                # Queue mtime update (batch later)
                if should_track and mtime_db_path:
                    try:
                        mtime = os.path.getmtime(path)
                        mtime_updates[path] = mtime
                    except OSError:
                        pass
            except Exception as e:
                print(f"WARN: unexpected error checking file ({e})", file=sys.stderr)

    # Batch write mtime tracking (after all workers finish)
    if mtime_updates and mtime_db_path:
        try:
            conn = init_mtime_db(mtime_db_path)
            for path, mtime in mtime_updates.items():
                update_mtime_tracking(conn, path, mtime)
            conn.close()
        except Exception as e:
            print(f"WARN: could not update mtime tracking ({e})", file=sys.stderr)

    return total, incomplete


def main():
    music_dir = sys.argv[1]
    out_file = sys.argv[2]
    # Optional third argument: path to mtime tracking database
    mtime_db = sys.argv[3] if len(sys.argv) > 3 else None
    # Optional fourth argument: number of worker threads
    num_workers = None
    if len(sys.argv) > 4:
        try:
            num_workers = int(sys.argv[4])
        except ValueError:
            pass

    total, incomplete = find_incomplete(music_dir, mtime_db, num_workers)
    with open(out_file, "w") as f:
        for p in incomplete:
            f.write(p + "\n")
    print(f"{total} audio files checked, {len(incomplete)} incomplete.")


if __name__ == "__main__":
    main()
