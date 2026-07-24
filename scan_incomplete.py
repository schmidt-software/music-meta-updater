#!/usr/bin/env python3
"""Scans a music folder for audio files missing cover art or basic tags and
updates each one via beets as soon as it's found (no waiting for the whole
scan to finish first).

Used by update_music_metadata.sh; kept as a standalone module so the
detection logic can be unit tested independently of the shell script.
"""
import sys
import os
import time
import threading
import queue
import subprocess
import concurrent.futures
import sqlite3
try:
    from mutagen import File as MutagenFile
except Exception:
    # Tests may run without mutagen installed; provide a noop placeholder
    def MutagenFile(path, easy=False):
        return None

import metadata_fallback

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aac"}

DEFAULT_SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "8"))
MTIME_TOLERANCE = float(os.environ.get("MTIME_TOLERANCE", "1.0"))  # seconds tolerance for mtime comparison on coarse filesystems


def has_cover(mf):
    try:
        # FLAC stores pictures as their own metadata blocks, independent of
        # (and possibly present even without) a VORBIS_COMMENT tags block -
        # a FLAC with art but zero text tags has mf.tags is None, so this
        # must not be nested inside the tags check below.
        if hasattr(mf, "pictures") and mf.pictures:
            return True
        if hasattr(mf, "tags") and mf.tags is not None:
            # ID3 (mp3, wav, aiff)
            if any(k.startswith("APIC") for k in mf.tags.keys()):
                return True
            # MP4/M4A
            if "covr" in mf.tags:
                return True
            # ASF/WMA
            if "WM/Picture" in mf.tags:
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


def _heartbeat(state, stop_event, start_time, interval=5.0):
    """Prints a status line every interval seconds, from a background thread
    independent of the scanning/updating loops.

    A single os.walk() step, MutagenFile() open, or beet subprocess call can
    block for a long time on a slow/flaky network mount (NFS/SFTP/S3/SMB) -
    whichever thread is in that blocking call simply isn't running any
    Python code, so nothing printed from inside it can appear until it
    returns. Python releases the GIL during blocking I/O though, so this
    thread keeps ticking regardless and proves the process is still alive.
    """
    while not stop_event.wait(interval):
        elapsed = time.monotonic() - start_time
        print(f"Scanning: {state['checked']} files checked so far "
              f"({state['incomplete']} incomplete found, {state['updated']} updated, "
              f"{state['failed']} failed, {elapsed:.0f}s elapsed)", flush=True)


def _check_file(path):
    """Returns ("warn", message), ("incomplete", None) or ("ok", None)."""
    try:
        mf = MutagenFile(path)
    except Exception as e:
        return "warn", f"WARN: could not read {path} ({e})"
    if mf is None:
        return "warn", f"WARN: unknown/corrupt format: {path}"
    if not has_cover(mf) or not has_basic_tags(mf):
        return "incomplete", None
    return "ok", None


