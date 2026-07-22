# Code Review: b84ecbb — feat: implement parallel file scanning and batch imports

## Summary

This commit parallelizes `scan_incomplete.py`'s file-checking loop with a `ThreadPoolExecutor` (splitting the walk into a "collect all paths" pass and a "check each path in a worker" pass, with mtime-tracking writes deferred to a single batched pass after all workers finish), and teaches `update_music_metadata.sh` to group files into `BATCH_IMPORT_SIZE`-sized chunks for `beet import` instead of invoking beets once per file. The core threading design is sound where it matters most — no shared, mutable `sqlite3.Connection` is ever touched by more than one thread, and mtime writes are deliberately serialized to a single post-parallel phase — and the bash batching loop correctly uses arrays (not string concatenation) so filenames with spaces/special characters remain safe, with a correct final-batch flush and no off-by-one. However, the commit introduces a real regression: the incremental-check path now opens (and does a `CREATE TABLE IF NOT EXISTS` + commit through) a brand-new SQLite connection **per file, per worker thread**, on what is the hottest path of the tool's own headline feature (incremental scans skipping unchanged files) — this is both wasteful and a lock-contention risk, and any exception it raises is swallowed by a handler that doesn't even log which file failed. There's also no validation of the new `SCAN_WORKERS`/`num_workers` value, so an operator setting `SCAN_WORKERS=0` (a plausible attempt to "disable" extra threads) will crash the whole scan with an unhandled `ValueError`. Overall: good structural instincts around thread-safety, but a handful of concrete robustness/diagnosability gaps that should be fixed before this ships to unattended/scheduled runs.

## Files Changed

- `scan_incomplete.py` — extracts per-file logic into `_check_file()` (opens its own SQLite connection per call since connections aren't thread-safe); `find_incomplete()` now does a two-pass walk (collect paths, then dispatch to a `ThreadPoolExecutor`), collects mtime updates into a dict and writes them in one batched pass after all workers complete; adds `DEFAULT_WORKERS = min(8, cpu_count()+1)`; `main()` gains an optional 4th CLI arg (worker count).
- `update_music_metadata.sh` — adds `SCAN_WORKERS` (forwarded to `scan_incomplete.py` as argv[4] when set) and `BATCH_IMPORT_SIZE` (default 50) env vars; replaces the per-file `beet import -q -s "$file"` loop with a `BATCH_FILES` array that's flushed via `beet import -q -s "${BATCH_FILES[@]}"` every `BATCH_IMPORT_SIZE` files, plus a final flush for the leftover partial batch.
- `tests/test_scan_incomplete.py` — adds two tests: parallel-vs-single-threaded output parity, and parallel+incremental combined behavior.
- `README.md` — adds "Parallel File Scanning" and "Batch Import" sections documenting `SCAN_WORKERS`/`BATCH_IMPORT_SIZE` and their defaults; removes the now-implemented "parallel file scanning" item from the open-items list.

## Findings

### Critical

None found.

### High

**1. `SCAN_WORKERS=0` (or any non-positive value) crashes the entire scan with an unhandled `ValueError`.**

`scan_incomplete.py:210-221` (`main()`):
```python
num_workers = None
if len(sys.argv) > 4:
    try:
        num_workers = int(sys.argv[4])
    except ValueError:
        pass

total, incomplete = find_incomplete(music_dir, mtime_db, num_workers)
```
`num_workers` is parsed as a plain `int` with no range check, then flows straight into `ThreadPoolExecutor(max_workers=num_workers)` at `scan_incomplete.py:172`. `ThreadPoolExecutor` raises `ValueError: max_workers must be greater than 0` for `0` or negative values, and nothing in `main()`/`find_incomplete()` catches it. `update_music_metadata.sh:187-191` forwards `SCAN_WORKERS` verbatim whenever it's non-empty (`[ -n "$SCAN_WORKERS" ]`), including the literal string `"0"`:
```bash
SCAN_ARGS=("$MUSIC_DIR" "$INCOMPLETE_LIST" "$MTIME_DB")
if [ -n "$SCAN_WORKERS" ]; then
  SCAN_ARGS+=("$SCAN_WORKERS")
fi
run_logged python3 "$SCRIPT_DIR/scan_incomplete.py" "${SCAN_ARGS[@]}"
```
Concrete failure scenario: an operator sets `SCAN_WORKERS=0` believing it disables extra threads (a reasonable guess given no documented valid range), or a templating bug in a compose/env file produces `"0"`. `scan_incomplete.py` immediately raises and exits non-zero; because the script runs under `set -euo pipefail`, `update_music_metadata.sh` aborts right there — before any tagging happens at all, with no cover-art/tag fixes applied that run and no clear indication in the log of *why* it aborted beyond a raw Python traceback.

Suggested fix: validate `num_workers` (e.g. `if num_workers is not None and num_workers < 1: num_workers = None` with a warning, or clamp to 1) before constructing the executor, and document the valid range in the README/usage comment.

**2. Worker-thread exceptions are logged without identifying which file failed, despite the file path being readily available.**

`scan_incomplete.py:172-195`:
```python
futures = {
    executor.submit(_check_file, path, mtime_db_path): path
    for path in files_to_check
}
...
for future in as_completed(futures):
    try:
        path, is_incomplete, should_track = future.result()
        ...
    except Exception as e:
        print(f"WARN: unexpected error checking file ({e})", file=sys.stderr)
```
`futures` is a `future -> path` map built specifically for this purpose, but the `except` branch never consults it (`futures[future]`). Any exception that isn't an `OSError` caught inside `_check_file` itself (see Finding 3 below for a concrete source of such exceptions — e.g. `sqlite3.OperationalError`) propagates through `future.result()` and is logged as a bare, file-less `WARN: unexpected error checking file (...)`. For a tool explicitly designed to run unattended over libraries of "1000+ files" (per this commit's own message), this makes any worker-thread failure essentially undiagnosable — the operator can't tell which of potentially thousands of files needs attention, and the file is silently excluded from both the `incomplete` list and the mtime-tracking update for that run.

