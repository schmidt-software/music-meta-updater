import sys
import os
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scan_incomplete as si


def test_init_mtime_db_failure_does_not_abort_scan(tmp_path, monkeypatch):
    # Create one audio file
    song = tmp_path / "song.mp3"
    song.write_bytes(b"")

    # Make init_mtime_db raise sqlite3.Error to simulate DB open failure
    def fake_init(path):
        raise sqlite3.Error("unable to open DB")

    monkeypatch.setattr(si, "init_mtime_db", fake_init)

    # Mutagen reports the file as missing
    fake_mf = type("FakeMF", (), {"tags": None, "filename": str(song)})()
    monkeypatch.setattr(si, "MutagenFile", lambda p: fake_mf)

    # Ensure updater is a no-op (so scan completes quickly)
    monkeypatch.setattr(si, "_update_file", lambda *a, **k: False)

    out_file = tmp_path / "incomplete.lst"
    checked, incomplete, updated, failed = si.scan_and_update(str(tmp_path), "/data/beets_config.yaml", str(out_file), mtime_db_path=str(tmp_path / "mtime.db"), max_scan_workers=1)

    assert str(song) in incomplete


def test_get_tracked_mtime_failure_does_not_abort_scan(tmp_path, monkeypatch):
    song = tmp_path / "song2.mp3"
    song.write_bytes(b"")

    # init real DB so other code paths may use it
    db_path = tmp_path / "mtime.db"
    conn = si.init_mtime_db(str(db_path))
    si.commit_mtime_db(conn)
    conn.close()

    # Make get_tracked_mtime raise during scan
    def fake_get_tracked(conn, path):
        raise sqlite3.Error("read error")

    monkeypatch.setattr(si, "get_tracked_mtime", fake_get_tracked)

    fake_mf = type("FakeMF", (), {"tags": None, "filename": str(song)})()
    monkeypatch.setattr(si, "MutagenFile", lambda p: fake_mf)
    monkeypatch.setattr(si, "_update_file", lambda *a, **k: False)

    out_file = tmp_path / "incomplete2.lst"
    checked, incomplete, updated, failed = si.scan_and_update(str(tmp_path), "/data/beets_config.yaml", str(out_file), mtime_db_path=str(db_path), max_scan_workers=1)

    assert str(song) in incomplete