def _apply_fallback_tags(file_path, metadata):
    """Apply artist/album tags into the file using Mutagen. Returns True on success.

    After saving tags, update the file mtime so external tools (beets)
    are more likely to notice the change without needing a full rescan.
    Use format-specific Mutagen save APIs when available.
    """
    try:
        ext = os.path.splitext(file_path)[1].lower()
    except Exception:
        ext = ''

    # Try format-specific handlers first
    try:
        if ext == '.flac':
            try:
                import importlib
                mod = importlib.import_module('mutagen.flac')
                FLAC = getattr(mod, 'FLAC')
                fl = FLAC(file_path)
                if 'artist' in metadata and metadata['artist']:
                    fl['artist'] = [metadata['artist']]
                if 'album' in metadata and metadata['album']:
                    fl['album'] = [metadata['album']]
                fl.save()
                try:
                    os.utime(file_path, None)
                except Exception:
                    pass
                print(f"Fallback tags applied (FLAC): {file_path}", flush=True)
                return True
            except Exception:
                # Fall through to generic path
                pass

        if ext in ('.m4a', '.mp4'):
            try:
                import importlib
                mod = importlib.import_module('mutagen.mp4')
                MP4 = getattr(mod, 'MP4')
                mp4 = MP4(file_path)
                if 'artist' in metadata and metadata['artist']:
                    mp4['\xa9ART'] = [metadata['artist']]
                if 'album' in metadata and metadata['album']:
                    mp4['\xa9alb'] = [metadata['album']]
                mp4.save()
                try:
                    os.utime(file_path, None)
                except Exception:
                    pass
                print(f"Fallback tags applied (MP4/M4A): {file_path}", flush=True)
                return True
            except Exception:
                # Fall through to generic path
                pass
    except Exception:
        # Ignore format-specific import errors; try generic approach
        pass

    # Generic path using MutagenFile / Easy APIs
    try:
        mf = MutagenFile(file_path, easy=True)
    except Exception:
        mf = None
    if mf is None:
        return False
    try:
        # Mutagen Easy* interfaces accept lists for values
        if 'artist' in metadata and metadata['artist']:
            mf['artist'] = [metadata['artist']]
        if 'album' in metadata and metadata['album']:
            mf['album'] = [metadata['album']]

        # Try multiple save strategies for different file types
        saved = False
        try:
            save = getattr(mf, 'save', None)
            if callable(save):
                save()
                saved = True
        except Exception:
            saved = False

        if not saved:
            try:
                if hasattr(mf, 'tags') and mf.tags is not None and hasattr(mf.tags, 'save'):
                    mf.tags.save()
                    saved = True
            except Exception:
                saved = False

        # As a last-ditch: re-open as easy and write back tags via Easy* API
        if not saved:
            try:
                easy = MutagenFile(file_path, easy=True)
                if easy is not None:
                    if 'artist' in metadata and metadata['artist']:
                        easy['artist'] = [metadata['artist']]
                    if 'album' in metadata and metadata['album']:
                        easy['album'] = [metadata['album']]
                    save2 = getattr(easy, 'save', None)
                    if callable(save2):
                        save2()
                        saved = True
            except Exception:
                saved = False

        if not saved:
            print(f"WARN: could not save tags with Mutagen for {file_path}", file=sys.stderr, flush=True)
            return False

        # Touch file mtime so other tools notice change
        try:
            os.utime(file_path, None)
        except Exception:
            # Not critical; log and continue
            print(f"WARN: could not update mtime for {file_path}", file=sys.stderr, flush=True)

        print(f"Fallback tags applied: {file_path}", flush=True)
        return True
    except Exception as e:
        print(f"WARN: could not apply fallback tags to {file_path} ({e})", file=sys.stderr, flush=True)
        return False