Suggested fix:
```python
except Exception as e:
    print(f"WARN: unexpected error checking file {futures[future]} ({e})", file=sys.stderr)
```

### Medium

**3. Incremental-mode check reopens a SQLite connection (with a `CREATE TABLE IF NOT EXISTS` + commit) for every single file, from every worker thread — a regression from the pre-commit single-connection design, and the likely source of Finding 2's exceptions.**

`scan_incomplete.py:114-126`:
```python
if mtime_db_path:
    try:
        current_mtime = os.path.getmtime(path)
        conn = init_mtime_db(mtime_db_path)
        tracked_mtime = get_tracked_mtime(conn, path)
        conn.close()
        if tracked_mtime is not None and current_mtime == tracked_mtime:
            return (path, False, False)
    except OSError as e:
        ...
```
`init_mtime_db()` (`scan_incomplete.py:22-31`) runs `CREATE TABLE IF NOT EXISTS ...` followed by `conn.commit()` on *every call*. Before this commit, `find_incomplete()` opened one connection for the entire walk (see `b84ecbb^:scan_incomplete.py`); now, in incremental mode, `_check_file()` opens/queries/closes a fresh connection **per file**, and — because up to `DEFAULT_WORKERS` (CPU count + 1, capped at 8) threads call this concurrently against the same on-disk SQLite file — this is exactly the hot path that incremental mode exists to make cheap (on any run after the first, the overwhelming majority of files hit this "unchanged, skip" branch). Besides the pure overhead of a connect/DDL/commit/close cycle per file, this creates avoidable lock contention across threads with no `busy_timeout` configured, i.e. a real (if not certain) risk of `sqlite3.OperationalError: database is locked` — which is not an `OSError`, so it isn't caught by the `except OSError` here; it propagates up to the broad, file-less handler in Finding 2, silently dropping that file from the run.

Suggested fix: read all tracked mtimes into an in-memory `dict` once via a single connection/query before submitting work to the executor (workers then do a simple dict lookup, no per-file DB I/O), and only open a connection for the deferred batch write that already exists at the end of `find_incomplete()`.

**4. Batching collapses per-file success/failure visibility in `update_music_metadata.sh`'s import step.**

