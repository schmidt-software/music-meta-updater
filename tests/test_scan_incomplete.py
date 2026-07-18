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


# --------------------------- find_incomplete (returns 3-tuple now) --------


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

    total, incomplete, errors = si.find_incomplete(str(tmp_path))

    assert total == 4  # all *.mp3/*.flac files, txt excluded
    assert str(complete) not in incomplete
    assert str(missing_cover) in incomplete
    assert str(missing_tags) in incomplete
    assert str(corrupt) not in incomplete
    assert isinstance(errors, dict)


def test_find_incomplete_reports_directory_listing_errors(tmp_path, monkeypatch, capsys):
    """A transient error listing a subdirectory (common on flaky network
    mounts) must be reported, not silently swallowed, and must not abort
    the scan."""

    def fake_walk(path, onerror=None):
        onerror(OSError("Permission denied: some/subdir"))
        return iter([])

    monkeypatch.setattr(si.os, "walk", fake_walk)

    total, incomplete, errors = si.find_incomplete(str(tmp_path))

    captured = capsys.readouterr()
    assert "could not list directory" in captured.err
    assert total == 0
    assert incomplete == []


# ----------------------- Error tracking & resilience --------------------


def test_init_error_db():
    """Error tracking database is created with correct schema."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    try:
        conn = si.init_error_db(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='error_tracking'"
        )
        assert cursor.fetchone() is not None
        conn.close()
    finally:
        os.unlink(db_path)


def test_record_and_check_error():
    """Errors can be recorded and files can be blacklisted."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    try:
        conn = si.init_error_db(db_path)
        filepath = "/path/to/flaky_file.mp3"
        
        # Record an error
        si.record_error(conn, filepath, "io_error", "Connection timeout", blacklist_duration=1)
        
        # File should be blacklisted
        assert si.is_blacklisted(conn, filepath) is True
        
        # Wait for blacklist to expire
        import time
        time.sleep(1.1)
        
        # File should no longer be blacklisted
        assert si.is_blacklisted(conn, filepath) is False
        conn.close()
    finally:
        os.unlink(db_path)


def test_get_error_type():
    """Error classification works correctly."""
    assert si.get_error_type(PermissionError("denied")) == "permission_error"
    assert si.get_error_type(FileNotFoundError("missing")) == "file_not_found"
    assert si.get_error_type(OSError("io error")) == "io_error"
    # TimeoutError is subclass of OSError, so it's classified as io_error
    assert si.get_error_type(TimeoutError("timeout")) == "io_error"
    assert si.get_error_type(ValueError("other")) == "unknown_error"


def test_error_telemetry_collection():
    """Error telemetry is collected correctly."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mtime.db") as f:
        mtime_db = f.name
    with tempfile.NamedTemporaryFile(delete=False, suffix=".error.db") as f:
        error_db = f.name
    
    try:
        # Create a tmp_path with audio files
        import tempfile as tmplib
        with tmplib.TemporaryDirectory() as tmp_dir:
            file1 = os.path.join(tmp_dir, "track.mp3")
            open(file1, "w").close()
            
            # Mock MutagenFile to raise errors
            original_check_file = si._check_file_with_retry
            call_count = [0]
            
            def mock_check_file(path, mtime_db_path=None, error_db_path=None):
                call_count[0] += 1
                # Simulate I/O error
                if call_count[0] <= si.MAX_RETRIES:
                    raise OSError("Simulated I/O error")
                # After retries, record error
                return (path, False, True, ("io_error", "Simulated I/O error"))
            
            si._check_file_with_retry = mock_check_file
            try:
                total, incomplete, errors = si.find_incomplete(tmp_dir, mtime_db, error_db, num_workers=1)
                # Should have captured io_error
                assert "io_error" in errors or len(errors) > 0
            finally:
                si._check_file_with_retry = original_check_file
    finally:
        if os.path.exists(mtime_db):
            os.unlink(mtime_db)
        if os.path.exists(error_db):
            os.unlink(error_db)


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
        total, incomplete, _ = si.find_incomplete(str(tmp_path), db_path)

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
        total, incomplete, _ = si.find_incomplete(str(tmp_path), db_path)

        assert scan_count["count"] > 0  # file WAS scanned
        assert str(file_to_modify) in incomplete
    finally:
        os.unlink(db_path)


# ----------------------- Parallel file checking -----------------------


def test_find_incomplete_with_parallel_workers(tmp_path, monkeypatch):
    """Parallel scan with multiple workers produces same results as single-threaded."""
    file1 = tmp_path / "track1.mp3"
    file1.write_bytes(b"")
    file2 = tmp_path / "track2.mp3"
    file2.write_bytes(b"")
    file3 = tmp_path / "track3.mp3"
    file3.write_bytes(b"")

    # Mock mutagen: file1 complete, file2/file3 incomplete
    complete_mf = FakeMF(tags=FakeTags({
        "APIC:cover": b"...",
        "title": ["T"], "artist": ["A"], "album": ["Al"],
    }))
    incomplete_mf = FakeMF(tags=FakeTags())

    by_name = {
        str(file1): complete_mf,
        str(file2): incomplete_mf,
        str(file3): incomplete_mf,
    }

    def fake_mutagen_file(path):
        return by_name.get(path)

    monkeypatch.setattr(si, "MutagenFile", fake_mutagen_file)

    # Single-threaded scan
    _, incomplete_single, _ = si.find_incomplete(str(tmp_path), None, None, num_workers=1)

    # Multi-threaded scan
    _, incomplete_multi, _ = si.find_incomplete(str(tmp_path), None, None, num_workers=4)

    # Results should be identical
    assert set(incomplete_single) == set(incomplete_multi)
    assert len(incomplete_multi) == 2
    assert str(file1) not in incomplete_multi
    assert str(file2) in incomplete_multi
    assert str(file3) in incomplete_multi


def test_find_incomplete_parallel_with_incremental(tmp_path, monkeypatch):
    """Parallel + incremental mode work together."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name

    try:
        file1 = tmp_path / "track1.mp3"
        file1.write_bytes(b"")
        file2 = tmp_path / "track2.mp3"
        file2.write_bytes(b"")

        # Pre-populate DB with file1 (unchanged)
        conn = si.init_mtime_db(db_path)
        mtime1 = os.path.getmtime(str(file1))
        si.update_mtime_tracking(conn, str(file1), mtime1)
        conn.close()

        incomplete_mf = FakeMF(tags=FakeTags())

        call_count = {"file1": 0, "file2": 0}

        def fake_mutagen_file(path):
            if str(file1) in path:
                call_count["file1"] += 1
            elif str(file2) in path:
                call_count["file2"] += 1
            return incomplete_mf

        monkeypatch.setattr(si, "MutagenFile", fake_mutagen_file)

        # Parallel + incremental scan
        _, incomplete, _ = si.find_incomplete(str(tmp_path), db_path, None, num_workers=2)

        # file1 should be skipped (unchanged mtime)
        # file2 should be scanned
        assert call_count["file1"] == 0  # skipped
        assert call_count["file2"] == 1  # scanned
        assert str(file2) in incomplete
        assert str(file1) not in incomplete
    finally:
        os.unlink(db_path)


