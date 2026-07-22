# Code Review: 4a02c37 — feat: intelligent retry & error handling for flaky network mounts

## Summary

This commit adds exponential-backoff retries, exception classification, a SQLite-backed error/blacklist store, and error telemetry to `scan_incomplete.py`, and threads an `error_db_path` parameter (and a new 3-tuple return) through `_check_file_with_retry`/`find_incomplete`/`main()`. The individual SQLite helpers (`init_error_db`, `is_blacklisted`, `record_error`, `get_error_type`) are clean and use parameterized queries, and the retry sleeps correctly happen inside worker threads rather than the main thread. However, the feature has three substantive defects: (1) `update_music_metadata.sh` — the only real caller of `scan_incomplete.py`'s CLI — was **not** updated for the new positional argument order, silently breaking the `SCAN_WORKERS` override and misusing a numeric string as an error-DB path; (2) the `timeout` branch in `get_error_type()` is unreachable dead code because `OSError` is checked before `TimeoutError` (a subclass), which the commit's own test comments on rather than fixes; and (3) the retry loop applies the same backoff-and-retry treatment to *all* exceptions, including permanently corrupt/unsupported files, wasting up to 1.5s per file that will never succeed regardless of retry count. Overall: functional for the happy path and for genuinely transient `OSError`s, but the CLI-contract break and the two logic bugs above mean this should not be considered production-ready as merged.

## Files Changed

- `scan_incomplete.py` — adds `MAX_RETRIES`/`RETRY_BASE_DELAY`/`BLACKLIST_DURATION` constants; adds `init_error_db`, `is_blacklisted`, `record_error`, `get_error_type`; renames `_check_file` to `_check_file_with_retry` and wraps the read/parse step in a bounded exponential-backoff retry loop with blacklist short-circuiting; changes `find_incomplete()`'s signature (inserts `error_db_path` before `num_workers`) and return type (2-tuple → 3-tuple, adds `error_telemetry`); updates `main()` to parse a new positional `error_db` CLI argument and print an error summary to stderr.
- `tests/test_scan_incomplete.py` — updates all existing `find_incomplete()` call sites for the new 3-tuple/4-arg signature; adds new tests for `init_error_db`, `record_error`/`is_blacklisted` (with blacklist expiry), `get_error_type`, and a mocked telemetry-collection test.
- `update_music_metadata.sh` — **not modified by this commit**, despite being the sole shell caller of `scan_incomplete.py`'s CLI (see Critical #1).

## Findings

### Critical

**1. `update_music_metadata.sh` was not updated for the new CLI argument order — `SCAN_WORKERS` is silently broken and a bogus error-DB file gets created.**

`scan_incomplete.py:329-342` (post-commit `main()`):
```python
def main():
    music_dir = sys.argv[1]
    out_file = sys.argv[2]
    mtime_db = sys.argv[3] if len(sys.argv) > 3 else None
    error_db = sys.argv[4] if len(sys.argv) > 4 else None      # NEW: inserted before workers
    num_workers = None
    if len(sys.argv) > 5:
        try:
            num_workers = int(sys.argv[5])                     # shifted from argv[4] to argv[5]
        except ValueError:
            pass
```
Before this commit, `sys.argv[4]` was `num_workers`. This commit inserts `error_db` at position 4 and pushes `num_workers` to position 5 — but `update_music_metadata.sh` (unchanged by this commit) still builds:
```bash
SCAN_ARGS=("$MUSIC_DIR" "$INCOMPLETE_LIST" "$MTIME_DB")
if [ -n "$SCAN_WORKERS" ]; then
  SCAN_ARGS+=("$SCAN_WORKERS")
fi
run_logged python3 "$SCRIPT_DIR/scan_incomplete.py" "${SCAN_ARGS[@]}"
```
When `SCAN_WORKERS` is set (this is a documented, user-facing feature per README.md line 140: `SCAN_WORKERS=4 MUSIC_DIR=/music ./update_music_metadata.sh`), the 4th CLI argument is now consumed by `main()` as `error_db` instead of `num_workers`. Concrete failure scenario: with `SCAN_WORKERS=4`, `scan_incomplete.py` receives `argv = [script, music_dir, out_file, mtime_db, "4"]`. `error_db` becomes the string `"4"`, so `init_error_db("4")` creates/opens a SQLite file literally named `4` in the process's current working directory. Meanwhile `num_workers` stays `None` (no `argv[5]`), so the user's configured worker count is silently discarded and `DEFAULT_WORKERS` is used instead. This is exactly the "did all call sites get updated consistently" scenario flagged for review — they did not. No test exercises `main()`/`sys.argv`, so nothing catches this.

