#!/usr/bin/env python3
"""Scans a music folder for audio files missing cover art or basic tags.

Used by update_music_metadata.sh; kept as a standalone module so the
detection logic can be unit tested independently of the shell script.
"""
import sys
import os
import time
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


def _on_walk_error(err):
    # Network mounts (NFS/SFTP/S3 etc.) can throw transient errors while
    # listing a directory; report them instead of silently skipping the
    # subtree (os.walk's default behavior).
    print(f"WARN: could not list directory ({err})", file=sys.stderr)


def _print_progress(checked, incomplete_count, start_time, last_print_time,
                     count_interval=100, time_interval=5.0):
    """Prints a "checked so far" status line every count_interval files or
    every time_interval seconds, whichever comes first.

    No total/percentage: computing one would need a full pre-pass walk of
    music_dir, which on network mounts (NFS/SFTP/S3/SMB) can be as slow as
    the scan itself and would leave the scan silent until it finished. This
    also always calls print(..., flush=True) instead of relying on the
    Dockerfile's PYTHONUNBUFFERED=1 to reach the console through
    update_music_metadata.sh's run_logged/tee pipeline.
    """
    now = time.monotonic()
    if checked and (checked % count_interval == 0 or now - last_print_time >= time_interval):
        elapsed = now - start_time
        print(f"Scanning: {checked} files checked so far "
              f"({incomplete_count} incomplete, {elapsed:.0f}s elapsed)", flush=True)
        return now
    return last_print_time


def find_incomplete(music_dir):
    """Walks music_dir and returns (total_checked, [incomplete_paths])."""
    incomplete = []
    checked = 0
    start_time = time.monotonic()
    last_print_time = start_time
    for root, _dirs, files in os.walk(music_dir, onerror=_on_walk_error):
        last_print_time = _print_progress(checked, len(incomplete), start_time, last_print_time)
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            path = os.path.join(root, fname)
            try:
                mf = MutagenFile(path)
            except Exception as e:
                print(f"WARN: could not read {path} ({e})", file=sys.stderr, flush=True)
                mf = None
            else:
                if mf is None:
                    print(f"WARN: unknown/corrupt format: {path}", file=sys.stderr, flush=True)

            if mf is not None:
                missing_cover = not has_cover(mf)
                missing_tags = not has_basic_tags(mf)
                if missing_cover or missing_tags:
                    incomplete.append(path)

            checked += 1
            last_print_time = _print_progress(checked, len(incomplete), start_time, last_print_time)
    return checked, incomplete


def main():
    music_dir, out_file = sys.argv[1], sys.argv[2]
    total, incomplete = find_incomplete(music_dir)
    with open(out_file, "w") as f:
        for p in incomplete:
            f.write(p + "\n")
    print(f"{total} audio files checked, {len(incomplete)} incomplete.", flush=True)


if __name__ == "__main__":
    main()