`update_music_metadata.sh:209-229`:
```bash
BATCH_FILES=()
while IFS= read -r file; do
  [ -f "$file" ] || continue
  BATCH_FILES+=("$file")
  if [ ${#BATCH_FILES[@]} -ge "$BATCH_IMPORT_SIZE" ]; then
    log "Processing batch of ${#BATCH_FILES[@]} files..."
    run_logged beet -c "$BEETS_CONFIG" import -q -s "${BATCH_FILES[@]}" \
      || err "WARNING: Some files in batch could not be automatically tagged (no confident match)."
    BATCH_FILES=()
  fi
done < "$INCOMPLETE_LIST"
```
Pre-commit, each file was imported (and logged) individually — `log "Processing: $file"` followed by a per-file warning on failure — so the log told the operator exactly which file failed. Now up to `BATCH_IMPORT_SIZE` (default 50) files go into a single `beet import` invocation; the batch log line only states a count, never the file list, and on failure the single generic warning applies to the whole batch with no indication of which file(s) within it actually failed vs. succeeded (files beets already tagged before hitting whatever caused the non-zero exit remain tagged on disk — `write: yes`, no dry-run — so partial success is likely but unauditable from the log). For a tool meant to be run unattended against large, possibly-flaky (network-mounted) libraries, this is a meaningful loss of diagnosability introduced by this commit.

Suggested fix: at minimum, log the batch's file list at a verbose/debug level so a failure can be cross-referenced later; consider a fallback that retries a failed batch file-by-file to pinpoint which file(s) actually failed.

**5. `BATCH_IMPORT_SIZE` is used without type or bounds validation.**

`update_music_metadata.sh:216` (`if [ ${#BATCH_FILES[@]} -ge "$BATCH_IMPORT_SIZE" ]; then`) assumes `BATCH_IMPORT_SIZE` is a positive integer but never checks it:
- A non-numeric value (e.g. a typo like `BATCH_IMPORT_SIZE=fifty`) makes `[ ... -ge "$BATCH_IMPORT_SIZE" ]` emit `bash: [: fifty: integer expression expected` on every iteration and evaluate as "not yet", silently degrading into "accumulate literally every incomplete file into one giant final batch" — the opposite of what the operator likely intended, and undocumented.
- An excessively large value (someone assuming "bigger = faster" and setting e.g. `BATCH_IMPORT_SIZE=100000` on a huge library) risks a single `beet import` invocation hitting the OS `ARG_MAX` argument-list limit ("Argument list too long").

Suggested fix: validate `BATCH_IMPORT_SIZE` is a positive integer near the top of the script (alongside the other config), falling back to the default with a warning otherwise, and consider documenting/enforcing a sane upper bound.

### Low / Nitpicks

**6. Invalid `SCAN_WORKERS` values are silently discarded.**

`scan_incomplete.py:214-219`:
```python
num_workers = None
if len(sys.argv) > 4:
    try:
        num_workers = int(sys.argv[4])
    except ValueError:
        pass
```
If parsing fails, `num_workers` just falls back to `None` (i.e., `DEFAULT_WORKERS`) with no message to the operator that their configured value was ignored. A one-line `WARN:` to stderr would save real debugging time when someone typos `SCAN_WORKERS` and can't figure out why thread count doesn't change.

**7. Double `os.path.getmtime(path)` call, now spanning a thread-pool worker boundary.**

The mtime used to decide "unchanged, skip" (`scan_incomplete.py:117`, inside `_check_file`, executed in a worker thread) and the mtime later persisted for that file (`scan_incomplete.py:190`, inside `find_incomplete`'s `as_completed` loop, executed after the worker's `MutagenFile` read completes) come from two separate `stat()` calls. This pattern predates this commit, but parallelizing it widens the window between the two reads (the second stat now waits for the whole worker task — including the `MutagenFile` parse — to finish before firing), making it marginally more likely that a file touched mid-scan gets a persisted mtime that doesn't correspond to what mutagen actually inspected. Capturing `current_mtime` once inside `_check_file` and threading it through the return tuple (e.g. `(path, is_incomplete, should_track, mtime)`) would remove both the extra syscall and the widened TOCTOU window.

**8. `_check_file`'s tri-state return contract isn't documented.**

`scan_incomplete.py:108-113` documents the return shape (`(path, is_incomplete, should_track_mtime)`) but not what the different `should_track_mtime` values mean on error paths: `(path, False, False)` on a stat/DB error (line 126) vs. `(path, False, True)` on an unreadable/corrupt file (lines 133, 137). The distinction is meaningful (the former means "we didn't even try, don't record anything"; the latter means "we tried and failed, but still bump the tracked mtime so this un-openable file isn't re-attempted every single run") but is left implicit — worth a short comment for the next reader.

**9. Batch-mode logs no longer show which files were processed in a given run.**

Previously every file got its own `log "Processing: $file"` line; now `update_music_metadata.sh:217`/`226` only logs a count ("Processing batch of 50 files..."). This is a reasonable trade-off for reduced log volume/speed but is worth being a deliberate choice — see Finding 4 for the failure-diagnosability angle.

## Test Coverage Assessment