def _update_file(file_path, beets_config_path, run=subprocess.run, library_root=None):
    """Tags file_path via beets, then fetches and embeds cover art for just
    that item (scoped via a "path:" query). Runs from a single dedicated
    worker (see _updater_worker) since beets' sqlite library isn't safe for
    concurrent writes from multiple beet processes at once.
    """
    print(f"Fetching metadata for: {file_path}", flush=True)
    result = run(["beet", "-v", "-c", beets_config_path, "import", "-q", "-s", file_path])
    if result.returncode != 0:
        print(f"WARN: Could not automatically tag '{file_path}' (no confident match found).",
              file=sys.stderr, flush=True)
        # Attempt fallback metadata extraction and application if enabled
        fallback_apply = os.environ.get('FALLBACK_APPLY', 'true').lower() not in ('0', 'false', 'no')
        if fallback_apply:
            library_root = os.environ.get('MUSIC_DIR')
            try:
                fb = metadata_fallback.extract_from_path(file_path, library_root=library_root)
            except Exception as e:
                fb = {}
                print(f"WARN: fallback extraction failed for {file_path} ({e})", file=sys.stderr)
            if fb:
                # Apply normalized values (metadata_fallback already normalizes)
                applied = _apply_fallback_tags(file_path, fb)
                if applied:
                    # Controlled rescan: opt-in via FALLBACK_BEETS_RESCAN (default: false)
                    rescan = os.environ.get('FALLBACK_BEETS_RESCAN', 'false').lower() in ('1', 'true', 'yes')
                    if rescan:
                        result2 = run(["beet", "-v", "-c", beets_config_path, "import", "-q", "-s", file_path])
                        if result2.returncode == 0:
                            print(f"Metadata written after fallback: {file_path}", flush=True)
                            query = f"path:{file_path}"
                            run(["beet", "-v", "-c", beets_config_path, "fetchart", "-q", query])
                            run(["beet", "-v", "-c", beets_config_path, "embedart", "-y", query])
                            print(f"Cover art updated: {file_path}", flush=True)
                            return True
                        else:
                            print(f"WARN: beets still could not import {file_path} after fallback.", file=sys.stderr, flush=True)
                            # Return False so future runs can retry the import
                            return False
                    else:
                        # Rescan disabled: treat as successfully handled (tags applied)
                        print(f"Fallback tags applied (rescan disabled): {file_path}", flush=True)
                        return True
                else:
                    print(f"WARN: fallback extraction produced values but applying tags failed for {file_path}", file=sys.stderr, flush=True)
        return False
    print(f"Metadata written: {file_path}", flush=True)

    query = f"path:{file_path}"
    run(["beet", "-v", "-c", beets_config_path, "fetchart", "-q", query])
    run(["beet", "-v", "-c", beets_config_path, "embedart", "-y", query])
    print(f"Cover art updated: {file_path}", flush=True)
    return True


def _updater_worker(work_queue, beets_config_path, state, update_fn=None, mtime_conn=None, db_lock=None, music_dir=None, failed_conn=None, failed_db_lock=None):
    """Consumes (path, mtime, size) tuples from work_queue one at a time until it
    sees the None sentinel. If mtime_conn is provided, successful updates
    are recorded to the mtime DB. Updates state for the heartbeat to report.

    music_dir is passed to update_fn as library_root so fallback extraction
    can be scoped relative to the library root, rather than reading MUSIC_DIR
    from the environment.
    """

    """Consumes (path, mtime, size) tuples from work_queue one at a time until it
    sees the None sentinel. If mtime_conn is provided, successful updates
    are recorded to the mtime DB. Updates state for the heartbeat to report."""
    if update_fn is None:
        update_fn = _update_file
    while True:
        item = work_queue.get()
        if item is None:
            work_queue.task_done()
            return
        try:
            file_path, file_mtime, file_size = item
        except Exception:
            # Backwards-compat: if older callers placed just a path, accept it
            file_path = item
            try:
                file_mtime = os.path.getmtime(file_path)
            except Exception:
                file_mtime = None
            try:
                file_size = os.path.getsize(file_path)
            except Exception:
                file_size = None

        # Pass library_root to update_fn so fallback extraction can be scoped
        try:
            success = update_fn(file_path, beets_config_path, library_root=music_dir)
        except TypeError:
            # Fallback: call without library_root if update_fn doesn't accept it
            success = update_fn(file_path, beets_config_path)
        if success:
            state["updated"] += 1
            # Record tracked mtime and size only on confirmed success
            if mtime_conn and file_mtime is not None and file_size is not None:
                try:
                    update_mtime_tracking(mtime_conn, file_path, file_mtime, file_size)
                except sqlite3.Error as e:
                    print(f"WARN: could not update mtime DB for {file_path} ({e})", file=sys.stderr)
        else:
            state["failed"] += 1
            # If configured, record the failed match into the failed_matches DB
            if failed_conn:
                try:
                    reason = "no_confident_match"
                    if failed_db_lock:
                        with failed_db_lock:
                            record_failed_match(failed_conn, file_path, reason)
                    else:
                        record_failed_match(failed_conn, file_path, reason)
                except Exception as e:
                    print(f"WARN: could not record failed match for {file_path} ({e})", file=sys.stderr)
        work_queue.task_done()


