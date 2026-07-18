#!/usr/bin/env python3
"""Deduplicate and extract metadata from folder structure for fallback matching.

Implements heuristic-based metadata extraction from folder patterns like:
  Artist/Album/track.mp3
  Artist - Album/track.mp3
  etc.
"""
import os
import re


def extract_from_path(filepath):
    """Extract artist/album from folder structure.
    
    Tries common patterns:
    - /Artist/Album/track.mp3
    - /Artist - Album/track.mp3
    - /Artist/track.mp3 (artist only)
    
    Returns dict with 'artist' and/or 'album' keys, or empty dict if no match.
    """
    dirpath = os.path.dirname(filepath)
    path_parts = dirpath.split(os.sep)
    
    # Try to extract from folder names (last 2 parts before filename)
    metadata = {}
    
    if len(path_parts) >= 2:
        last_folder = path_parts[-1]
        second_last = path_parts[-2] if len(path_parts) >= 2 else None
        
        # Pattern: "Artist - Album"
        if " - " in last_folder:
            parts = last_folder.split(" - ", 1)
            metadata['artist'] = parts[0].strip()
            metadata['album'] = parts[1].strip()
            return metadata
        
        # Pattern: "Artist/Album"
        if second_last:
            # Check if second_last looks like artist, last_folder like album
            # Simple heuristic: album folders often have years or "ep" in them
            if any(x in last_folder.lower() for x in ['ep', 'album', 'lp']) or \
               re.search(r'\d{4}', last_folder):  # contains year
                metadata['artist'] = second_last.strip()
                metadata['album'] = last_folder.strip()
                return metadata
            else:
                # Assume second_last is artist, last is album
                metadata['artist'] = second_last.strip()
                metadata['album'] = last_folder.strip()
                return metadata
    
    # Last resort: use folder name as artist
    if path_parts:
        last_folder = path_parts[-1]
        if last_folder and not last_folder.startswith('.'):
            metadata['artist'] = last_folder.strip()
    
    return metadata


if __name__ == "__main__":
    # Test
    test_paths = [
        "/mnt/music/The Beatles/Abbey Road/01 - Come Together.mp3",
        "/mnt/music/Pink Floyd - The Wall/Side A/01 - In The Flesh.flac",
        "/mnt/music/Various Artists/Compilation Album 2024/track.mp3",
    ]
    for path in test_paths:
        print(f"{path} -> {extract_from_path(path)}")