The two new tests (`test_find_incomplete_with_parallel_workers`, `test_find_incomplete_parallel_with_incremental`) are reasonable smoke tests: they confirm `find_incomplete()` produces the same result set at `num_workers=1` vs. `num_workers=4`, and that incremental skip-detection still works when combined with the thread pool. They follow the existing `FakeMF`/`FakeTags` mocking conventions and use meaningful assertions (`call_count` dict to prove `MutagenFile` was/wasn't invoked, not just checking the output list).

However, they don't actually exercise concurrency in any way that could catch a real race:
- All mocked work (`fake_mutagen_file`) is instant and CPU-bound under the GIL, with no injected delay/barrier, so there's no guarantee the "parallel" runs ever have more than one worker thread runnable at the same time — the tests mostly prove the sequential-equivalent code path is wired correctly, not that concurrent execution is safe.
- Neither test uses more than 2-3 files, far short of what would be needed to stress the per-file-per-thread SQLite connection churn described in Finding 3 (that would require dozens of files and several workers hammering a real temp DB file to have a chance of surfacing lock contention).
- There is no test covering the `num_workers <= 0` crash (Finding 1), no test asserting that a worker exception is logged with an identifiable file path (Finding 2), and no test at all — Python or shell — for the new `update_music_metadata.sh` batching loop (Findings 4/5), which is arguably the more failure-prone new logic in this commit given the instructions' emphasis on batch-boundary and quoting correctness. The shell script currently has zero automated test coverage of any kind.

## Positive Notes

- Correct core thread-safety instinct: no raw `sqlite3.Connection` is ever shared across threads — `_check_file()`'s docstring explicitly calls out *why* ("sqlite3 connections are NOT thread-safe"), and the design deliberately defers all mtime **writes** to a single-threaded batch phase after the executor's `with` block exits, avoiding concurrent writers entirely.
- The bash batching loop uses a proper array (`BATCH_FILES+=("$file")` / `"${BATCH_FILES[@]}"`) rather than string concatenation, so filenames containing spaces, quotes, or globbing metacharacters are passed through to `beet import` safely — no shell-injection or word-splitting risk.
- The batch-boundary logic itself is correct: batches trigger at exactly `BATCH_IMPORT_SIZE` (`-ge` comparison), and the trailing partial batch is correctly flushed after the loop — no off-by-one, no dropped files, no empty-batch invocation of `beet import` when the count is zero.
- `find_incomplete()` handles the empty-file-list, single-file, and exactly-N-files edge cases gracefully (`ThreadPoolExecutor`/`as_completed` on an empty or single-item `futures` dict just does nothing / one iteration — no special-casing needed and none was added).
- README updates accurately describe the actually-implemented defaults (`CPU_count + 1` capped at 8; `BATCH_IMPORT_SIZE` default 50) and correctly note that Docker Compose users must manually add the new env vars to their `environment:` block (compose doesn't auto-forward host env vars into the container).

## Recommendations

1. **(High)** Validate `num_workers`/`SCAN_WORKERS` is a positive integer before constructing `ThreadPoolExecutor`; fall back to the default with a warning instead of crashing the entire run on `0`/negative input.
2. **(High)** Fix the exception handler in `find_incomplete()`'s `as_completed` loop to include `futures[future]` (the file path) in the logged warning, so worker failures are attributable.
3. **(Medium)** Replace the per-file `init_mtime_db()` call inside `_check_file()`'s incremental branch with a single bulk read of all tracked mtimes into a dict before the executor is created; only open a live connection for the existing batched write phase.
4. **(Medium)** Add basic validation of `BATCH_IMPORT_SIZE` (numeric, positive, sane upper bound) in `update_music_metadata.sh`, and consider logging the batch's file list (or at least a sample) so a failed batch can be diagnosed after the fact.
5. **(Low)** Log a warning when a supplied `SCAN_WORKERS` value fails to parse, instead of silently falling back to the default.
6. **(Low)** Capture `os.path.getmtime(path)` once inside `_check_file()` and thread it through the return value instead of re-statting later in `find_incomplete()`.
7. **(Test coverage)** Add a test that forces `num_workers <= 0` and asserts a clean, documented failure mode (not a raw `ValueError`); add a test with enough files/workers against a real temp SQLite DB to have a realistic chance of catching lock-contention regressions like Finding 3; add shell-level tests (e.g. via `bats`/`shunit2`) for the batching loop's boundary behavior, since it currently has no automated coverage at all.