def _handle_check_result(future, path_info, incomplete, work_queue, checked, state):
    kind, msg = future.result()
    # path_info is (path, current_mtime, current_size)
    path, file_mtime, file_size = path_info
    if kind == "warn":
        print(msg, file=sys.stderr, flush=True)
    elif kind == "incomplete":
        incomplete.append(path)
        # enqueue tuple(path, mtime, size) so updater can record the tracked values
        work_queue.put((path, file_mtime, file_size))
    checked += 1
    state["checked"] = checked
    state["incomplete"] = len(incomplete)
    return checked


# Generic KV-table helpers to reduce duplication across different tracking tables

def _init_kv_db(conn, table_name, extra_columns_sql):
    """Initialize a KV SQLite table with a filepath primary key and extra columns.

    extra_columns_sql should be a comma-separated SQL fragment like "mtime REAL NOT NULL, size INTEGER NOT NULL".
    """
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE IF NOT EXISTS {table_name} (filepath TEXT PRIMARY KEY, {extra_columns_sql})")


def _get_kv_row(conn, table_name, filepath, columns):
    """Return a tuple of columns from table_name for filepath, or None."""
    cur = conn.cursor()
    cols_sql = ", ".join(columns)
    cur.execute(f"SELECT {cols_sql} FROM {table_name} WHERE filepath = ?", (filepath,))
    row = cur.fetchone()
    return tuple(row) if row else None


def _set_kv_row(conn, table_name, filepath, columns_values):
    """Insert or replace a KV row. columns_values is an ordered dict-like sequence of (col, val)."""
    cur = conn.cursor()
    cols = [c for c, _ in columns_values]
    vals = [v for _, v in columns_values]
    placeholders = ", ".join(["?" for _ in vals])
    cols_sql = ", ".join(cols)
    cur.execute(f"INSERT OR REPLACE INTO {table_name} (filepath, {cols_sql}) VALUES (?, {placeholders})", (filepath, *vals))


def _prune_kv_missing(conn, table_name):
    """Delete rows in table_name where filepath no longer exists on disk."""
    cur = conn.cursor()
    cur.execute(f"SELECT filepath FROM {table_name}")
    rows = cur.fetchall()
    to_delete = []
    for (fp,) in rows:
        if not os.path.exists(fp):
            to_delete.append(fp)
    if to_delete:
        cur.executemany(f"DELETE FROM {table_name} WHERE filepath = ?", [(fp,) for fp in to_delete])


def init_mtime_db(db_path):
    """Create/open the mtime tracking DB and ensure schema exists. Returns sqlite3.Connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    _init_kv_db(conn, "file_mtime_tracking", "mtime REAL NOT NULL, size INTEGER NOT NULL")
    conn.commit()
    return conn


def get_tracked_mtime(conn, filepath):
    """Return (mtime, size) tuple or None. May raise sqlite3.Error."""
    return _get_kv_row(conn, "file_mtime_tracking", filepath, ["mtime", "size"])


def update_mtime_tracking(conn, filepath, mtime, size):
    """Insert or replace tracked mtime and size for filepath. May raise sqlite3.Error.

    NOTE: this function does NOT call conn.commit() to allow batching of
    multiple updates. Call commit_mtime_db(conn) after a batch completes.
    """
    _set_kv_row(conn, "file_mtime_tracking", filepath, [("mtime", mtime), ("size", size)])


def commit_mtime_db(conn):
    """Commit pending mtime DB changes. Safe to call if no changes were made."""
    conn.commit()


def prune_stale_mtime_rows(conn):
    """Remove tracking rows whose files no longer exist on disk."""
    _prune_kv_missing(conn, "file_mtime_tracking")


# ----------------------------- Cover tracking -----------------------------
# Implement a small cover-tracking layer using the generic KV helpers so that
# a future incremental cover-fetch phase can avoid re-fetching covers that
# were already successfully processed. The functions are intentionally
# lightweight and batch-aware (no per-file commit).


def init_cover_db(db_path):
    """Open or create the cover tracking DB and ensure schema exists."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    _init_kv_db(conn, "cover_tracking", "mtime REAL NOT NULL, cover_status TEXT NOT NULL")
    conn.commit()
    return conn