Fix: either update `update_music_metadata.sh` in this commit (or a follow-up) to pass an explicit `ERROR_DB` positional argument before `SCAN_WORKERS`, or make `main()` use `argparse`/named flags instead of fragile positional indices so adding a new optional argument can't silently shift the meaning of existing ones.

### High

**2. `get_error_type()`'s `timeout` branch is unreachable — `TimeoutError` is a subclass of `OSError`, and `OSError` is checked first.**

`scan_incomplete.py:84-95`:
```python
def get_error_type(exception):
    """Classify exception into error type for telemetry."""
    if isinstance(exception, PermissionError):
        return "permission_error"
    elif isinstance(exception, FileNotFoundError):
        return "file_not_found"
    elif isinstance(exception, OSError):
        return "io_error"
    elif isinstance(exception, TimeoutError):
        return "timeout"
    else:
        return "unknown_error"
```
In Python 3, `TimeoutError` inherits from `OSError`. Because the `OSError` check comes before the `TimeoutError` check, every `TimeoutError` instance matches the `OSError` branch first and is classified as `"io_error"` — the `"timeout"` branch can never execute. This directly contradicts the commit message's advertised categories (`permission_error, file_not_found, io_error, timeout, unknown`) and defeats one of the feature's stated goals (distinguishing timeout errors in telemetry for monitoring flaky mounts).

The commit's own test acknowledges this without fixing it, at `tests/test_scan_incomplete.py:390-397`:
```python
def test_get_error_type():
    """Error classification works correctly."""
    assert si.get_error_type(PermissionError("denied")) == "permission_error"
    assert si.get_error_type(FileNotFoundError("missing")) == "file_not_found"
    assert si.get_error_type(OSError("io error")) == "io_error"
    # TimeoutError is subclass of OSError, so it's classified as io_error
    assert si.get_error_type(TimeoutError("timeout")) == "io_error"
    assert si.get_error_type(ValueError("other")) == "unknown_error"
```
This test locks in the bug as expected behavior rather than flagging it as a regression against the stated design.

Fix: reorder the `isinstance` checks so `TimeoutError` (and, if relevant, `socket.timeout`, which is an alias for `TimeoutError` in modern Python) is checked before the generic `OSError` fallback.

**3. Retry/backoff is applied uniformly to all exceptions, including permanent, non-retryable failures — wasting time on every corrupt/unsupported file.**

`scan_incomplete.py:215-246`:
```python
for attempt in range(MAX_RETRIES):
    try:
        mf = MutagenFile(path)
        ...
    except Exception as e:
        last_exception = e
        if attempt < MAX_RETRIES - 1:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            time.sleep(delay)
        else:
            ...
```
The bare `except Exception` catches *everything* mutagen can raise — including permanent, deterministic failures such as unsupported/corrupt file formats (mutagen parsing errors), not just transient I/O conditions from a flaky mount. Before this commit, such files were reported and skipped immediately (see pre-commit `scan_incomplete.py` around the old `_check_file`: `except Exception as e: ... return (path, False, True)`, no retry). After this commit, a permanently corrupt file now costs `0.5 + 1.0 = 1.5s` of wasted retries (2 sleeps) before it's finally given up on — every single time it's scanned, since a corrupt file will fail identically on every attempt. `get_error_type()` is only consulted *after* retries are exhausted (for telemetry/blacklist purposes), never *before* deciding whether a given exception is worth retrying at all. For a library with hundreds of legacy/unsupported files, this measurably regresses the scan performance gained in the prior parallel-scanning commit, and undercuts the "intelligent" framing in the commit message — the retry logic isn't actually error-type-aware where it matters (deciding to retry), only where it doesn't (post-mortem classification).

Fix: classify the exception with `get_error_type()` (or an explicit "is this retryable" predicate) *before* sleeping, and only retry for exception types plausibly caused by transient mount flakiness (`OSError`/`TimeoutError`/`ConnectionError` family), failing fast for everything else (e.g., mutagen-specific parse errors, `ValueError`, etc.) exactly as the pre-commit code did.

### Medium

**4. `error_db_conn` is leaked (never closed) on two early-return paths.**

