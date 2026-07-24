import os
import sys
import tempfile
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import scan_incomplete as si


def test_smoke_fallback_applies_and_beets_imports(monkeypatch):
    # Create temp music dir and a fake mp3 file
    tmpdir = tempfile.TemporaryDirectory()
    music_dir = tmpdir.name
    fpath = os.path.join(music_dir, 'Artist', 'Album')
    os.makedirs(fpath, exist_ok=True)
    file_path = os.path.join(fpath, '01 - Track.mp3')
    with open(file_path, 'wb') as f:
        f.write(b'')

    # Ensure MUSIC_DIR env used by fallback
    monkeypatch.setenv('MUSIC_DIR', music_dir)
    monkeypatch.setenv('FALLBACK_APPLY', 'true')
    monkeypatch.setenv('FALLBACK_BEETS_RESCAN', 'true')

    # Fake MutagenFile to allow tag writes
    class FakeMutagen:
        def __init__(self, path, easy=False):
            self.filename = path
            self.tags = {}
            self._saved = False
        def __setitem__(self, k, v):
            self.tags[k] = v
        def save(self):
            self._saved = True

    monkeypatch.setattr(si, 'MutagenFile', FakeMutagen)

    # Mock subprocess.run to simulate beets behavior: first import fails, second succeeds
    call_counts = {'import': 0}

    def fake_run(args, **kwargs):
        # args is like ['beet', '-v', '-c', config, 'import', '-q', '-s', file]
        if 'import' in args:
            call_counts['import'] += 1
            if call_counts['import'] == 1:
                return SimpleNamespace(returncode=1)
            else:
                return SimpleNamespace(returncode=0)
        # fetchart/embedart/no-op
        return SimpleNamespace(returncode=0)

    # Ensure _update_file uses fake_run by wrapping it with run=fake_run
    orig_update = si._update_file
    def wrapped_update(file_path, beets_config_path, run=None, library_root=None):
        return orig_update(file_path, beets_config_path, run=fake_run, library_root=library_root)
    monkeypatch.setattr(si, '_update_file', wrapped_update)

    # Run scan_and_update with a small pool to exercise workflow
    out_file = os.path.join(music_dir, 'incomplete.txt')

    # Use max_scan_workers=1 to keep deterministic order
    checked, incomplete, updated, failed = si.scan_and_update(music_dir, beets_config_path='/dev/null', out_file=out_file, max_scan_workers=1)

    # After run: updated should be >= 1 (because fallback + rescan succeeded)
    assert updated >= 1
    # Import should have been attempted twice (first failed, second after fallback)
    assert call_counts['import'] >= 2

    tmpdir.cleanup()
