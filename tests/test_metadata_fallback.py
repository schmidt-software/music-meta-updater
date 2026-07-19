import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import metadata_fallback as mf


def test_extract_artist_album_from_slash_pattern():
    """Extract artist/album from Artist/Album/track.mp3 pattern."""
    path = "/mnt/music/The Beatles/Abbey Road/01 - Come Together.mp3"
    result = mf.extract_from_path(path, library_root="/mnt/music")
    assert result == {'artist': 'The Beatles', 'album': 'Abbey Road'}


def test_extract_from_hyphen_pattern():
    """Extract artist/album from 'Artist - Album' folder pattern."""
    path = "/mnt/music/Pink Floyd - The Wall/01 - In The Flesh.flac"
    result = mf.extract_from_path(path)
    assert result.get('artist') == 'Pink Floyd'
    assert result.get('album') == 'The Wall'


def test_extract_hyphen_pattern_with_disc_subfolder():
    """A disc/side subfolder nested under an 'Artist - Album' folder must
    not be mistaken for the album itself (regression test)."""
    path = "/mnt/music/Pink Floyd - The Wall/Side A/01 - In The Flesh.flac"
    result = mf.extract_from_path(path)
    assert result == {'artist': 'Pink Floyd', 'album': 'The Wall'}


def test_extract_with_year():
    """Folder with year pattern is recognized as album."""
    path = "/mnt/music/Artist/Album 2024/track.mp3"
    result = mf.extract_from_path(path)
    assert result == {'artist': 'Artist', 'album': 'Album 2024'}


def test_extract_artist_only_with_library_root():
    """With a library_root, a single folder level below it is correctly
    recognized as artist-only (no album) - regression test for the
    previously-unreachable 'Artist/track.mp3' pattern."""
    path = "/mnt/music/artist/track.mp3"
    result = mf.extract_from_path(path, library_root="/mnt/music")
    assert result == {'artist': 'artist'}


def test_extract_ignores_hidden_folder_with_library_root():
    """A leading hidden/dot folder isn't treated as an artist name."""
    path = "/mnt/music/.hidden/track.mp3"
    result = mf.extract_from_path(path, library_root="/mnt/music")
    assert result == {}


def test_extract_minimal():
    """Minimal path still extracts something (best-effort guess without
    a library_root to anchor folder depth)."""
    path = "/music/artist/track.mp3"
    result = mf.extract_from_path(path)
    assert isinstance(result, dict)


def test_extract_empty_on_no_match():
    """Returns dict even if no clear pattern matches."""
    path = "/track.mp3"
    result = mf.extract_from_path(path)
    assert isinstance(result, dict)
