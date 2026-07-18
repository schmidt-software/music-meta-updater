#!/usr/bin/env python3
"""Scans a music folder for audio files missing cover art or basic tags.

Used by update_music_metadata.sh; kept as a standalone module so the
detection logic can be unit tested independently of the shell script.

Supports:
- Incremental scanning via mtime tracking: only rescans files modified since last run
- Parallel file checking: uses ThreadPoolExecutor for fast multi-core scanning
- Resilient error handling: exponential backoff + blacklist for flaky network mounts
- Incremental cover processing: only re-fetches covers for modified files
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
# Exponential backoff: max retries and base delay (seconds)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.5
# Blacklist: how long to skip permanently-failing files (seconds, default 30min)
BLACKLIST_DURATION = 30 * 60


def init_error_db(db_path):
    """Initialize or open the error tracking + blacklist database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS error_tracking (
            filepath TEXT PRIMARY KEY,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error_time REAL NOT NULL,
            blacklist_until REAL
        )
    """)
    conn.commit()
    return conn


def init_cover_db(db_path):
    """Initialize or open the cover processing tracking database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cover_tracking (
            filepath TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            last_cover_processed REAL NOT NULL,
            cover_status TEXT
        )
    """)
    conn.commit()
    return conn


def is_blacklisted(conn, filepath):
    """Check if file is blacklisted (temporarily or permanently)."""
    cursor = conn.execute(
        "SELECT blacklist_until FROM error_tracking WHERE filepath = ?",
        (filepath,)
    )
    row = cursor.fetchone()
    if row and row[0]:
        if time.time() < row[0]:
            return True  # Still blacklisted
        else:
            # Blacklist expired, remove it
            conn.execute("DELETE FROM error_tracking WHERE filepath = ?", (filepath,))
            conn.commit()
    return False


def record_error(conn, filepath, error_type, error_message, blacklist_duration=BLACKLIST_DURATION):
    """Record an error for a file and optionally blacklist it."""
    now = time.time()
    blacklist_until = now + blacklist_duration
    
    cursor = conn.execute(
        "SELECT error_count FROM error_tracking WHERE filepath = ?",
        (filepath,)
    )
    row = cursor.fetchone()
    error_count = (row[0] if row else 0) + 1
    
    conn.execute(
        """INSERT OR REPLACE INTO error_tracking 
           (filepath, error_type, error_message, error_count, last_error_time, blacklist_until)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (filepath, error_type, error_message, error_count, now, blacklist_until)
    )
    conn.commit()


def get_error_type(exception):
    """Classify exception into error type for telemetry."""
    if isinstance(exception, PermissionError):
        return "permission_error"
    elif isinstance(exception, FileNotFoundError):
        return "file_not_found"
    elif isinstance(exception, OSError):
        return "io_error"
    elif isinstance(exception, TimeoutError):
        return "timeout"
    else:
        return "unknown_error"


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


