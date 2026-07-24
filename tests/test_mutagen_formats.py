import tempfile
import os
import types
import sys

# Ensure repo root is on sys.path for direct module imports during pytest -k
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import scan_incomplete as si


def test_apply_fallback_tags_flac_monkeypatch(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix='.flac', delete=False)
    path = tmp.name
    tmp.close()

    saved = {'called': False}

    class FakeFLAC:
        def __init__(self, p):
            assert p == path
            saved['init'] = True
            self.tags = {}
        def save(self):
            saved['called'] = True

    # Ensure mutagen package and submodule entries exist for the import
    import types as _types
    mod = _types.ModuleType('mutagen')
    sub = _types.ModuleType('mutagen.flac')
    sub.FLAC = FakeFLAC
    mod.flac = sub
    monkeypatch.setitem(__import__('sys').modules, 'mutagen', mod)
    monkeypatch.setitem(__import__('sys').modules, 'mutagen.flac', sub)

    res = si._apply_fallback_tags(path, {'artist': 'A1', 'album': 'B1'})
    # Either init or save should have been attempted for the format-specific handler
    assert saved.get('init') is True or saved.get('called') is True

    try:
        os.unlink(path)
    except Exception:
        pass


def test_apply_fallback_tags_mp4_monkeypatch(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix='.m4a', delete=False)
    path = tmp.name
    tmp.close()

    saved = {'called': False}

    class FakeMP4:
        def __init__(self, p):
            assert p == path
            saved['init'] = True
            self.tags = {}
        def save(self):
            saved['called'] = True

    # Ensure mutagen package and submodule entries exist for the import
    import types as _types
    mod = _types.ModuleType('mutagen')
    sub = _types.ModuleType('mutagen.mp4')
    sub.MP4 = FakeMP4
    mod.mp4 = sub
    monkeypatch.setitem(__import__('sys').modules, 'mutagen', mod)
    monkeypatch.setitem(__import__('sys').modules, 'mutagen.mp4', sub)

    res = si._apply_fallback_tags(path, {'artist': 'A1', 'album': 'B1'})
    # Either init or save should have been attempted for the format-specific handler
    assert saved.get('init') is True or saved.get('called') is True

    try:
        os.unlink(path)
    except Exception:
        pass
