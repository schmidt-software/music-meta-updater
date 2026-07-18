#!/usr/bin/env python3
"""
Tagging mode support for selective metadata updates.

Modes:
  - cover_only: Only fetch/embed cover art, skip tag updates
  - strict: Only tag files with high-confidence matches
  - aggressive: Tag files with lower-confidence matches  
  - default: Standard behavior (balanced)
"""

# Tagging mode thresholds for beets matching
TAGGING_MODES = {
    "cover_only": {
        "description": "Only fetch/embed cover art, skip tag updates",
        "skip_tagging": True,
        "skip_cover": False,
        "match_strength": None,  # N/A
    },
    "strict": {
        "description": "Only tag with high-confidence matches",
        "skip_tagging": False,
        "skip_cover": False,
        "match_strength": "strong",  # beets strong_rec_thresh
        "strong_rec_thresh": 0.95,  # Very high threshold
    },
    "aggressive": {
        "description": "Tag with lower-confidence matches",
        "skip_tagging": False,
        "skip_cover": False,
        "match_strength": "weak",
        "strong_rec_thresh": 0.70,  # Lower threshold
    },
    "default": {
        "description": "Balanced: tag medium-confidence matches",
        "skip_tagging": False,
        "skip_cover": False,
        "match_strength": "balanced",
        "strong_rec_thresh": 0.85,  # Default beets value
    },
}


def validate_mode(mode):
    """Validate tagging mode is recognized."""
    return mode in TAGGING_MODES


def get_mode_config(mode):
    """Get configuration for a tagging mode."""
    return TAGGING_MODES.get(mode, TAGGING_MODES["default"])


def find_incomplete_by_mode(music_dir, mode, mtime_db_path=None, error_db_path=None, num_workers=None):
    """Find incomplete files based on tagging mode.
    
    - cover_only: Only files missing covers
    - strict/aggressive/default: Files missing tags OR covers (standard behavior)
    
    Returns (total_checked, [incomplete_paths], error_telemetry, mode_config)
    """
    import scan_incomplete as si
    
    if not validate_mode(mode):
        raise ValueError(f"Unknown tagging mode: {mode}. Valid modes: {', '.join(TAGGING_MODES.keys())}")
    
    mode_config = get_mode_config(mode)
    total, incomplete, error_telemetry = si.find_incomplete(music_dir, mtime_db_path, error_db_path, num_workers)
    
    # For cover_only mode, filter to only files missing covers
    if mode_config["skip_tagging"]:
        cover_only = []
        for filepath in incomplete:
            try:
                mf = si.MutagenFile(filepath)
                if mf and not si.has_cover(mf):
                    cover_only.append(filepath)
            except Exception:
                pass  # If error reading, skip it
        incomplete = cover_only
    
    return total, incomplete, error_telemetry, mode_config
