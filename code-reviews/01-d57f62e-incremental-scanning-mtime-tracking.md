# Code Review: d57f62e — feat: implement incremental scanning with mtime tracking

## Summary

This commit adds an SQLite-backed mtime cache to `scan_incomplete.py` so that files whose modification time hasn't changed since the last run are skipped, with `update_music_metadata.sh` wiring a persistent `MTIME_DB` path through as an optional third CLI argument. The mechanism itself (parameterized SQL, opt-in via `mtime_db_path=None`, four new focused tests, a README section) is implemented cleanly and doesn't regress existing behavior when the new argument is omitted. However, there is one significant logic bug: the mtime is recorded for a file *whenever it is scanned*, regardless of whether it was found complete or incomplete — so a file that beets subsequently fails to fix (no match, network hiccup, etc.) will be silently skipped on every future run even though it is still missing metadata, defeating the tool's core purpose for exactly the cases that need repeated attempts. There is also a real exception-handling gap around the new SQLite calls. Recommend fixing the tracking-on-failure bug and broadening exception handling before this ships.

## Files Changed

- `scan_incomplete.py` — adds `init_mtime_db()`, `get_tracked_mtime()`, `update_mtime_tracking()`; `find_incomplete()` gains an optional `mtime_db_path` parameter that skips files whose current mtime matches the tracked one; `main()` reads an optional third `sys.argv` entry for the DB path.
- `update_music_metadata.sh` — adds `MTIME_DB="$WORK_DIR/mtime_tracking.db"` and passes it as a third argument to `scan_incomplete.py`; log message updated to mention "incremental mode".
- `tests/test_scan_incomplete.py` — adds 4 tests: DB schema creation, get/update roundtrip, skip-when-unchanged, rescan-when-changed.
- `README.md` — documents incremental scanning behavior, DB location/persistence, and adds "parallel scanning" to the open-items list.

## Findings

### Critical

**1. Files that remain incomplete after a failed/skipped tagging attempt become permanently invisible to future scans.**

`scan_incomplete.py:144-155`:
```python
missing_cover = not has_cover(mf)
missing_tags = not has_basic_tags(mf)
if missing_cover or missing_tags:
    incomplete.append(path)

# Update mtime tracking after checking the file
if conn:
    try:
        mtime = os.path.getmtime(path)
        update_mtime_tracking(conn, path, mtime)
    except OSError:
        pass
```
The mtime is recorded unconditionally for every file that gets scanned — including files just added to `incomplete`. `find_incomplete()` runs *before* beets attempts to fix anything (see `update_music_metadata.sh:170-183`, where `beet import -q -s "$file"` runs only after the scan has already returned and closed the DB connection). If beets fails to find a confident match (the script explicitly tolerates this: `run_logged beet ... || err "WARNING: Could not automatically tag '$file' (no confident match found)."`), the file's on-disk mtime never changes, so on the *next* run `current_mtime == tracked_mtime` at line 129 and the file is skipped — even though it is still missing cover art/tags.

Concrete failure scenario: AcoustID rate-limits or the network mount hiccups during `beet import` for `track.mp3`. Run 1 flags it incomplete and records its mtime. Run 2 (and every run thereafter) silently skips it forever, with no warning to the user that a file was "given up on." This directly contradicts the feature's own promise in the README ("Subsequent runs: Only scans files that have been modified since the last run") and undermines the tool's stated purpose of finding and fixing incomplete metadata.

Suggested fix: only persist the mtime tracking entry for files found *complete* (i.e., move the `update_mtime_tracking` call inside an `else` branch of the `if missing_cover or missing_tags` check, or — better — have tracking reflect confirmed-fixed state, updated after `update_music_metadata.sh` successfully processes the file, not at detection time).

### High

**2. SQLite exceptions are not caught, so a single DB error can crash the whole scan and lose all accumulated results.**

