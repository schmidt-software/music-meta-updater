import sys
import os
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scan_incomplete as si


def test_cover_tracking_end_to_end(tmp_path, monkeypatch):
    # Setup three files: untracked, tracked-but-modified, and tracked-and-unchanged
    base = tmp_path
    a = base / "a.mp3"
    b = base / "b.mp3"
    c = base / "c.mp3"
    a.write_bytes(b"")
    b.write_bytes(b"")
    c.write_bytes(b"")

    db_path = tmp_path / "cover.db"

    # Initially, find_files_needing_cover_update with no DB returns all files
    files = si.find_files_needing_cover_update(str(base), str(db_path))
    assert set(files) == {str(a), str(b), str(c)}

    # Mark 'a' as processed (success)
    conn = si.init_mtime_db(str(db_path))  # reuse mtime DB helpers for schema; find uses same schema
    si.update_mtime_tracking(conn, str(a), os.path.getmtime(a), os.path.getsize(a))
    si.commit_mtime_db(conn)
    conn.close()

    # Now find_files_needing_cover_update should exclude 'a'
    files2 = si.find_files_needing_cover_update(str(base), str(db_path))
    assert str(a) not in files2

    # Mark 'b' as processed with an older mtime (simulate modified file since processing)
    conn = si.init_mtime_db(str(db_path))
    si.update_mtime_tracking(conn, str(b), 0.0, os.path.getsize(b))
    si.commit_mtime_db(conn)
    conn.close()

    files3 = si.find_files_needing_cover_update(str(base), str(db_path))
    assert str(b) in files3

    # Clean up
    os.remove(str(db_path))