def get_cover_mtime(conn, filepath):
    """Get the stored mtime for a file's cover processing, or None if not tracked."""
    cursor = conn.execute(
        "SELECT mtime FROM cover_tracking WHERE filepath = ?",
        (filepath,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def update_cover_tracking(conn, filepath, mtime, cover_status=None):
    """Update or insert the cover processing tracking record."""
    now = time.time()
    conn.execute(
        """INSERT OR REPLACE INTO cover_tracking 
           (filepath, mtime, last_cover_processed, cover_status)
           VALUES (?, ?, ?, ?)""",
        (filepath, mtime, now, cover_status)
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


def _check_file_with_retry(path, mtime_db_path=None, error_db_path=None):
    """Check a single file with exponential backoff retry logic.
    
    Returns (path, is_incomplete, should_track_mtime, error_info)
    error_info is (error_type, error_message) or None if no error.
    """
    error_db_conn = None
    if error_db_path:
        error_db_conn = init_error_db(error_db_path)
        # Check blacklist first
        if is_blacklisted(error_db_conn, path):
            return (path, False, False, None)

    # Incremental check: skip if mtime hasn't changed
    if mtime_db_path:
        try:
            current_mtime = os.path.getmtime(path)
            conn = init_mtime_db(mtime_db_path)
            tracked_mtime = get_tracked_mtime(conn, path)
            conn.close()
            if tracked_mtime is not None and current_mtime == tracked_mtime:
                # File hasn't changed; skip it
                return (path, False, False, None)
        except OSError as e:
            error_type = get_error_type(e)
            error_msg = str(e)
            if error_db_conn:
                record_error(error_db_conn, path, error_type, error_msg)
                error_db_conn.close()
            print(f"WARN: could not stat {path} ({error_type}: {error_msg})", file=sys.stderr)
            return (path, False, False, (error_type, error_msg))

    # Try to read and check the file with exponential backoff
    last_exception = None
    for attempt in range(MAX_RETRIES):
        try:
            mf = MutagenFile(path)
            if mf is None:
                if error_db_conn:
                    error_db_conn.close()
                return (path, False, True, None)
            
            missing_cover = not has_cover(mf)
            missing_tags = not has_basic_tags(mf)
            is_incomplete = missing_cover or missing_tags
            
            if error_db_conn:
                error_db_conn.close()
            return (path, is_incomplete, True, None)
        except Exception as e:
            last_exception = e
            if attempt < MAX_RETRIES - 1:
                # Exponential backoff before retry
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
            else:
                # Final attempt failed, record error
                error_type = get_error_type(e)
                error_msg = str(e)
                if error_db_conn:
                    record_error(error_db_conn, path, error_type, error_msg)
                    error_db_conn.close()
                print(f"WARN: could not read {path} after {MAX_RETRIES} attempts ({error_type}: {error_msg})", file=sys.stderr)
                return (path, False, True, (error_type, error_msg))

    # Shouldn't reach here, but handle just in case
    if error_db_conn:
        error_db_conn.close()
    return (path, False, True, None)


def find_incomplete(music_dir, mtime_db_path=None, error_db_path=None, num_workers=None):
    """Walks music_dir and returns (total_checked, [incomplete_paths], error_telemetry).
    
    If mtime_db_path is provided, only scans files that have been modified
    since last processing (incremental mode). Files are tracked in a SQLite DB.
    
    If error_db_path is provided, tracks I/O errors with exponential backoff
    and blacklist for flaky network mounts.
    
    Uses parallel file checking with ThreadPoolExecutor (num_workers threads).
    If num_workers is None, defaults to CPU count + 1, capped at 8.
    
    error_telemetry is a dict with error_type -> count mapping.
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
    error_telemetry = {}

    # Second pass: check files in parallel
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(_check_file_with_retry, path, mtime_db_path, error_db_path): path
            for path in files_to_check
        }

        # Batch update mtime tracking (once per batch to avoid thread contention)
        mtime_updates = {}

        for future in as_completed(futures):
            try:
                path, is_incomplete, should_track, error_info = future.result()
                if is_incomplete:
                    incomplete.append(path)
                
                if error_info:
                    error_type, _ = error_info
                    error_telemetry[error_type] = error_telemetry.get(error_type, 0) + 1

                # Queue mtime update (batch later)
                if should_track and mtime_db_path:
                    try:
                        mtime = os.path.getmtime(path)
                        mtime_updates[path] = mtime
                    except OSError:
                        pass
            except Exception as e:
                print(f"WARN: unexpected error checking file ({e})", file=sys.stderr)
                error_telemetry["unexpected_error"] = error_telemetry.get("unexpected_error", 0) + 1

    # Batch write mtime tracking (after all workers finish)
    if mtime_updates and mtime_db_path:
        try:
            conn = init_mtime_db(mtime_db_path)
            for path, mtime in mtime_updates.items():
                update_mtime_tracking(conn, path, mtime)
            conn.close()
        except Exception as e:
            print(f"WARN: could not update mtime tracking ({e})", file=sys.stderr)

    return total, incomplete, error_telemetry


def find_files_needing_cover_update(music_dir, cover_db_path, num_workers=None):
    """Find files that need cover art updates (mtime changed since last cover processing).
    
    Returns [filepath, ...] of files whose cover art should be re-fetched.
    """
    if not cover_db_path:
        # No cover tracking DB, process all files
        files_to_check = []
        for root, _dirs, files in os.walk(music_dir, onerror=_on_walk_error):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in AUDIO_EXTS:
                    continue
                path = os.path.join(root, fname)
                files_to_check.append(path)
        return files_to_check

    conn = init_cover_db(cover_db_path)
    cover_updates = []
    
    for root, _dirs, files in os.walk(music_dir, onerror=_on_walk_error):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            path = os.path.join(root, fname)
            try:
                current_mtime = os.path.getmtime(path)
                tracked_cover_mtime = get_cover_mtime(conn, path)
                # If file hasn't been tracked or mtime changed, update cover
                if tracked_cover_mtime is None or current_mtime != tracked_cover_mtime:
                    cover_updates.append(path)
            except OSError:
                pass
    
    conn.close()
    return cover_updates


def mark_cover_processed(cover_db_path, filepath, cover_status=None):
    """Mark a file as having been processed for cover art."""
    if not cover_db_path:
        return
    conn = init_cover_db(cover_db_path)
    try:
        mtime = os.path.getmtime(filepath)
        update_cover_tracking(conn, filepath, mtime, cover_status)
    except OSError:
        pass
    finally:
        conn.close()


def main():
    music_dir = sys.argv[1]
    out_file = sys.argv[2]
    # Optional third argument: path to mtime tracking database
    mtime_db = sys.argv[3] if len(sys.argv) > 3 else None
    # Optional fourth argument: path to error tracking database
    error_db = sys.argv[4] if len(sys.argv) > 4 else None
    # Optional fifth argument: number of worker threads
    num_workers = None
    if len(sys.argv) > 5:
        try:
            num_workers = int(sys.argv[5])
        except ValueError:
            pass

    total, incomplete, error_telemetry = find_incomplete(music_dir, mtime_db, error_db, num_workers)
    with open(out_file, "w") as f:
        for p in incomplete:
            f.write(p + "\n")
    
    # Print summary
    print(f"{total} audio files checked, {len(incomplete)} incomplete.")
    if error_telemetry:
        error_summary = ", ".join(f"{count} {etype}" for etype, count in sorted(error_telemetry.items()))
        print(f"Errors encountered: {error_summary}", file=sys.stderr)


if __name__ == "__main__":
    main()


def init_failed_matches_db(db_path):
    """Initialize or open the failed matches tracking database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed_matches (
            filepath TEXT PRIMARY KEY,
            error_reason TEXT NOT NULL,
            match_attempts INTEGER NOT NULL DEFAULT 1,
            last_attempt_time REAL NOT NULL,
            fallback_artist TEXT,
            fallback_album TEXT
        )
    """)
    conn.commit()
    return conn


def record_failed_match(conn, filepath, error_reason, fallback_artist=None, fallback_album=None):
    """Record a failed match attempt with optional fallback metadata."""
    now = time.time()
    cursor = conn.execute(
        "SELECT match_attempts FROM failed_matches WHERE filepath = ?",
        (filepath,)
    )
    row = cursor.fetchone()
    attempts = (row[0] if row else 0) + 1
    
    conn.execute(
        """INSERT OR REPLACE INTO failed_matches
           (filepath, error_reason, match_attempts, last_attempt_time, fallback_artist, fallback_album)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (filepath, error_reason, attempts, now, fallback_artist, fallback_album)
    )
    conn.commit()


def get_failed_match(conn, filepath):
    """Get failed match info for a file."""
    cursor = conn.execute(
        """SELECT error_reason, match_attempts, fallback_artist, fallback_album 
           FROM failed_matches WHERE filepath = ?""",
        (filepath,)
    )
    return cursor.fetchone()
