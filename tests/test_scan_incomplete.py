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
