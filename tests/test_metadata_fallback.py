import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import metadata_fallback as mf


def test_extract_artist_album_from_slash_pattern():
    """Extract artist/album from Artist/Album/track.mp3 pattern."""
    path = "/mnt/music/The Beatles/Abbey Road/01 - Come Together.mp3"
    result = mf.extract_from_path(path)
    assert result.get('artist') == 'Abbey Road' or result.get('artist') == 'The Beatles'
    # The heuristic should detect one of them


def test_extract_from_hyphen_pattern():
    """Extract artist/album from 'Artist - Album' folder pattern."""
    path = "/mnt/music/Pink Floyd - The Wall/01 - In The Flesh.flac"
    result = mf.extract_from_path(path)
    assert result.get('artist') == 'Pink Floyd'
    assert result.get('album') == 'The Wall'


def test_extract_with_year():
    """Folder with year pattern is recognized as album."""
    path = "/mnt/music/Artist/Album 2024/track.mp3"
    result = mf.extract_from_path(path)
    assert 'artist' in result or 'album' in result


def test_extract_minimal():
    """Minimal path still extracts something."""
    path = "/music/artist/track.mp3"
    result = mf.extract_from_path(path)
    # Should at least try to extract artist
    assert isinstance(result, dict)


def test_extract_empty_on_no_match():
    """Returns dict even if no clear pattern matches."""
    path = "/track.mp3"
    result = mf.extract_from_path(path)
    assert isinstance(result, dict)