`scan_incomplete.py:125-134` and `150-155`:
```python
if conn:
    try:
        current_mtime = os.path.getmtime(path)
        tracked_mtime = get_tracked_mtime(conn, path)
        if tracked_mtime is not None and current_mtime == tracked_mtime:
            continue
    except OSError as e:
        print(f"WARN: could not stat {path} ({e})", file=sys.stderr)
        continue
...
if conn:
    try:
        mtime = os.path.getmtime(path)
        update_mtime_tracking(conn, path, mtime)
    except OSError:
        pass
```
Both blocks only catch `OSError`, which covers `os.path.getmtime()` failures. `get_tracked_mtime()` / `update_mtime_tracking()` execute SQL through `sqlite3`, whose exceptions (`sqlite3.OperationalError` for a locked/corrupt DB, disk-full during `conn.commit()`, etc.) are **not** subclasses of `OSError`. Such an error — plausible given this tool's target environment of flaky/network-mounted storage where `$WORK_DIR`/`/data` could itself be network-backed — will propagate uncaught out of `find_incomplete()`, aborting the entire walk. Because `main()` only writes `out_file` after the full walk completes (`scan_incomplete.py:169-172`), a crash partway through a multi-hour first scan of a large library discards every incomplete-file result found so far, and also skips `conn.close()` at line 157-158 (unclosed connection). Compare this to the existing, deliberately broad `except Exception as e` used for `MutagenFile(path)` at line 138 — the new DB code is inconsistent and narrower than the pattern it sits next to.

Suggested fix: catch `(OSError, sqlite3.Error)` in both blocks, and wrap the `conn` lifecycle in `try/finally` so `conn.close()` always runs.

**3. mtime-equality skip logic is fragile on exactly the mount types this tool targets.**

`scan_incomplete.py:127-131`. The script's own header comment describes it as scanning "a (e.g. network-mounted) music folder" (`update_music_metadata.sh:5`), and the README documents specific "Supported mount types" compatibility concerns (added in an earlier commit, `a3aa0c2`). Network filesystems (SMB in particular, and some NFS client caching configurations) can report coarse mtime resolution (as low as 1-2 seconds) or serve a cached/stale `stat()` result for a short window after a remote write. A file modified twice in quick succession, or read back through client-side attribute caching, can appear to have an unchanged mtime and be incorrectly skipped by `current_mtime == tracked_mtime`. This isn't purely hypothetical given the project's explicit design target; worth at least documenting as a known limitation, if not mitigating (e.g., comparing with a tolerance, or also checking file size).

### Medium

**4. Per-file `conn.commit()` adds unbatched fsync overhead on the largest/slowest scan.**

`scan_incomplete.py:50` (`update_mtime_tracking`) calls `conn.commit()` on every single newly-scanned or changed file, with no batching. On the first run of a large library — the exact case the README calls out as slow ("may take a while") — this adds one synchronous commit per file on top of the mutagen read, working against the feature's own goal of being fast. Consider batching commits (e.g., every N files, or one commit at the end of `find_incomplete()` with an explicit transaction), trading a little crash-durability for meaningfully less I/O.

**5. Redundant/TOCTOU-prone double `getmtime()` call per scanned file.**

`os.path.getmtime(path)` is read once at line 127 to decide whether to skip, and read again at line 152 — after the (potentially slow) `MutagenFile(path)` read and tag inspection — to persist the tracked value. If the file changes between these two reads (e.g., another process touches it while beets/mutagen work is happening), the mtime that gets stored won't correspond to the content that was actually inspected, silently producing an inconsistent tracking entry. Capture `current_mtime` once near the top of the per-file loop and reuse it in both places.

**6. No pruning of stale tracking rows.**

`file_mtime_tracking` rows are never removed for deleted, moved, or renamed files. Every rename/move creates a new row while the old path's row lingers indefinitely. Not incorrect, but an unbounded-growth/no-reconciliation gap worth a follow-up (out of scope to require it in this commit, but worth flagging since the DB is introduced here).

### Low / Nitpicks

**7. Misleading "checked" count/log message in incremental mode.**

`scan_incomplete.py:121` increments `total` for every audio-extension file, including ones skipped via the mtime match at line 129-131 (which never reach `MutagenFile`). The final `main()` output ("`{total} audio files checked, {len(incomplete)} incomplete.`", line 173) and the docstring's "`total_checked`" naming (line 105) both imply every counted file was actually inspected, which isn't true once incremental mode kicks in. Consider a separate `skipped` counter or rewording the message to avoid confusing users about how much work was actually done.

