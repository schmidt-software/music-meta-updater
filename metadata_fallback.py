#!/usr/bin/env python3
"""Extract fallback artist/album metadata from folder structure, for use
when beets fails to find a confident match.

Implements heuristic-based metadata extraction from folder patterns like:
  Artist/Album/track.mp3
  Artist - Album/track.mp3
  etc.

This module applies NFC Unicode normalization to extracted strings to
avoid mismatches arising from filesystem-specific normalization forms.
"""
import os
import re
import unicodedata

# Disc/side/part/volume subfolders (e.g. "Disc 1", "Side A", "CD2", "Bonus")
# that can sit *underneath* the real artist/album folder in multi-disc or
# box-set releases. These need to be skipped, or "Artist - Album/Disc 1"
# gets misread as artist="Artist - Album", album="Disc 1".
_DISC_MARKER_RE = re.compile(
    r"^(disc|cd|side|part|vol(ume)?|bonus)\s*[0-9]*[a-d]?$", re.IGNORECASE
)


def _skip_disc_markers(path_parts):
    """Drop trailing disc/side/part/volume marker folders so the real
    album/artist folder pair underneath them can still be found."""
    parts = list(path_parts)
    while parts and _DISC_MARKER_RE.match(parts[-1].strip()):
        parts.pop()
    return parts


def _normalize(s):
    if not s:
        return s
    return unicodedata.normalize("NFC", s).strip()


def extract_from_path(filepath, library_root=None):
    """Extract artist/album from folder structure.

    Tries common patterns:
    - Artist/Album/track.mp3
    - Artist - Album/track.mp3
    - Artist/track.mp3 (artist only)

    Disc/side/part/volume subfolders (e.g. "Disc 1", "Side A") are skipped
    so a nested "Artist - Album/Disc 1/track.mp3" layout still resolves to
    the actual artist/album pair instead of the disc subfolder.

    If library_root is given, folder depth is measured relative to it, so
    the "artist only" pattern (exactly one folder below the library root)
    can be told apart from "Artist/Album" (two folders below). Without
    library_root there is no reliable way to tell "artist only" apart from
    an arbitrary path prefix/mount point, so that pattern is not applied.

    Returns dict with 'artist' and/or 'album' keys, or empty dict if no match.
    """
    dirpath = os.path.dirname(filepath)

    if library_root:
        rel = os.path.relpath(dirpath, library_root)
        raw_path_parts = [] if rel == os.curdir else rel.split(os.sep)
    else:
        raw_path_parts = dirpath.split(os.sep)

    # Remove empty / parent segments
    raw_path_parts = [p for p in raw_path_parts if p and p != os.pardir]
    # Remove trailing disc/side markers
    path_parts = _skip_disc_markers(raw_path_parts)

    metadata = {}
    if not path_parts:
        return metadata

    # Work with normalized folder names
    norm_parts = [_normalize(p) for p in path_parts if p and not p.startswith('.')]
    if not norm_parts:
        return metadata

    last_folder = norm_parts[-1]

    # Pattern: "Artist - Album" in a single folder name
    if " - " in last_folder:
        parts = [_normalize(x) for x in last_folder.split(" - ", 1)]
        metadata['artist'] = parts[0]
        metadata['album'] = parts[1]
        return metadata

    # Pattern: "Artist/track.mp3" (artist only) - only reachable when we
    # can actually confirm there is exactly one folder level relative to
    # the library root.
    if library_root and len(norm_parts) == 1:
        metadata['artist'] = last_folder
        return metadata

    # Pattern: "Artist/Album" - take the last two meaningful folders
    if len(norm_parts) >= 2:
        metadata['artist'] = norm_parts[-2]
        metadata['album'] = last_folder
        return metadata

    # Last resort: use last folder as artist
    metadata['artist'] = last_folder
    return metadata


if __name__ == "__main__":
    # Smoke tests
    library_root = "/mnt/music"
    test_paths = [
        "/mnt/music/The Beatles/Abbey Road/01 - Come Together.mp3",
        "/mnt/music/Pink Floyd - The Wall/Side A/01 - In The Flesh.flac",
        "/mnt/music/Various Artists/Compilation Album 2024/track.mp3",
        "/mnt/music/Radiohead/01 - Airbag.mp3",
    ]
    for path in test_paths:
        print(f"{path} -> {extract_from_path(path, library_root=library_root)}")