def get_cover_mtime(conn, filepath):
    """Return (mtime, cover_status) or None."""
    return _get_kv_row(conn, "cover_tracking", filepath, ["mtime", "cover_status"])


def update_cover_tracking(conn, filepath, mtime, cover_status):
    """Insert or replace cover tracking entry. Does not commit (batched)."""
    _set_kv_row(conn, "cover_tracking", filepath, [("mtime", mtime), ("cover_status", cover_status)])


def find_files_needing_cover_update(music_dir, cover_db_path, num_workers=None):
    """Return list of file paths whose cover should be re-fetched.

    If cover_db_path is falsy, return all audio files (no incremental filtering).
    """
    files = []
    if not cover_db_path:
        for root, _dirs, filenames in os.walk(music_dir, onerror=_on_walk_error):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in AUDIO_EXTS:
                    continue
                files.append(os.path.join(root, fname))
        return files

    try:
        conn = init_cover_db(cover_db_path)
    except sqlite3.Error as e:
        print(f"WARN: could not open cover DB {cover_db_path} ({e})", file=sys.stderr)
        # Fall back to listing all files
        return find_files_needing_cover_update(music_dir, None, num_workers=num_workers)

    # Load tracked rows into memory to avoid per-file DB roundtrips
    cur = conn.cursor()
    tracked = {}
    try:
        cur.execute("SELECT filepath, mtime, cover_status FROM cover_tracking")
        tracked = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        # If cover_tracking table exists but is empty, also consider file_mtime_tracking
        if not tracked:
            try:
                cur.execute("SELECT filepath, mtime FROM file_mtime_tracking")
                tracked = {row[0]: (row[1], 'success') for row in cur.fetchall()}
            except sqlite3.OperationalError:
                pass
    except sqlite3.OperationalError:
        # If cover_tracking table doesn't exist, fall back to file_mtime_tracking
        try:
            cur.execute("SELECT filepath, mtime FROM file_mtime_tracking")
            tracked = {row[0]: (row[1], 'success') for row in cur.fetchall()}
        except sqlite3.OperationalError:
            tracked = {}

    try:
        for root, _dirs, filenames in os.walk(music_dir, onerror=_on_walk_error):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in AUDIO_EXTS:
                    continue
                path = os.path.join(root, fname)
                try:
                    current_mtime = os.path.getmtime(path)
                except OSError:
                    continue

                tracked_row = tracked.get(path)
                if tracked_row is None:
                    files.append(path)
                else:
                    tracked_mtime, tracked_status = tracked_row
                    # Only skip if tracked status is 'success' and mtime hasn't changed
                    if tracked_status == 'success' and abs(current_mtime - tracked_mtime) <= MTIME_TOLERANCE:
                        continue
                    files.append(path)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return files


def mark_cover_processed(conn_or_path, records):
    """Mark multiple files as processed.

    records: iterable of (filepath, cover_status). If conn_or_path is a
    sqlite3.Connection, it is used; otherwise conn_or_path is treated as a
    DB path and a temporary connection is opened and committed.
    """
    transient = False
    if isinstance(conn_or_path, str):
        conn = init_cover_db(conn_or_path)
        transient = True
    else:
        conn = conn_or_path

    try:
        for filepath, status in records:
            try:
                mtime = os.path.getmtime(filepath)
            except OSError:
                # If file missing, skip
                continue
            update_cover_tracking(conn, filepath, mtime, status)
        if transient:
            commit_mtime_db(conn)
    finally:
        if transient:
            try:
                conn.close()
            except Exception:
                pass


def init_failed_matches_db(db_path):
    """Create/open the failed_matches DB and ensure schema exists. Returns sqlite3.Connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS failed_matches (
            filepath TEXT PRIMARY KEY,
            error_reason TEXT NOT NULL,
            match_attempts INTEGER NOT NULL,
            last_attempt_time REAL NOT NULL
        )"""
    )
    conn.commit()
    return conn


