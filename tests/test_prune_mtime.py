import sys
import os
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scan_incomplete as si


def test_prune_removes_missing_files(tmp_path, monkeypatch):
    # Setup: create one real file and one path that doesn't exist
    real = tmp_path / "real.mp3"
    real.write_bytes(b"")
    missing = tmp_path / "missing.mp3"

    db_path = tmp_path / "mtime.db"

    # Initialize DB and insert both entries
    conn = si.init_mtime_db(str(db_path))
    si.update_mtime_tracking(conn, str(real), os.path.getmtime(real), os.path.getsize(real))
    si.update_mtime_tracking(conn, str(missing), 1234567890.0, 42)
    si.commit_mtime_db(conn)
    conn.close()

    # Run scan_and_update pointing at the directory containing only the real file
    out_file = tmp_path / "incomplete.lst"
    checked, incomplete, updated, failed = si.scan_and_update(str(tmp_path), "/data/beets_config.yaml", str(out_file), mtime_db_path=str(db_path), max_scan_workers=1)

    # After scan, the DB should no longer contain the missing file entry
    conn2 = sqlite3.connect(str(db_path))
    cur = conn2.cursor()
    cur.execute("SELECT filepath FROM file_mtime_tracking WHERE filepath = ?", (str(missing),))
    assert cur.fetchone() is None
    conn2.close()