**8. Trailing whitespace in docstring.**

`scan_incomplete.py:106` has a trailing-whitespace blank line inside the `find_incomplete` docstring (`"""Walks music_dir...\n    \n    If mtime_db_path...`). Cosmetic, but inconsistent with the rest of the file's style.

**9. Test naming is slightly misleading.**

In `tests/test_scan_incomplete.py`, `test_find_incomplete_incremental_mode_skips_unchanged` names its second file `changed`, but that file was never pre-tracked at all (it's a brand-new/untracked file, not a previously-tracked-then-modified one). The actual "previously tracked, then modified" scenario is a different case, covered separately by `test_find_incomplete_incremental_detects_file_changes`. Renaming to something like `untracked` would avoid confusing the two scenarios when reading the test file later.

## Test Coverage Assessment

The four new tests are focused and use meaningful (non-tautological) assertions — `test_init_mtime_db` checks the table actually exists via `sqlite_master`, `test_mtime_tracking_get_and_update` checks both the "not yet tracked → None" and the roundtrip cases, and the two `find_incomplete` integration tests correctly assert on `call_count`/`scan_count` to prove mutagen was or wasn't invoked, not just on the returned lists.

That said, there is a real gap: **no test exercises the Critical bug above.** All four tests either manually pre-populate the DB to simulate "already scanned" state, or check a single scan in isolation — none of them call `find_incomplete()` twice in a row against a file that *remains* incomplete (i.e., the realistic "beets didn't fix it" case) to check whether it's still reported the second time. Such a test (scan once with a `FakeMF` that's missing tags, don't change the file, scan again, assert the file is *still* in `incomplete`) would currently fail and would have caught the bug described in Critical Finding 1 before it shipped.

Also missing: any test around the sqlite-exception-handling gap (Finding 2) — e.g., a corrupt/locked DB file — and no test verifying behavior when the DB's parent directory doesn't exist (mitigated in practice by `update_music_metadata.sh`'s `mkdir -p "$WORK_DIR"`, but not exercised at the Python-module level, which is supposed to be independently testable per the module's own docstring).

## Positive Notes

- All new SQL uses parameterized queries (`?` placeholders) — no SQL injection risk despite building queries with string filepaths that could theoretically contain unusual characters.
- `filepath TEXT PRIMARY KEY` correctly prevents duplicate tracking rows and gives cheap lookups without needing an extra index.
- The new `mtime_db_path=None` default keeps `find_incomplete()` fully backward compatible — existing callers/tests that don't pass the third argument are unaffected.
- New functions (`init_mtime_db`, `get_tracked_mtime`, `update_mtime_tracking`) are small, single-purpose, and easy to unit test in isolation, consistent with the module's stated "kept as a standalone module so ... unit testable" design goal.
- README was updated in the same commit with a concrete, accurate-sounding (modulo Finding 1) description of first-run vs. subsequent-run behavior and where the DB lives.
- Existing `OSError` handling around `os.path.getmtime` degrades gracefully (warns and continues) rather than crashing on a single bad stat call.

## Recommendations

1. **(Critical)** Only persist a file's mtime as "tracked" when it was found complete, or otherwise decouple tracking from mid-pipeline detection so files beets fails to fix keep getting re-surfaced on subsequent runs. Add a regression test that scans an incomplete file twice in a row (without modifying it) and asserts it's still reported both times.
2. **(High)** Catch `sqlite3.Error` (not just `OSError`) around both DB-touching try blocks, and wrap the connection lifecycle in `try/finally` so `conn.close()` runs even on failure.
3. **(High)** Document (or mitigate) the mtime-resolution/caching risk on network mounts, given this is the project's explicit target environment.
4. **(Medium)** Batch `conn.commit()` calls instead of committing once per file, to avoid adding per-file fsync overhead to the (already slow) first full scan.
5. **(Medium)** Capture `os.path.getmtime(path)` once per file and reuse the value instead of calling it twice.
6. **(Low)** Tighten the "audio files checked" wording/counter so it doesn't overstate how much work incremental mode actually did; fix the stray trailing whitespace in the `find_incomplete` docstring; rename the misleading `changed` variable in `test_find_incomplete_incremental_mode_skips_unchanged`.
