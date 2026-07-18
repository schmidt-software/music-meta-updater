#!/usr/bin/env python3
"""Scans a music folder for audio files missing cover art or basic tags.

Used by update_music_metadata.sh; kept as a standalone module so the
detection logic can be unit tested independently of the shell script.

Supports incremental scanning via mtime tracking: only rescans files that
have been modified since last scan.
"""
import sys
import os
import sqlite3
import time
from mutagen import File as MutagenFile

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aac"}


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


def find_incomplete(music_dir, mtime_db_path=None):
    """Walks music_dir and returns (total_checked, [incomplete_paths]).
    
    If mtime_db_path is provided, only scans files that have been modified
    since last processing (incremental mode). Files are tracked in a SQLite DB.
    """
    conn = None
    if mtime_db_path:
        conn = init_mtime_db(mtime_db_path)

    incomplete = []
    total = 0
    for root, _dirs, files in os.walk(music_dir, onerror=_on_walk_error):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            total += 1
            path = os.path.join(root, fname)

            # If incremental mode: skip files that haven't been modified
            if conn:
                try:
                    current_mtime = os.path.getmtime(path)
                    tracked_mtime = get_tracked_mtime(conn, path)
                    if tracked_mtime is not None and current_mtime == tracked_mtime:
                        # File hasn't changed since last scan; skip it
                        continue
                except OSError as e:
                    print(f"WARN: could not stat {path} ({e})", file=sys.stderr)
                    continue

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

            # Update mtime tracking after checking the file
            if conn:
                try:
                    mtime = os.path.getmtime(path)
                    update_mtime_tracking(conn, path, mtime)
                except OSError:
                    pass

    if conn:
        conn.close()

    return total, incomplete


def main():
    music_dir = sys.argv[1]
    out_file = sys.argv[2]
    # Optional third argument: path to mtime tracking database
    mtime_db = sys.argv[3] if len(sys.argv) > 3 else None

    total, incomplete = find_incomplete(music_dir, mtime_db)
    with open(out_file, "w") as f:
        for p in incomplete:
            f.write(p + "\n")
    print(f"{total} audio files checked, {len(incomplete)} incomplete.")


if __name__ == "__main__":
    main()
