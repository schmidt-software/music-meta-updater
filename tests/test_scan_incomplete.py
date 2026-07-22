import sys
import os

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


def test_has_cover_wma_picture():
    mf = FakeMF(tags=FakeTags({"WM/Picture": [b"..."]}))
    assert si.has_cover(mf) is True


def test_has_cover_flac_pictures():
    mf = FakeMF(tags=FakeTags({}), pictures=["picture-data"])
    assert si.has_cover(mf) is True


def test_has_cover_flac_pictures_without_any_tags_block():
    """A FLAC with an embedded picture but zero VORBIS_COMMENT tags has
    mf.tags is None (confirmed against real mutagen.flac.FLAC) - the
    pictures check must not be nested inside a "tags is not None" guard,
    or files like this are wrongly reported as missing a cover."""
    mf = FakeMF(tags=None, pictures=["picture-data"])
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


# ----------------------------- _check_file -------------------------------


def test_check_file_ok(monkeypatch):
    mf = FakeMF(tags=FakeTags({
        "APIC:cover": b"...",
        "title": ["T"], "artist": ["A"], "album": ["Al"],
    }))
    monkeypatch.setattr(si, "MutagenFile", lambda path: mf)
    assert si._check_file("/music/song.mp3") == ("ok", None)


def test_check_file_incomplete_missing_cover(monkeypatch):
    mf = FakeMF(tags=FakeTags({"title": ["T"], "artist": ["A"], "album": ["Al"]}))
    monkeypatch.setattr(si, "MutagenFile", lambda path: mf)
    assert si._check_file("/music/song.mp3") == ("incomplete", None)


def test_check_file_warns_on_read_error(monkeypatch):
    def raising(path):
        raise ValueError("cannot parse")
    monkeypatch.setattr(si, "MutagenFile", raising)
    kind, msg = si._check_file("/music/corrupt.mp3")
    assert kind == "warn"
    assert "could not read" in msg


def test_check_file_warns_on_unknown_format(monkeypatch):
    monkeypatch.setattr(si, "MutagenFile", lambda path: None)
    kind, msg = si._check_file("/music/weird.mp3")
    assert kind == "warn"
    assert "unknown/corrupt format" in msg


# ----------------------------- _update_file -------------------------------


class FakeCompletedProcess:
    def __init__(self, returncode):
        self.returncode = returncode


def test_update_file_success_runs_import_fetchart_embedart():
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return FakeCompletedProcess(0)

    ok = si._update_file("/music/song.mp3", "/data/beets_config.yaml", run=fake_run)

    assert ok is True
    assert calls[0] == ["beet", "-v", "-c", "/data/beets_config.yaml",
                         "import", "-q", "-s", "/music/song.mp3"]
    assert calls[1] == ["beet", "-v", "-c", "/data/beets_config.yaml",
                         "fetchart", "-q", "path:/music/song.mp3"]
    assert calls[2] == ["beet", "-v", "-c", "/data/beets_config.yaml",
                         "embedart", "-y", "path:/music/song.mp3"]


def test_update_file_import_failure_skips_fetchart_embedart(capsys):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return FakeCompletedProcess(1)

    ok = si._update_file("/music/song.mp3", "/data/beets_config.yaml", run=fake_run)

    assert ok is False
    assert len(calls) == 1
    assert "Could not automatically tag" in capsys.readouterr().err


# --------------------------- _updater_worker ------------------------------


def test_updater_worker_processes_queue_until_sentinel():
    import queue as queue_mod

    work_queue = queue_mod.Queue()
    work_queue.put(("/music/a.mp3", 1.0, 100))
    work_queue.put(("/music/b.mp3", 2.0, 200))
    work_queue.put(None)

    processed = []

    def fake_update(file_path, beets_config_path):
        processed.append(file_path)
        return file_path != "/music/b.mp3"  # simulate one success, one failure

    state = {"updated": 0, "failed": 0}
    si._updater_worker(work_queue, "/data/beets_config.yaml", state, update_fn=fake_update)

    assert processed == ["/music/a.mp3", "/music/b.mp3"]
    assert state["updated"] == 1
    assert state["failed"] == 1


# ----------------------------- _heartbeat ---------------------------------


class FakeStopEvent:
    """Stands in for threading.Event: wait() returns the next canned value
    instead of actually sleeping, so the heartbeat loop can be driven
    deterministically without real threads/timing."""

    def __init__(self, wait_returns):
        self._returns = iter(wait_returns)

    def wait(self, timeout):
        return next(self._returns)


def test_heartbeat_prints_once_per_tick(capsys, monkeypatch):
    monkeypatch.setattr(si.time, "monotonic", lambda: 105.0)
    state = {"checked": 42, "incomplete": 3, "updated": 2, "failed": 1}
    stop_event = FakeStopEvent([False, False, True])

    si._heartbeat(state, stop_event, start_time=100.0, interval=5.0)

    out = capsys.readouterr().out
    expected = ("Scanning: 42 files checked so far "
                "(3 incomplete found, 2 updated, 1 failed, 5s elapsed)\n")
    assert out == expected * 2


def test_heartbeat_stops_immediately_when_event_already_set(capsys):
    state = {"checked": 0, "incomplete": 0, "updated": 0, "failed": 0}
    stop_event = FakeStopEvent([True])

    si._heartbeat(state, stop_event, start_time=100.0)

    assert capsys.readouterr().out == ""


# --------------------------- scan_and_update ------------------------------


def test_scan_and_update(tmp_path, monkeypatch):
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

    updated_paths = []

    def fake_update_file(file_path, beets_config_path, run=None):
        updated_paths.append(file_path)
        return True

    monkeypatch.setattr(si, "_update_file", fake_update_file)

    out_file = tmp_path / "incomplete.lst"
    checked, incomplete, updated, failed = si.scan_and_update(
        str(tmp_path), "/data/beets_config.yaml", str(out_file), max_scan_workers=2)

    assert checked == 4  # all *.mp3/*.flac files, txt excluded
    assert str(complete) not in incomplete
    assert str(missing_cover) in incomplete
    assert str(missing_tags) in incomplete
    assert str(corrupt) not in incomplete
    assert sorted(updated_paths) == sorted([str(missing_cover), str(missing_tags)])
    assert updated == 2
    assert failed == 0
    assert set(out_file.read_text().splitlines()) == {str(missing_cover), str(missing_tags)}


def test_scan_and_update_reports_directory_listing_errors(tmp_path, monkeypatch, capsys):
    """A transient error listing a subdirectory (common on flaky network
    mounts) must be reported, not silently swallowed, and must not abort
    the scan."""

    def fake_walk(path, onerror=None):
        onerror(OSError("Permission denied: some/subdir"))
        return iter([])

    monkeypatch.setattr(si.os, "walk", fake_walk)

    out_file = tmp_path / "incomplete.lst"
    checked, incomplete, updated, failed = si.scan_and_update(
        str(tmp_path), "/data/beets_config.yaml", str(out_file))

    captured = capsys.readouterr()
    assert "could not list directory" in captured.err
    assert checked == 0
    assert incomplete == []
    assert updated == 0
    assert failed == 0