# Module-level lock to guard shared sqlite connection usage from multiple
# threads. sqlite3 connections with check_same_thread=False can be used from
# other threads, but cursors/execute must not be used concurrently.
_FAILED_MATCHES_LOCK = threading.Lock()


def record_failed_match(conn_or_path, filepath, error_reason):
    """Atomically increment or insert a failed_matches row for filepath.

    Accepts either a sqlite3.Connection or a DB path string. Uses a single
    INSERT ... ON CONFLICT DO UPDATE statement to avoid TOCTOU races.
    """
    transient = False
    if isinstance(conn_or_path, str):
        conn = init_failed_matches_db(conn_or_path)
        transient = True
    else:
        conn = conn_or_path

    try:
        now = time.time()
        # Use a lock when using a shared connection to avoid concurrent
        # cursor/execute calls which sqlite3 doesn't allow.
        if transient:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO failed_matches (filepath, error_reason, match_attempts, last_attempt_time)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(filepath) DO UPDATE SET
                       match_attempts = failed_matches.match_attempts + 1,
                       error_reason = excluded.error_reason,
                       last_attempt_time = excluded.last_attempt_time
                """,
                (filepath, error_reason, now),
            )
            try:
                conn.commit()
            except Exception:
                pass
        else:
            with _FAILED_MATCHES_LOCK:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO failed_matches (filepath, error_reason, match_attempts, last_attempt_time)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(filepath) DO UPDATE SET
                           match_attempts = failed_matches.match_attempts + 1,
                           error_reason = excluded.error_reason,
                           last_attempt_time = excluded.last_attempt_time
                    """,
                    (filepath, error_reason, now),
                )
                try:
                    conn.commit()
                except Exception:
                    pass
    finally:
        if transient:
            try:
                conn.close()
            except Exception:
                pass


def get_failed_match(conn_or_path, filepath):
    """Return (error_reason, match_attempts, last_attempt_time) or None."""
    transient = False
    if isinstance(conn_or_path, str):
        conn = init_failed_matches_db(conn_or_path)
        transient = True
    else:
        conn = conn_or_path
    try:
        cur = conn.cursor()
        cur.execute("SELECT error_reason, match_attempts, last_attempt_time FROM failed_matches WHERE filepath = ?", (filepath,))
        row = cur.fetchone()
        return tuple(row) if row else None
    finally:
        if transient:
            try:
                conn.close()
            except Exception:
                pass


