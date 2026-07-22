# Changelog

All notable changes to this project are documented here.

## Unreleased

- Fix: persist mtime only after successful beets update (fix/cr01)
  - Prevents files that couldn't be fully updated from being permanently
    skipped in future scans.
  - Operators: the mtime DB (`file_mtime_tracking`) will be populated only
    after successful updates; run an initial scan to populate the DB if
    you want to enable incremental skipping immediately.

- Fix: batch mtime DB commits to reduce IO and contention (fix/cr02)

- Fix: add `MTIME_TOLERANCE` and include file size in tracking comparisons
  to reduce false-positive skips on coarse-resolution filesystems (fix/cr03)

- Fix: prune stale tracking rows for files that no longer exist (fix/cr04)

- Fix: handle sqlite errors gracefully so scans continue even when the
  tracking DB is unavailable (fix/cr05)

- Chore: small docstring/testability improvements and lightweight
  cover-tracking helpers (fix/cr06)

