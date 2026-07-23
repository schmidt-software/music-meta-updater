#!/usr/bin/env python3
"""Cover art fallback chain configuration and management.

Implements a configurable fallback chain for cover art fetching:
  1. MusicBrainz Cover Art Archive (CAA) - default source
  2. Amazon Cover Art
  3. Discogs
  4. Local cache (previously downloaded covers)
  5. Placeholder (last resort)
"""

import os
import sys


# Default fallback chain configuration
DEFAULT_COVER_SOURCES = [
    "musicbrainz",      # MusicBrainz Cover Art Archive (CAA)
    "amazon",           # Amazon Cover Art
    "discogs",          # Discogs
]

# Extended chain with local cache and placeholder
EXTENDED_COVER_SOURCES = [
    "musicbrainz",
    "amazon",
    "discogs",
    "local",            # Local cache
    "placeholder",      # Generated placeholder
]


def validate_cover_sources(sources):
    """Validate a list of cover sources.
    
    Valid sources: musicbrainz, amazon, discogs, local, placeholder
    
    Returns (True, None) if valid, (False, error_msg) if invalid.
    """
    valid_sources = {"musicbrainz", "amazon", "discogs", "local", "placeholder"}
    
    if not sources:
        return False, "Cover sources list cannot be empty"
    
    if not isinstance(sources, list):
        return False, "Cover sources must be a list"
    
    for source in sources:
        if source not in valid_sources:
            return False, f"Unknown cover source: {source}. Valid: {', '.join(valid_sources)}"
    
    return True, None


def parse_cover_sources_string(sources_str):
    """Parse a comma-separated string of cover sources.
    
    Args:
        sources_str: "musicbrainz,amazon,discogs" or empty string
    
    Returns:
        (sources_list, error) tuple or (DEFAULT_COVER_SOURCES, None) if empty
    """
    if not sources_str or not sources_str.strip():
        return DEFAULT_COVER_SOURCES, None
    
    sources = [s.strip().lower() for s in sources_str.split(",")]
    valid, error = validate_cover_sources(sources)
    
    if not valid:
        return None, error
    
    return sources, None


def generate_beets_fetchart_config(sources):
    """Generate beets fetchart config section for given sources.
    
    Args:
        sources: List of cover sources in order
    
    Returns:
        YAML config string for beets fetchart section

    Note: "local" and "placeholder" are not beets fetchart plugin
    sources - they represent this project's own local-cache/placeholder
    fallback concept, handled outside of beets (if at all). If present
    in `sources`, they are intentionally excluded from the generated
    config; a warning is printed to stderr so this isn't silent.
    """
    valid, error = validate_cover_sources(sources)
    if not valid:
        raise ValueError(f"Invalid cover sources: {error}")

    # Map source names to beets fetchart source IDs (not human names).
    # Use conservative defaults that are commonly available in beets.
    source_plugins = {
        "musicbrainz": "coverart",   # Cover Art Archive (MusicBrainz)
        "amazon": "amazon",
        # Discogs isn't always provided by upstream beets; map to the
        # AlbumArt.org scraper as a best-effort fallback.
        "discogs": "albumart",
    }

    # Build beets sources list (IDs)
    beets_sources = []
    dropped = []
    for source in sources:
        if source in source_plugins:
            beets_sources.append(source_plugins[source])
        else:
            dropped.append(source)

    if dropped:
        print(
            f"NOTE: cover source(s) {', '.join(dropped)} are not beets "
            "fetchart plugins and were excluded from the generated "
            "fetchart config (handled outside of beets, if at all).",
            file=sys.stderr,
        )

    # If no valid beets sources, use default ID 'coverart'
    if not beets_sources:
        beets_sources = ["coverart"]

    # Render as YAML mapping of source_id: "*" (default matching criteria),
    # preserving a comment with human-readable names for operator clarity.
    sources_yaml = "\n".join([f"    {s}: \"*\"" for s in beets_sources])

    human_readable = ", ".join([s.title() for s in sources if s not in dropped])

    config = f"""# Generated fetchart configuration (requested sources: {human_readable})
fetchart:
  auto: yes
  force: no
  enforce_ratio: no
  sources:
{sources_yaml}
  # Fallback behavior:
  # - cover_only: skip_cover=false (try all sources)
  # - strict/aggressive/default: all sources enabled
"""

    return config


def describe_cover_sources(sources):
    """Generate human-readable description of cover sources chain.
    
    Args:
        sources: List of cover sources
    
    Returns:
        Description string
    """
    descriptions = {
        "musicbrainz": "MusicBrainz Cover Art Archive",
        "amazon": "Amazon Cover Art",
        "discogs": "Discogs",
        "local": "Local Cache",
        "placeholder": "Generated Placeholder",
    }
    
    parts = []
    for source in sources:
        if source in descriptions:
            parts.append(descriptions[source])
    
    return " → ".join(parts) if parts else "No sources configured"


if __name__ == "__main__":
    # Test
    test_chains = [
        DEFAULT_COVER_SOURCES,
        EXTENDED_COVER_SOURCES,
        ["musicbrainz", "amazon"],
        ["invalid"],
    ]
    
    for chain in test_chains:
        valid, error = validate_cover_sources(chain)
        if valid:
            print(f"✓ {describe_cover_sources(chain)}")
        else:
            print(f"✗ {error}")