`scan_incomplete.py:189-205`:
```python
error_db_conn = None
if error_db_path:
    error_db_conn = init_error_db(error_db_path)
    # Check blacklist first
    if is_blacklisted(error_db_conn, path):
        return (path, False, False, None)          # <-- no close()

# Incremental check: skip if mtime hasn't changed
if mtime_db_path:
    try:
        current_mtime = os.path.getmtime(path)
        conn = init_mtime_db(mtime_db_path)
        tracked_mtime = get_tracked_mtime(conn, path)
        conn.close()
        if tracked_mtime is not None and current_mtime == tracked_mtime:
            return (path, False, False, None)        # <-- no close() either
```
Every other return path in this function explicitly calls `error_db_conn.close()` before returning (e.g., lines 210-213, 221-223, 229-231, 242-246, 249-251), but these two early returns — the blacklisted-file case and the unchanged-mtime case — do not. In practice CPython's reference counting will finalize (and thus close) the unreferenced `sqlite3.Connection` almost immediately, but this is implementation-specific behavior, not a guarantee, and it's inconsistent with the rest of the function's explicit cleanup discipline. This is exactly the common case for this feature (incremental scanning combined with error tracking on a flaky mount), so it's the majority code path, not an edge case.

Fix: wrap the body in `try/finally: if error_db_conn: error_db_conn.close()`, or use `with contextlib.closing(...)`.

**5. `error_count`/`last_error_time` are recorded but never used anywhere; the read-modify-write is also non-atomic.**

`scan_incomplete.py:63-81`:
```python
def record_error(conn, filepath, error_type, error_message, blacklist_duration=BLACKLIST_DURATION):
    now = time.time()
    blacklist_until = now + blacklist_duration
    cursor = conn.execute("SELECT error_count FROM error_tracking WHERE filepath = ?", (filepath,))
    row = cursor.fetchone()
    error_count = (row[0] if row else 0) + 1
    conn.execute(
        """INSERT OR REPLACE INTO error_tracking
           (filepath, error_type, error_message, error_count, last_error_time, blacklist_until)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (filepath, error_type, error_message, error_count, now, blacklist_until)
    )
    conn.commit()
```
`error_count` is computed and persisted, but nothing in the codebase ever reads it back — it doesn't extend `blacklist_duration` for repeat offenders, isn't surfaced in the CLI's error summary (which uses a separate in-memory `error_telemetry` dict built in `find_incomplete`, not this column), and isn't queried anywhere else. It's write-only data. Additionally, `is_blacklisted()` (lines 46-60) `DELETE`s the row entirely once the blacklist expires, so `error_count` resets to 0/1 on the very next failure — a file that fails intermittently over weeks never accumulates a count above 1 in practice, defeating the apparent intent of tracking repeat offenders. The read-then-write pattern is also a classic non-atomic increment (a lost update is possible if the same `filepath` is processed by two overlapping runs concurrently).

Fix: either use the count to drive escalating blacklist durations (e.g., `blacklist_duration * error_count`, capped), surface it in the error summary, or drop the column if it's not meant to be used yet.

**6. Per-file SQLite connection overhead plus lock-contention risk during a real outage.**

`scan_incomplete.py:189-191`: every single file check opens a brand-new `sqlite3.connect()` to the error DB when `error_db_path` is set — even for files that have no error history and never error. For a large library this multiplies connection-open/close overhead across every file just to perform a blacklist lookup. More importantly, in the exact scenario this feature targets (mount is fully down, many files fail simultaneously), many `ThreadPoolExecutor` worker threads will call `record_error()` at roughly the same time against the same SQLite file with no explicit `timeout=` configured on `sqlite3.connect()` (default 5s) and no retry-on-`OperationalError` handling. A `database is locked` error raised inside `record_error`/`_check_file_with_retry` is not caught locally; it propagates up to `future.result()` in `find_incomplete()`'s loop (`scan_incomplete.py:296-314`), where it's swallowed generically as `"unexpected_error"` — silently losing the true error classification for that file at exactly the moment telemetry is most needed (a real outage).

**7. No differentiation between a first-time failure and a confirmed repeat offender — every failure gets the same 30-minute blacklist.**

`record_error()` unconditionally sets `blacklist_until = now + blacklist_duration` on the very first recorded failure. A file that hits one unlucky transient hiccup (fails all 3 quick attempts within 1.5s due to, say, a momentary network blip) is blacklisted for the same 30 minutes as a file that is permanently unreadable. Combined with Medium #5 (error_count unused), there's no mechanism to treat these differently, and no way to shorten/lengthen the window based on observed history.

