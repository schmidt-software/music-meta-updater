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
from mutagen import File as MutagenFile

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aac"}

DEFAULT_SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "8"))


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


def _update_file(file_path, beets_config_path, run=subprocess.run):
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
        return False
    print(f"Metadata written: {file_path}", flush=True)

    query = f"path:{file_path}"
    run(["beet", "-v", "-c", beets_config_path, "fetchart", "-q", query])
    run(["beet", "-v", "-c", beets_config_path, "embedart", "-y", query])
    print(f"Cover art updated: {file_path}", flush=True)
    return True


def _updater_worker(work_queue, beets_config_path, state, update_fn=None, mtime_conn=None, db_lock=None):
    """Consumes (path, mtime) tuples from work_queue one at a time until it
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
            file_path, file_mtime = item
        except Exception:
            # Backwards-compat: if older callers placed just a path, accept it
            file_path = item
            try:
                file_mtime = os.path.getmtime(file_path)
            except Exception:
                file_mtime = None

        success = update_fn(file_path, beets_config_path)
        if success:
            state["updated"] += 1
            # Record tracked mtime only on confirmed success
            if mtime_conn and file_mtime is not None:
                try:
                    update_mtime_tracking(mtime_conn, file_path, file_mtime)
                except sqlite3.Error as e:
                    print(f"WARN: could not update mtime DB for {file_path} ({e})", file=sys.stderr)
        else:
            state["failed"] += 1
        work_queue.task_done()


def _handle_check_result(future, path_info, incomplete, work_queue, checked, state):
    kind, msg = future.result()
    path, file_mtime = path_info
    if kind == "warn":
        print(msg, file=sys.stderr, flush=True)
    elif kind == "incomplete":
        incomplete.append(path)
        # enqueue tuple(path, mtime) so updater can record the tracked mtime
        work_queue.put((path, file_mtime))
    checked += 1
    state["checked"] = checked
    state["incomplete"] = len(incomplete)
    return checked


def init_mtime_db(db_path):
    """Create/open the mtime tracking DB and ensure schema exists. Returns sqlite3.Connection."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS file_mtime_tracking (
        filepath TEXT PRIMARY KEY,
        mtime REAL NOT NULL
    )
    """)
    conn.commit()
    return conn


def get_tracked_mtime(conn, filepath):
    """Return tracked mtime (float) or None. May raise sqlite3.Error."""
    cur = conn.cursor()
    cur.execute("SELECT mtime FROM file_mtime_tracking WHERE filepath = ?", (filepath,))
    row = cur.fetchone()
    return row[0] if row else None


def update_mtime_tracking(conn, filepath, mtime):
    """Insert or replace tracked mtime for filepath. May raise sqlite3.Error."""
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO file_mtime_tracking (filepath, mtime) VALUES (?, ?)", (filepath, mtime))
    conn.commit()


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

    work_queue = queue.Queue()
    updater_thread = threading.Thread(
        target=_updater_worker, args=(work_queue, beets_config_path, state, None, mtime_conn, db_lock), daemon=True)
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

                    # If DB present, check tracked mtime and skip if unchanged
                    if mtime_conn:
                        try:
                            tracked = get_tracked_mtime(mtime_conn, path)
                            if tracked is not None and current_mtime == tracked:
                                # skip this file (unchanged since last successful update)
                                continue
                        except sqlite3.Error as e:
                            print(f"WARN: mtime DB read failed for {path} ({e})", file=sys.stderr)
                            # Fall through and inspect the file rather than aborting the scan

                    # Submit the check; store path + current_mtime for later
                    futures[pool.submit(_check_file, path)] = (path, current_mtime)

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
                mtime_conn.close()
            except Exception:
                pass

    with open(out_file, "w") as f:
        for p in incomplete:
            f.write(p + "\n")

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
