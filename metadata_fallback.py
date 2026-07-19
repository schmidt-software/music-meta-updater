#!/usr/bin/env python3
"""Extract fallback artist/album metadata from folder structure, for use
when beets fails to find a confident match.

Implements heuristic-based metadata extraction from folder patterns like:
  Artist/Album/track.mp3
  Artist - Album/track.mp3
  etc.
"""
import os
import re

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

    raw_path_parts = [p for p in raw_path_parts if p and p != os.pardir]
    path_parts = _skip_disc_markers(raw_path_parts)

    metadata = {}
    if not path_parts:
        return metadata

    last_folder = path_parts[-1]

    # Pattern: "Artist - Album"
    if " - " in last_folder:
        parts = last_folder.split(" - ", 1)
        metadata['artist'] = parts[0].strip()
        metadata['album'] = parts[1].strip()
        return metadata

    # Pattern: "Artist/track.mp3" (artist only) - only reachable when we
    # can actually confirm there is exactly one folder level, i.e. a
    # library_root was given.
    if library_root and len(path_parts) == 1:
        if last_folder and not last_folder.startswith('.'):
            metadata['artist'] = last_folder.strip()
        return metadata

    # Pattern: "Artist/Album"
    if len(path_parts) >= 2:
        second_last = path_parts[-2]
        metadata['artist'] = second_last.strip()
        metadata['album'] = last_folder.strip()
        return metadata

    # Last resort (no library_root given, so we can't distinguish an
    # "artist only" folder from a mount-point/library-root segment):
    # use the folder name as artist.
    if last_folder and not last_folder.startswith('.'):
        metadata['artist'] = last_folder.strip()

    return metadata


if __name__ == "__main__":
    # Test
    library_root = "/mnt/music"
    test_paths = [
        "/mnt/music/The Beatles/Abbey Road/01 - Come Together.mp3",
        "/mnt/music/Pink Floyd - The Wall/Side A/01 - In The Flesh.flac",
        "/mnt/music/Various Artists/Compilation Album 2024/track.mp3",
        "/mnt/music/Radiohead/01 - Airbag.mp3",
    ]
    for path in test_paths:
        print(f"{path} -> {extract_from_path(path, library_root=library_root)}")
