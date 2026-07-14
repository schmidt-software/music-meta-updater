#!/usr/bin/env python3
"""Scans a music folder for audio files missing cover art or basic tags.

Used by update_music_metadata.sh; kept as a standalone module so the
detection logic can be unit tested independently of the shell script.
"""
import sys
import os
from mutagen import File as MutagenFile

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aac"}


def has_cover(mf):
    try:
        if hasattr(mf, "tags") and mf.tags is not None:
            # ID3 (mp3)
            if any(k.startswith("APIC") for k in mf.tags.keys()):
                return True
            # MP4/M4A
            if "covr" in mf.tags:
                return True
            # FLAC
            if hasattr(mf, "pictures") and mf.pictures:
                return True
            # Vorbis/Opus (embedded as base64 in metadata_block_picture)
            if "metadata_block_picture" in mf.tags:
                return True
    except Exception:
        pass
    return False


def has_basic_tags(mf):
    try:
        easy = MutagenFile(mf.filename, easy=True) if hasattr(mf, "filename") else None
    except Exception:
        easy = None
    tags = easy.tags if easy is not None else getattr(mf, "tags", None)
    if not tags:
        return False

    def get(key):
        try:
            val = tags.get(key)
            if isinstance(val, list):
                return val[0] if val else None
            return val
        except Exception:
            return None

    title = get("title")
    artist = get("artist")
    album = get("album")
    return bool(title) and bool(artist) and bool(album)


def find_incomplete(music_dir):
    """Walks music_dir and returns (total_checked, [incomplete_paths])."""
    incomplete = []
    total = 0
    for root, _dirs, files in os.walk(music_dir):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            total += 1
            path = os.path.join(root, fname)
            try:
                mf = MutagenFile(path)
            except Exception as e:
                print(f"WARN: could not read {path} ({e})", file=sys.stderr)
                continue
            if mf is None:
                print(f"WARN: unknown/corrupt format: {path}", file=sys.stderr)
                continue
            missing_cover = not has_cover(mf)
            missing_tags = not has_basic_tags(mf)
            if missing_cover or missing_tags:
                incomplete.append(path)
    return total, incomplete


def main():
    music_dir, out_file = sys.argv[1], sys.argv[2]
    total, incomplete = find_incomplete(music_dir)
    with open(out_file, "w") as f:
        for p in incomplete:
            f.write(p + "\n")
    print(f"{total} audio files checked, {len(incomplete)} incomplete.")


if __name__ == "__main__":
    main()