**8. Retry storm is doubled by a pre-existing (not introduced here, but directly compounded by this commit) double-invocation of `scan_incomplete.py`.**

`update_music_metadata.sh` (unchanged by this commit) invokes `scan_incomplete.py` twice — once via `run_logged` to actually produce `$INCOMPLETE_LIST`, and again immediately after purely to capture the `"audio files checked"` summary line for `SCAN_OUTPUT` (`update_music_metadata.sh:270,273`). Since this commit adds up to 1.5s of retry delay per failing/flaky file, and both invocations run the full scan independently, any real mount outage now costs roughly double the retry-storm delay this commit introduces. Not a new bug, but worth noting since it materially amplifies the impact of Medium #6/#7 above, and is exactly the kind of interaction the review should surface even though the double-invocation itself predates this commit.

### Low / Nitpicks

**9. `MAX_RETRIES = 3` actually means 3 total attempts (1 initial + 2 retries), not 3 retries.** `scan_incomplete.py:23,217,234`: the loop is `for attempt in range(MAX_RETRIES)`, so with `MAX_RETRIES=3` there are 3 attempts total and only 2 delays. The name is a mild misnomer versus common "number of retries after the first attempt" convention; consider renaming to `MAX_ATTEMPTS` or adjusting the loop bound to match the name.

**10. `init_error_db()` re-runs `CREATE TABLE IF NOT EXISTS` on every single file check** (`scan_incomplete.py:189-191`, invoked once per call to `_check_file_with_retry`). Harmless but wasteful DDL execution repeated potentially thousands of times per scan; could be created once up front (e.g., in `find_incomplete()` before submitting to the executor) and only opened per-thread thereafter.

**11. Inconsistent telemetry key naming: `"unknown_error"` vs `"unexpected_error"`.** `get_error_type()` (line 95) returns `"unknown_error"` for unclassified exceptions, while the outer `except Exception` in `find_incomplete()`'s result-collection loop (`scan_incomplete.py:312-314`) uses a different key, `"unexpected_error"`, for exceptions raised by `future.result()` itself. These represent conceptually similar "we don't really know what happened" buckets under two different names in the same summary output, which is confusing for anyone reading the stderr error summary or querying telemetry.

**12. Raw exception text and file paths are embedded directly into stderr/log output without sanitization** (e.g. `scan_incomplete.py:212,245`). Given paths originate from a walked filesystem (potentially a network mount not fully trusted/controlled), a crafted filename or exception message containing newlines could inject misleading lines into the log stream. Low real-world severity given the trust model here, but worth a defensive thought if these logs are ever parsed downstream.

**13. `last_exception` (`scan_incomplete.py:216,233`) is assigned but never read.** Dead variable; either use it (e.g., to include the original exception in the final error message alongside the current one) or remove it.

## Test Coverage Assessment

The new unit tests give reasonable isolated coverage of the SQLite helper functions but fall short of validating the actual retry/classification/telemetry pipeline end-to-end, and provide zero coverage of the exact place where a real regression exists:

- `test_init_error_db` and `test_record_and_check_error` (`tests/test_scan_incomplete.py:350-387`) adequately confirm table creation and one blacklist expiry transition (waiting past the deadline), but there is no test for the boundary itself (e.g., a value exactly at or just before `blacklist_until`), and no test confirming that an expired blacklist entry is actually deleted from the table (only that `is_blacklisted` returns `False` afterward).
- `test_get_error_type` (`tests/test_scan_incomplete.py:390-397`) covers all five branches syntactically, but as noted in High #2, it asserts the buggy behavior (`TimeoutError` → `"io_error"`) as correct rather than catching the dead-code regression against the commit's own stated design.
- `test_error_telemetry_collection` (`tests/test_scan_incomplete.py:400-437`) monkeypatches `si._check_file_with_retry` entirely, so it never exercises the real retry loop, the real backoff timing, or the real `get_error_type`/`record_error` call chain — it only tests that `find_incomplete()`'s result-aggregation logic can plumb an `error_info` tuple into the telemetry dict. Its core assertion, `assert "io_error" in errors or len(errors) > 0`, is a hedge that would pass even if the wrong error key ended up in the dict, which meaningfully weakens its regression-catching value.
- There is no test that exercises a **successful recovery** (an exception on attempt 1–2 followed by success on the final attempt) — the positive path for the whole retry feature is untested.
- There is no test asserting the actual backoff delays (e.g., monkeypatching `time.sleep` and checking it was called with `0.5` then `1.0`) — the exponential-backoff math itself, which the commit message highlights, is unverified.
- There is no integration test showing that a blacklisted file is actually skipped by `find_incomplete()`/`_check_file_with_retry` together (only `is_blacklisted()` is tested in isolation).
- There is no test for `main()`'s `sys.argv` handling, which is exactly where Critical #1 lives; the CLI contract change (positional shift for `num_workers`) is entirely unverified.
- No test covers the resource-leak paths from Medium #4.

