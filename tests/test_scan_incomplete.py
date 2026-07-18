import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scan_incomplete as si


class FakeTags(dict):
    """Mimics a mutagen tags mapping (dict-like, values sometimes wrapped in lists)."""


class FakeMF:
    def __init__(self, tags=None, pictures=None, filename=None):
        self.tags = tags
        if pictures is not None:
            self.pictures = pictures
        if filename is not None:
            self.filename = filename


class RaisingTags:
    def keys(self):
        raise RuntimeError("boom")


# --------------------------- has_cover ---------------------------------


def test_has_cover_id3_apic():
    mf = FakeMF(tags=FakeTags({"APIC:cover": b"..."}))
    assert si.has_cover(mf) is True


def test_has_cover_mp4_covr():
    mf = FakeMF(tags=FakeTags({"covr": [b"..."]}))
    assert si.has_cover(mf) is True


def test_has_cover_flac_pictures():
    mf = FakeMF(tags=FakeTags({}), pictures=["picture-data"])
    assert si.has_cover(mf) is True


def test_has_cover_vorbis_metadata_block_picture():
    mf = FakeMF(tags=FakeTags({"metadata_block_picture": ["base64..."]}))
    assert si.has_cover(mf) is True


def test_has_cover_no_tags():
    mf = FakeMF(tags=None)
    assert si.has_cover(mf) is False


def test_has_cover_tags_without_art():
    mf = FakeMF(tags=FakeTags({"title": ["Some Title"]}))
    assert si.has_cover(mf) is False


def test_has_cover_swallows_exceptions():
    mf = FakeMF(tags=RaisingTags())
    assert si.has_cover(mf) is False


# ------------------------- has_basic_tags -------------------------------


def test_has_basic_tags_all_present():
    mf = FakeMF(tags=FakeTags({
        "title": ["Song"],
        "artist": ["Artist"],
        "album": ["Album"],
    }))
    assert si.has_basic_tags(mf) is True


def test_has_basic_tags_plain_string_values():
    mf = FakeMF(tags=FakeTags({
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
    }))
    assert si.has_basic_tags(mf) is True


def test_has_basic_tags_missing_artist():
    mf = FakeMF(tags=FakeTags({
        "title": ["Song"],
        "album": ["Album"],
    }))
    assert si.has_basic_tags(mf) is False


def test_has_basic_tags_empty_value():
    mf = FakeMF(tags=FakeTags({
        "title": [""],
        "artist": ["Artist"],
        "album": ["Album"],
    }))
    assert si.has_basic_tags(mf) is False


def test_has_basic_tags_no_tags():
    mf = FakeMF(tags=None)
    assert si.has_basic_tags(mf) is False


# --------------------------- find_incomplete -----------------------------


def test_find_incomplete(tmp_path, monkeypatch):
    complete = tmp_path / "complete.mp3"
    complete.write_bytes(b"")
    missing_cover = tmp_path / "missing_cover.mp3"
    missing_cover.write_bytes(b"")
    missing_tags = tmp_path / "missing_tags.flac"
    missing_tags.write_bytes(b"")
    corrupt = tmp_path / "corrupt.mp3"
    corrupt.write_bytes(b"")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("not audio")

    complete_mf = FakeMF(tags=FakeTags({
        "APIC:cover": b"...",
        "title": ["T"], "artist": ["A"], "album": ["Al"],
    }))
    missing_cover_mf = FakeMF(tags=FakeTags({
        "title": ["T"], "artist": ["A"], "album": ["Al"],
    }))
    missing_tags_mf = FakeMF(tags=FakeTags({"APIC:cover": b"..."}))

    by_name = {
        str(complete): complete_mf,
        str(missing_cover): missing_cover_mf,
        str(missing_tags): missing_tags_mf,
    }

    def fake_mutagen_file(path):
        if path == str(corrupt):
            raise ValueError("cannot parse")
        return by_name.get(path)

    monkeypatch.setattr(si, "MutagenFile", fake_mutagen_file)

    total, incomplete = si.find_incomplete(str(tmp_path))

    assert total == 4  # all *.mp3/*.flac files, txt excluded
    assert str(complete) not in incomplete
    assert str(missing_cover) in incomplete
    assert str(missing_tags) in incomplete
    assert str(corrupt) not in incomplete


