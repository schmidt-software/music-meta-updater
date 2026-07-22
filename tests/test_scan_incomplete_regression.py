import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scan_incomplete as si


def test_incomplete_resurfaces_after_failed_update(tmp_path, monkeypatch):
    # Create a single audio file
    song = tmp_path / "song.mp3"
    song.write_bytes(b"")

    # Mutagen reports the file as missing tags/cover
    mf = si.FakeMF = type("F", (), {})  # sentinel if needed; but monkeypatch below

    fake_mf = type("FakeMF", (), {"tags": None, "filename": str(song)})()

    def fake_mutagen(path):
        return fake_mf

    monkeypatch.setattr(si, "MutagenFile", fake_mutagen)

    # Simulate beets failing to tag the file
    def fake_update(file_path, beets_config_path):
        return False

    monkeypatch.setattr(si, "_update_file", fake_update)

    mtime_db = tmp_path / "mtime.db"

    out_file = tmp_path / "incomplete.lst"

    # First run: file should be reported incomplete
    checked1, incomplete1, updated1, failed1 = si.scan_and_update(
        str(tmp_path), "/data/beets_config.yaml", str(out_file), max_scan_workers=1, mtime_db_path=str(mtime_db)
    )

    assert str(song) in incomplete1

    # Second run (no changes): because update failed, mtime should not have been recorded
    out_file2 = tmp_path / "incomplete2.lst"
    checked2, incomplete2, updated2, failed2 = si.scan_and_update(
        str(tmp_path), "/data/beets_config.yaml", str(out_file2), max_scan_workers=1, mtime_db_path=str(mtime_db)
    )

    assert str(song) in incomplete2