def scan_and_update(music_dir, beets_config_path, out_file, max_scan_workers=None, mtime_db_path=None):
    """Walks music_dir with a pool of worker threads (I/O-bound file checks
    benefit from concurrency, especially on network mounts), dispatching
    each file found missing cover art or tags to a single updater thread
    the moment it's found - not after the whole scan finishes.

    Returns (total_checked, [incomplete_paths], updated_count, failed_count).
    """
    if max_scan_workers is None:
        max_scan_workers = DEFAULT_SCAN_WORKERS

    incomplete = []
    checked = 0
    state = {"checked": 0, "incomplete": 0, "updated": 0, "failed": 0}

    stop_event = threading.Event()
    start_time = time.monotonic()
    heartbeat_thread = threading.Thread(
        target=_heartbeat, args=(state, stop_event, start_time), daemon=True)
    heartbeat_thread.start()

    # Optional mtime DB: open connection once and pass it to the updater
    mtime_conn = None
    db_lock = None
    if mtime_db_path:
        try:
            mtime_conn = init_mtime_db(mtime_db_path)
            db_lock = threading.Lock()
        except sqlite3.Error as e:
            print(f"WARN: could not open mtime DB {mtime_db_path} ({e})", file=sys.stderr)
            mtime_conn = None
            db_lock = None

    # Optional failed_matches DB path via env var FAILED_MATCH_DB
    failed_conn = None
    failed_db_lock = None
    failed_db_path = os.environ.get('FAILED_MATCH_DB')
    if failed_db_path:
        try:
            failed_conn = init_failed_matches_db(failed_db_path)
            failed_db_lock = threading.Lock()
        except sqlite3.Error as e:
            print(f"WARN: could not open failed matches DB {failed_db_path} ({e})", file=sys.stderr)
            failed_conn = None
            failed_db_lock = None

    work_queue = queue.Queue()
    updater_thread = threading.Thread(
        target=_updater_worker, args=(work_queue, beets_config_path, state, None, mtime_conn, db_lock, music_dir, failed_conn, failed_db_lock), daemon=True)
    updater_thread.start()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_scan_workers) as pool:
            futures = {}
            for root, _dirs, files in os.walk(music_dir, onerror=_on_walk_error):
                for fname in files:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in AUDIO_EXTS:
                        continue
                    path = os.path.join(root, fname)

                    # Capture mtime once and reuse
                    try:
                        current_mtime = os.path.getmtime(path)
                    except OSError as e:
                        print(f"WARN: could not stat {path} ({e})", file=sys.stderr)
                        continue
                    try:
                        current_size = os.path.getsize(path)
                    except OSError as e:
                        print(f"WARN: could not stat size for {path} ({e})", file=sys.stderr)
                        continue

                    # If DB present, check tracked mtime+size and skip if unchanged
                    if mtime_conn:
                        try:
                            tracked = get_tracked_mtime(mtime_conn, path)
                            if tracked is not None:
                                tracked_mtime, tracked_size = tracked
                                if abs(current_mtime - tracked_mtime) <= MTIME_TOLERANCE and current_size == tracked_size:
                                    # skip this file (unchanged since last successful update)
                                    checked += 1
                                    state["checked"] = checked
                                    state["incomplete"] = len(incomplete)
                                    # record skipped for reporting but do not submit to workers
                                    if "skipped" not in state:
                                        state["skipped"] = 0
                                    state["skipped"] += 1
                                    continue
                        except sqlite3.Error as e:
                            print(f"WARN: mtime DB read failed for {path} ({e})", file=sys.stderr)
                            # Fall through and inspect the file rather than aborting the scan

                    # Submit the check; store path + current_mtime + current_size for later
                    futures[pool.submit(_check_file, path)] = (path, current_mtime, current_size)

                    for future in [f for f in futures if f.done()]:
                        done_info = futures.pop(future)
                        checked = _handle_check_result(
                            future, done_info, incomplete, work_queue, checked, state)
            for future in concurrent.futures.as_completed(futures):
                done_info = futures.pop(future)
                checked = _handle_check_result(
                    future, done_info, incomplete, work_queue, checked, state)
    finally:
        # Ensure the updater is told to stop and join, and that DB connection is closed
        work_queue.put(None)
        updater_thread.join()
        stop_event.set()
        heartbeat_thread.join()
        if mtime_conn:
            try:
                # Prune stale rows for files that were deleted/moved during scans
                try:
                    prune_stale_mtime_rows(mtime_conn)
                except sqlite3.Error:
                    pass
                # Commit batched mtime updates made by the updater thread
                try:
                    commit_mtime_db(mtime_conn)
                except sqlite3.Error:
                    pass
                mtime_conn.close()
            except Exception:
                pass
        if failed_conn:
            try:
                failed_conn.close()
            except Exception:
                pass

    with open(out_file, "w") as f:
        for p in incomplete:
            f.write(p + "\n")

    # Print an additional summary line including skipped count for clarity
    skipped = state.get("skipped", 0)
    if skipped:
        print(f"Summary: {checked} checked, {skipped} skipped by mtime tracking, {len(incomplete)} incomplete, {state['updated']} updated, {state['failed']} failed", flush=True)

    return checked, incomplete, state["updated"], state["failed"]


def main():
    music_dir = sys.argv[1]
    out_file = sys.argv[2]
    beets_config_path = sys.argv[3]
    mtime_db_path = sys.argv[4] if len(sys.argv) > 4 else None

    checked, incomplete, updated, failed = scan_and_update(
        music_dir, beets_config_path, out_file, mtime_db_path=mtime_db_path)
    print(f"{checked} audio files checked, {len(incomplete)} incomplete, "
          f"{updated} updated, {failed} failed.", flush=True)


if __name__ == "__main__":
    main()