def test_find_incomplete_reports_directory_listing_errors(tmp_path, monkeypatch, capsys):
    """A transient error listing a subdirectory (common on flaky network
    mounts) must be reported, not silently swallowed, and must not abort
    the scan."""

    def fake_walk(path, onerror=None):
        onerror(OSError("Permission denied: some/subdir"))
        return iter([])

    monkeypatch.setattr(si.os, "walk", fake_walk)

    total, incomplete = si.find_incomplete(str(tmp_path))

    captured = capsys.readouterr()
    assert "could not list directory" in captured.err
    assert total == 0
    assert incomplete == []


# ----------------------- Incremental mtime tracking ----------------------


def test_init_mtime_db():
    """Database is created with the correct schema."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    try:
        conn = si.init_mtime_db(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='file_mtime_tracking'"
        )
        assert cursor.fetchone() is not None
        conn.close()
    finally:
        os.unlink(db_path)


def test_mtime_tracking_get_and_update():
    """Mtime can be stored and retrieved."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    try:
        conn = si.init_mtime_db(db_path)
        filepath = "/path/to/file.mp3"
        mtime = 1234567890.5

        # Initially not tracked
        assert si.get_tracked_mtime(conn, filepath) is None

        # Update and retrieve
        si.update_mtime_tracking(conn, filepath, mtime)
        assert si.get_tracked_mtime(conn, filepath) == mtime

        conn.close()
    finally:
        os.unlink(db_path)


def test_find_incomplete_incremental_mode_skips_unchanged(tmp_path, monkeypatch):
    """In incremental mode, files with unchanged mtime are skipped."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name

    try:
        unchanged = tmp_path / "unchanged.mp3"
        unchanged.write_bytes(b"")
        changed = tmp_path / "changed.mp3"
        changed.write_bytes(b"")

        # Pre-populate DB with unchanged file
        conn = si.init_mtime_db(db_path)
        unchanged_mtime = os.path.getmtime(str(unchanged))
        si.update_mtime_tracking(conn, str(unchanged), unchanged_mtime)
        conn.close()

        unchanged_mf = FakeMF(tags=FakeTags())  # incomplete (missing tags)
        changed_mf = FakeMF(tags=FakeTags())    # incomplete

        call_count = {"unchanged": 0, "changed": 0}

        def fake_mutagen_file(path):
            if str(unchanged) in path:
                call_count["unchanged"] += 1
                return unchanged_mf
            elif str(changed) in path:
                call_count["changed"] += 1
                return changed_mf
            return None

        monkeypatch.setattr(si, "MutagenFile", fake_mutagen_file)

        # First incremental scan: unchanged file should be skipped
        total, incomplete = si.find_incomplete(str(tmp_path), db_path)

        # unchanged file's MutagenFile should NOT have been called
        # (it's been skipped due to matching mtime)
        assert call_count["unchanged"] == 0
        # changed file WAS scanned
        assert call_count["changed"] == 1
        assert str(changed) in incomplete
        assert str(unchanged) not in incomplete
    finally:
        os.unlink(db_path)


def test_find_incomplete_incremental_detects_file_changes(tmp_path, monkeypatch):
    """When a file is modified, incremental scan detects it."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name

    try:
        file_to_modify = tmp_path / "track.mp3"
        file_to_modify.write_bytes(b"old content")

        # First scan: record current mtime
        conn = si.init_mtime_db(db_path)
        initial_mtime = os.path.getmtime(str(file_to_modify))
        si.update_mtime_tracking(conn, str(file_to_modify), initial_mtime)
        conn.close()

        # Modify the file (change content + mtime)
        import time
        time.sleep(0.01)  # ensure mtime differs
        file_to_modify.write_bytes(b"new content")

        scan_count = {"count": 0}

        def fake_mutagen_file(path):
            scan_count["count"] += 1
            return FakeMF(tags=FakeTags())  # incomplete

        monkeypatch.setattr(si, "MutagenFile", fake_mutagen_file)

        # Second scan: file should be rescanned because mtime changed
        total, incomplete = si.find_incomplete(str(tmp_path), db_path)

        assert scan_count["count"] > 0  # file WAS scanned
        assert str(file_to_modify) in incomplete
    finally:
        os.unlink(db_path)
