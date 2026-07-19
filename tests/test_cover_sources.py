import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cover_sources as cs


def test_validate_cover_sources_valid():
    """Valid cover source chains are accepted."""
    valid_chains = [
        ["musicbrainz"],
        ["musicbrainz", "amazon"],
        ["musicbrainz", "amazon", "discogs"],
        ["musicbrainz", "amazon", "discogs", "local", "placeholder"],
    ]
    for chain in valid_chains:
        valid, error = cs.validate_cover_sources(chain)
        assert valid is True, f"Expected valid for {chain}, got: {error}"


def test_validate_cover_sources_invalid():
    """Invalid cover source chains are rejected."""
    invalid_chains = [
        [],                                    # Empty
        ["invalid"],                           # Unknown source
        ["musicbrainz", "fake"],              # Mixed valid/invalid
    ]
    for chain in invalid_chains:
        valid, error = cs.validate_cover_sources(chain)
        assert valid is False, f"Expected invalid for {chain}"


def test_validate_cover_sources_not_list():
    """Non-list inputs are rejected."""
    valid, error = cs.validate_cover_sources("musicbrainz")
    assert valid is False


def test_parse_cover_sources_string_empty():
    """Empty string returns default sources."""
    sources, error = cs.parse_cover_sources_string("")
    assert error is None
    assert sources == cs.DEFAULT_COVER_SOURCES


def test_parse_cover_sources_string_valid():
    """Valid source strings are parsed correctly."""
    sources, error = cs.parse_cover_sources_string("musicbrainz,amazon,discogs")
    assert error is None
    assert sources == ["musicbrainz", "amazon", "discogs"]


def test_parse_cover_sources_string_invalid():
    """Invalid source strings return errors."""
    sources, error = cs.parse_cover_sources_string("musicbrainz,invalid")
    assert error is not None
    assert sources is None


def test_generate_beets_fetchart_config():
    """Beets fetchart config is generated correctly."""
    sources = ["musicbrainz", "amazon"]
    config = cs.generate_beets_fetchart_config(sources)
    assert "fetchart:" in config
    assert "MusicBrainz" in config
    assert "Amazon" in config


def test_generate_beets_fetchart_config_invalid():
    """Invalid sources raise ValueError."""
    try:
        cs.generate_beets_fetchart_config(["invalid"])
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_describe_cover_sources():
    """Human-readable descriptions are generated."""
    sources = ["musicbrainz", "amazon", "discogs"]
    desc = cs.describe_cover_sources(sources)
    assert "MusicBrainz" in desc
    assert "Amazon" in desc
    assert "Discogs" in desc
    assert "→" in desc  # Chain indicator


def test_describe_cover_sources_with_placeholders():
    """Descriptions include placeholder sources."""
    sources = ["musicbrainz", "placeholder"]
    desc = cs.describe_cover_sources(sources)
    assert "Placeholder" in desc


def test_default_vs_extended_sources():
    """Default and extended chains are different."""
    assert cs.DEFAULT_COVER_SOURCES != cs.EXTENDED_COVER_SOURCES
    assert "local" not in cs.DEFAULT_COVER_SOURCES
    assert "local" in cs.EXTENDED_COVER_SOURCES