# ----------------------- Cover tracking & incremental fetching ----------


def test_init_cover_db():
    """Cover tracking database is created with correct schema."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    try:
        conn = si.init_cover_db(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cover_tracking'"
        )
        assert cursor.fetchone() is not None
        conn.close()
    finally:
        os.unlink(db_path)


def test_cover_tracking_get_and_update():
    """Cover mtime can be stored and retrieved."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    try:
        conn = si.init_cover_db(db_path)
        filepath = "/path/to/file.mp3"
        mtime = 1234567890.5

        # Initially not tracked
        assert si.get_cover_mtime(conn, filepath) is None

        # Update and retrieve
        si.update_cover_tracking(conn, filepath, mtime, cover_status="success")
        assert si.get_cover_mtime(conn, filepath) == mtime

        conn.close()
    finally:
        os.unlink(db_path)


def test_find_files_needing_cover_update(tmp_path):
    """Identifies files that need cover re-fetching due to mtime change."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name

    try:
        file1 = tmp_path / "processed.mp3"
        file1.write_bytes(b"")
        file2 = tmp_path / "new.mp3"
        file2.write_bytes(b"")
        file3 = tmp_path / "modified.mp3"
        file3.write_bytes(b"old")

        # Pre-populate DB with file1 and file3 (with old mtime)
        conn = si.init_cover_db(db_path)
        mtime1 = os.path.getmtime(str(file1))
        si.update_cover_tracking(conn, str(file1), mtime1, "success")
        
        # file3: store old mtime
        old_mtime = mtime1 - 100
        si.update_cover_tracking(conn, str(file3), old_mtime, "success")
        conn.close()

        # Modify file3 to change mtime
        import time
        time.sleep(0.01)
        file3.write_bytes(b"new")

        # Find files needing update
        files_needing_update = si.find_files_needing_cover_update(str(tmp_path), db_path)

        # file1: processed and unchanged -> should NOT be in list
        # file2: never seen -> should be in list
        # file3: modified -> should be in list
        assert str(file1) not in files_needing_update
        assert str(file2) in files_needing_update
        assert str(file3) in files_needing_update
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_mark_cover_processed():
    """Cover processing status can be marked for a file."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            filepath = f.name

        # Mark as processed
        si.mark_cover_processed(db_path, filepath, "success")

        # Verify it was tracked
        conn = si.init_cover_db(db_path)
        mtime_tracked = si.get_cover_mtime(conn, filepath)
        assert mtime_tracked is not None
        conn.close()
        
        os.unlink(filepath)
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


# ----------------------- Failed matches tracking & fallback ---------------


def test_init_failed_matches_db():
    """Failed matches database is created with correct schema."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    try:
        conn = si.init_failed_matches_db(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='failed_matches'"
        )
        assert cursor.fetchone() is not None
        conn.close()
    finally:
        os.unlink(db_path)


def test_record_failed_match():
    """Failed matches can be recorded with fallback metadata."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    try:
        conn = si.init_failed_matches_db(db_path)
        filepath = "/path/to/track.mp3"
        
        # Record first failed match
        si.record_failed_match(conn, filepath, "no_confident_match", 
                              fallback_artist="The Beatles", fallback_album="Abbey Road")
        
        # Get the record
        record = si.get_failed_match(conn, filepath)
        assert record is not None
        error_reason, attempts, fallback_artist, fallback_album = record
        assert error_reason == "no_confident_match"
        assert attempts == 1
        assert fallback_artist == "The Beatles"
        assert fallback_album == "Abbey Road"
        
        # Record second attempt (attempts should increment)
        si.record_failed_match(conn, filepath, "timeout_on_acoustid")
        record = si.get_failed_match(conn, filepath)
        assert record[1] == 2  # attempts incremented
        
        conn.close()
    finally:
        os.unlink(db_path)