Net assessment: the new tests are a reasonable start on the low-level helpers but do not adequately cover the higher-risk surfaces (retry decision logic, CLI argument wiring, concurrency behavior, blacklist-driven skipping in the real code path).

## Positive Notes

- All new SQLite queries use parameterized placeholders (`?`) consistently — no SQL injection risk in `error_message`/`filepath` storage.
- Retry sleeps (`time.sleep`) correctly happen inside `ThreadPoolExecutor` worker threads (inside `_check_file_with_retry`, itself run via `executor.submit`), not on the main thread — the main thread only waits on already-submitted futures via `as_completed`, so the pool submission itself isn't blocked by any single file's backoff.
- The exponential backoff math for the two delays that do execute is correct: `0.5 * 2**0 = 0.5s`, then `0.5 * 2**1 = 1.0s`.
- `find_incomplete()`'s docstring was updated to describe the new `error_db_path` parameter and the `error_telemetry` return value, keeping documentation roughly in sync with the signature (module docstring at the top of the file was also updated to mention "Resilient error handling").
- Existing call sites within the test suite were all mechanically updated for the new 3-tuple return (no leftover 2-tuple unpacking inside the Python code itself), which is good diligence for the in-repo Python callers — the gap is specifically the external shell-script CLI caller (Critical #1).
- Introducing a fully separate `error_tracking` SQLite database file (rather than adding a column/table to the existing mtime-tracking DB) avoids any schema-migration hazard for users upgrading from the `b84ecbb`/`bcb09cc` state — existing mtime DBs are completely untouched by this change.

## Recommendations

1. **Fix the CLI contract break immediately** — update `update_music_metadata.sh` to pass an explicit error-DB path before `SCAN_WORKERS`, or switch `main()` to `argparse` with named flags so future positional insertions can't silently reinterpret existing arguments. This is a live, unshipped regression against a documented feature (`SCAN_WORKERS`).
2. **Fix `get_error_type()`'s branch ordering** so `TimeoutError` is checked before the generic `OSError` fallback, and update `test_get_error_type` to assert the corrected (intended) behavior rather than documenting the bug.
3. **Make the retry loop error-aware before retrying**, not just after: classify the exception first and only apply backoff/retry to plausibly-transient types (`OSError`/`TimeoutError`/connection-related), failing fast (as the pre-commit code did) for deterministic/permanent failures like corrupt or unsupported files.
4. **Guarantee `error_db_conn` closure on every return path** via `try/finally` or a context manager, rather than relying on a subset of explicit `close()` calls plus GC finalization for the rest.
5. **Either use `error_count` meaningfully** (escalating blacklist duration for repeat offenders, or surfacing it in the CLI summary) **or remove it** until there's a concrete consumer — dead persisted state adds confusion without benefit.
6. Consider setting an explicit `timeout=` on `sqlite3.connect()` for the error DB and/or serializing error-DB writes through a single connection (e.g., one connection per scan reused across threads with a lock, or a dedicated writer thread) to reduce lock-contention risk during genuine mount-outage storms — and make sure a `database is locked` failure doesn't get silently absorbed into the generic `"unexpected_error"` telemetry bucket.
7. Add the missing test scenarios called out in the Test Coverage Assessment, prioritizing: (a) a `main()`/`sys.argv` test that would have caught Critical #1, (b) a retry-then-succeed integration test, (c) a real (non-mocked) exercise of the exhaust-retries → classify → record → blacklist pipeline.
8. Document the retry/backoff/blacklist behavior in `README.md` (currently undocumented outside the commit message) — at minimum, the existence of the error-tracking DB file, its default 30-minute blacklist window, and how to point it at a custom path via the CLI.
