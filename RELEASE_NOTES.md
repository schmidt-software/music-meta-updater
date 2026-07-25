Release notes
=============

Unreleased changes:

- Parallel updater workers: added support for UPDATER_WORKERS (env var, default 1).
  Multiple updater threads can now perform per-file pre-processing concurrently while
  beets subprocesses (import/fetchart/embedart) are serialized with a reentrant lock
  to avoid concurrent SQLite writes. This reduces idle time spent preparing items
  and can speed up runs on large libraries, especially when SCAN_WORKERS is high.

Notes:
- Default behavior unchanged (UPDATER_WORKERS=1) to preserve prior safety.
- Tune UPDATER_WORKERS > 1 only if you have CPU and I/O capacity; beets DB writes
  remain single-threaded internally to keep the library safe.
