# Copilot instructions for music-meta-updater

Purpose
- Short guide for Copilot sessions: how to build/run/test this repo and important repo-specific conventions.

Build / Test / Lint (repository-specific)
- Docker image: docker compose up --build
- Build image only: docker compose build
- Run one-shot in container (example): MUSIC_HOST_PATH=/path/to/music ACOUSTID_API_KEY=xxxxx docker compose up --build
- Recurring/scheduled run: set SCHEDULE (cron) then docker compose up -d, e.g.
  SCHEDULE="0 2 * * *" MUSIC_HOST_PATH=/path ACOUSTID_API_KEY=xxxxx docker compose up -d
- Direct (no Docker): ensure system deps (python3, ffmpeg, chromaprint), then:
  pip install -r requirements-dev.txt
  MUSIC_DIR=/mnt/music ACOUSTID_API_KEY=abcd1234 ./update_music_metadata.sh

- Tests:
  - Install test deps: pip install -r requirements-dev.txt
  - Full suite: pytest tests/
  - Single test function: pytest tests/test_scan_incomplete.py::test_scan_and_update
  - Single file: pytest tests/test_scan_incomplete.py
  - Use -q / -k for filtering: pytest -q -k "scan_and_update"

- Lint: no repo-specific linter configured. Do not add tools without CI changes.

High-level architecture (big picture)
- update_music_metadata.sh: orchestrator; creates venv, generates beets config, runs scan and emits metrics + optional webhook. Main entry for non-container usage.
- entrypoint.sh: container entrypoint; either runs the script once or validates SCHEDULE and runs supercronic for recurring execution.
- scan_incomplete.py: core scanning logic. Walks MUSIC_DIR, checks audio files (AUDIO_EXTS) concurrently, reports progress via a heartbeat thread, and enqueues incomplete files to a single updater thread which runs beets import/fetchart/embedart for that file. Kept testable and isolated from shell wrapper.
- tagging_modes.py, cover_sources.py: helpers to validate/generate beets configuration snippets (TAGGING_MODE, STRONG_REC_THRESH, COVER_SOURCES).
- metadata_fallback.py: heuristics to extract artist/album from path as a last-resort fallback.
- Dockerfile / docker-compose.yml: containerization and runtime defaults; mounts host music dir to /music and persistent data to /data.

Key conventions and repository-specific patterns
- Env-driven configuration: tune behavior through env vars passed to update_music_metadata.sh or docker-compose: MUSIC_DIR / MUSIC_HOST_PATH, ACOUSTID_API_KEY, SCHEDULE, SCAN_WORKERS, TAGGING_MODE, STRONG_REC_THRESH, COVER_SOURCES, WEBHOOK_URL.
- SCAN_WORKERS defaults to 8 (I/O-bound concurrency); adjust upward for high-latency mounts.
- TAGGING_MODE values: default, strict, aggressive, cover_only. STRONG_REC_THRESH (0.0-1.0) can override mode presets. Use tagging_modes.py for validation.
- COVER_SOURCES is a comma-separated string; valid sources: musicbrainz, amazon, discogs, local, placeholder. cover_sources.py converts to a beets fetchart config and warns when entries are not beets plugins.
- Beets config is generated at runtime into WORK_DIR (default $HOME/.music-metadata-tool or /data in container). The script writes beets_config.yaml and uses it for per-file import/fetch/embed ops.
- Immediate-update pattern: scan_incomplete.py enqueues incomplete files and a single updater thread performs beets operations serially to avoid concurrent sqlite/beets DB writes.
- Metrics/logging: update_music_metadata.sh writes metrics JSON to $WORK_DIR/metrics.json and logs to $WORK_DIR/update.log. Tests and automation can rely on that path.
- Webhook safety: WEBHOOK_URL is vetted (must be https and not point to loopback/private ranges) before sending.
- Tests mock external systems: unit tests mock MutagenFile and subprocess.run so tests run without real audio files or beets installed. Follow existing patterns when adding tests (monkeypatch, FakeMF, FakeCompletedProcess).
- Audio extensions: AUDIO_EXTS in scan_incomplete.py enumerates supported audio files; add new formats there if needed.

Where to look for more details
- README.md: usage examples, env var docs, metrics format, and rationale.
- update_music_metadata.sh: exact runtime flow, venv creation, dependency installs, beets config template and metrics emission.
- scan_incomplete.py and tests/ for detection/update behaviour and test patterns.

Repository AI assistant configs
- No CLAUDE.md, .cursorrules, AGENTS.md, .windsurfrules, CONVENTIONS.md, or other assistant-specific config files found. If adding assistant guidelines, place them under .github/ or root and update this file accordingly.

Notes for Copilot sessions
- Focus on edits that preserve the immediate-update / single-updater-worker pattern; changing that requires coordinating beets sqlite access and updating tests.
- When changing config generation (COVER_SOURCES / TAGGING_MODE), update update_music_metadata.sh and the tests that validate invalid inputs.
- Tests are fast and isolated; run pytest tests/ as the primary verification for logic changes.

