import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tagging_modes as tm


def test_validate_mode():
    """Valid tagging modes are recognized."""
    assert tm.validate_mode("default") is True
    assert tm.validate_mode("cover_only") is True
    assert tm.validate_mode("strict") is True
    assert tm.validate_mode("aggressive") is True
    assert tm.validate_mode("invalid_mode") is False


def test_get_mode_config():
    """Mode configurations are retrievable."""
    config = tm.get_mode_config("cover_only")
    assert config["skip_tagging"] is True
    assert config["skip_cover"] is False
    
    config = tm.get_mode_config("strict")
    assert config["skip_tagging"] is False
    assert config["strong_rec_thresh"] == 0.95
    
    config = tm.get_mode_config("aggressive")
    assert config["strong_rec_thresh"] == 0.70
    
    config = tm.get_mode_config("default")
    assert config["strong_rec_thresh"] == 0.85


def test_mode_config_invalid_fallback():
    """Invalid mode falls back to default config."""
    config = tm.get_mode_config("nonexistent")
    assert config == tm.TAGGING_MODES["default"]


def test_tagging_modes_have_descriptions():
    """All modes have descriptions."""
    for mode_name, config in tm.TAGGING_MODES.items():
        assert "description" in config
        assert isinstance(config["description"], str)
        assert len(config["description"]) > 0


def test_cover_only_mode_characteristics():
    """cover_only mode has expected characteristics."""
    config = tm.get_mode_config("cover_only")
    assert config["skip_tagging"] is True
    assert config["skip_cover"] is False
    assert "cover" in config["description"].lower()


def test_strict_mode_high_threshold():
    """strict mode has high matching threshold."""
    strict = tm.get_mode_config("strict")
    aggressive = tm.get_mode_config("aggressive")
    assert strict["strong_rec_thresh"] > aggressive["strong_rec_thresh"]


def test_all_modes_consistently_configured():
    """All modes have consistent configuration structure."""
    required_keys = {"description", "skip_tagging", "skip_cover"}
    for mode_name, config in tm.TAGGING_MODES.items():
        assert required_keys.issubset(config.keys()), f"Mode {mode_name} missing required keys"
