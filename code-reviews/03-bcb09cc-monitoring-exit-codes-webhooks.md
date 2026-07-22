# Code Review: bcb09cc — feat: implement monitoring, exit codes, and webhook notifications

## Summary

This commit adds process-exit-code semantics (0/1/2), a `metrics.json` health-check file, and
an optional webhook POST of those metrics, on top of the parallel-scanning work from `b84ecbb`.
The instrumentation itself (timing, counters, JSON emission, curl call) is reasonably
straightforward bash, and the README/inline documentation is a nice addition. However, the way
metrics are *sourced* introduces a severe functional regression: to obtain `total_files_scanned`
and `incomplete_files_found`, the script re-invokes `scan_incomplete.py` a second time and parses
its stdout with a regex, discarding the already-correct result of the first (real) scan. Because
`scan_incomplete.py` already persists an mtime cache (from the prior commit) and unconditionally
updates it during the first invocation, the second invocation sees every file as "unchanged since
last scan" and reports (and writes to `$INCOMPLETE_LIST`) essentially zero incomplete files —
in effect, this commit disables the tool's core tagging step on every normal run. Combined with an
un-timeout'd webhook call and an unrestricted `WEBHOOK_URL`, this commit needs a fix before it
should be considered mergeable/deployable, despite the otherwise clean structure.

## Files Changed

- `update_music_metadata.sh` — adds exit-code constants/doc header, `START_TIME`/counter globals,
  `emit_metrics()` (JSON generation + optional webhook POST), instruments `err()` to bump
  `ERROR_COUNT`/`WARNING_MESSAGES`, adds a second `scan_incomplete.py` invocation to scrape
  scan totals via regex, wires `emit_metrics`/`exit` into all early-return and end-of-script paths,
  and tracks `PROCESSED_FILES`/`TAGGED_FILES` around the batch-import loop.
- `README.md` — new "Monitoring & Integration" section documenting exit codes, the `metrics.json`
  schema, and `WEBHOOK_URL` usage.

## Findings

### Critical

1. **The metrics-gathering re-scan silently breaks the core tagging pipeline (near-guaranteed regression on every run).**
   `update_music_metadata.sh:264-278`:
   ```bash
   SCAN_ARGS=("$MUSIC_DIR" "$INCOMPLETE_LIST" "$MTIME_DB")
   ...
   run_logged python3 "$SCRIPT_DIR/scan_incomplete.py" "${SCAN_ARGS[@]}"

   # Parse scan output for metrics (run again to capture the final line)
   SCAN_OUTPUT=$(python3 "$SCRIPT_DIR/scan_incomplete.py" "${SCAN_ARGS[@]}" 2>&1 | grep "audio files checked" | tail -1)
   if [[ $SCAN_OUTPUT =~ ([0-9]+)\ audio\ files\ checked,\ ([0-9]+)\ incomplete ]]; then
     TOTAL_FILES="${BASH_REMATCH[1]}"
     INCOMPLETE_FILES="${BASH_REMATCH[2]}"
   fi

   if [ "$INCOMPLETE_FILES" -eq 0 ]; then
     log "All files already have cover art and metadata. Nothing to do."
     ...
     exit 0
   fi
   ```
   `scan_incomplete.py` (as it stands at this commit) is invoked with the **same** `$INCOMPLETE_LIST`
   and `$MTIME_DB` both times. Its `_check_file()`/`find_incomplete()` logic
   (`scan_incomplete.py:108-143, 197-205`) unconditionally records the *current* mtime for every
   file it inspects during the **first** invocation — including files it just found incomplete
   (`return (path, is_incomplete, True)` at line 143, `should_track=True` regardless of
   `is_incomplete`). The **second** invocation, run immediately afterward with the exact same
   `MTIME_DB`, sees `current_mtime == tracked_mtime` for essentially every file
   (`scan_incomplete.py:121-123`) and therefore reports **every** file as "unchanged, not
   incomplete" — regardless of whether it actually has missing tags/art. Two compounding effects:
   - `main()` (`scan_incomplete.py:223-227`) overwrites `out_file` (== `$INCOMPLETE_LIST`) with this
     second, incorrect, near-empty result, clobbering the correct list produced by the first scan.
   - The regex-derived `INCOMPLETE_FILES` used for the `-eq 0` early-exit check is likewise ~0.

   Net effect: on essentially every invocation (including the very first run against a fresh
   library), the script logs "All files already have cover art and metadata. Nothing to do." and
   exits 0 **without ever running the batch beet-import/tagging step**, even though the music
   directory genuinely contains incomplete files that the first (correct) scan found. This is not
   a rare edge case — it is the expected outcome of every normal invocation once mtime tracking is
   populated (which happens within the very first scan of the very first run). This silently
   defeats the entire purpose of the tool as of this commit.
   **Fix:** don't re-run the scan. Derive `INCOMPLETE_FILES` from `wc -l < "$INCOMPLETE_LIST"` (as
   the pre-commit code correctly did) and capture `TOTAL_FILES` either by having
   `scan_incomplete.py` write structured output (e.g., a small JSON/line to a file, or print the
   summary once and tee/capture the *first* run's stdout via `run_logged`/process substitution)
   rather than invoking the expensive, side-effecting scan a second time.

### High

2. **Webhook POST has no timeout and can hang the whole run indefinitely.**
   `update_music_metadata.sh:144-148`:
   ```bash
   curl -s -X POST "$WEBHOOK_URL" \
     -H "Content-Type: application/json" \
     -d @"$METRICS_FILE" \
     || err "WARNING: Failed to send webhook notification"
   ```
   No `--max-time`/`--connect-timeout` is set. If `WEBHOOK_URL` points at a host that never
   responds (firewalled, blackholed, or simply slow), `curl` can hang for a very long time (or
   indefinitely on a connect that never resolves/refuses), which will hang the whole script —
   including any Kubernetes CronJob/cron wrapper waiting on it — well past the point where the
   actual tagging work has already finished successfully. This is especially bad because
   `emit_metrics()` is the very last thing the script does before `exit`, so a slow/unreachable
   webhook host turns a successful run into an apparently-hung job.
   **Fix:** add `--max-time 10 --connect-timeout 5` (or similar) to the `curl` invocation.

3. **`tags_updated` does not measure what it claims to measure.**
   `update_music_metadata.sh:295-319`:
   ```bash
   run_logged beet -c "$BEETS_CONFIG" import -q -s "${BATCH_FILES[@]}" \
     || err "WARNING: Some files in batch could not be automatically tagged (no confident match)."
   PROCESSED_FILES=$((PROCESSED_FILES + ${#BATCH_FILES[@]}))
   ...
   TAGGED_FILES=$PROCESSED_FILES
   ```
   `PROCESSED_FILES` is incremented by the full batch size regardless of whether the `beet import`
   call for that batch succeeded or hit the `|| err` fallback (which fires precisely when files in
   the batch could **not** be confidently tagged). `TAGGED_FILES` is then just set equal to
   `PROCESSED_FILES`. As a result, `tags_updated` in `metrics.json`/webhook payload always equals
   `files_processed` — i.e. "files submitted to beet," not "files actually tagged" — even for
   batches where the accompanying warning says the opposite. Any monitoring/alerting consuming
   this field will get a falsely rosy number.
   **Fix:** either rename the field to reflect what's actually counted ("files submitted for
   tagging"), or derive a real success count from beet's own per-file result (out of scope details
   aside, at minimum stop asserting `TAGGED_FILES=$PROCESSED_FILES` when a batch import reported an
   error).

4. **`WEBHOOK_URL` is completely unvalidated (SSRF-shaped surface) and the payload leaks internal file paths / raw error text to it.**
   `update_music_metadata.sh:142-152` accepts any string in `WEBHOOK_URL` and POSTs to it with no
   scheme allowlist (e.g. `file://`, `gopher://` are not rejected — though curl support for those
   varies) and no check against loopback/link-local/private ranges (e.g. `127.0.0.1`,
   `169.254.169.254` cloud metadata endpoints, RFC1918 ranges). The POST body
   (`$METRICS_FILE`, built at lines 121-136) includes `music_dir` (an absolute filesystem path) and
   the full text of every `err()` call, which can include arbitrary OS/library error text
   (`WARNING_MESSAGES` at `update_music_metadata.sh:84-88`). If `WEBHOOK_URL` is ever set from a
   less-trusted source than the deploying operator (a UI field, a multi-tenant config, etc.), this
   is a textbook SSRF primitive combined with unintended data exfiltration; even in a
   single-operator deployment, there is no defense-in-depth (no destination allowlist, no way to
   redact `music_dir`/error text from the payload).
   **Fix:** at minimum, document that `WEBHOOK_URL` must be treated as a trusted, operator-only
   setting; consider restricting to `https://` and rejecting loopback/link-local/private
   destinations if this value could ever originate from a less-trusted layer.

### Medium

5. **`metrics.json` write is non-atomic and unescaped, risking corrupt/invalid JSON.**
   `update_music_metadata.sh:121-136` writes directly to `$METRICS_FILE` via `cat > ... <<EOF`
   (no temp-file + `mv`). A crash/SIGKILL mid-write (OOM-kill, k8s pod eviction, `docker kill`)
   leaves a truncated file for the next health-check reader. Separately, `"music_dir": "$MUSIC_DIR"`
   and `"log_file": "$LOG_FILE"` are interpolated raw with no JSON escaping — unlike the
   `warnings` array (correctly built through `jq -R .`), a `MUSIC_DIR` containing a `"` or `\`
   (plausible for some SMB/exotic mount names) produces invalid JSON that breaks any downstream
   parser/monitoring system.
   **Fix:** write to `"$METRICS_FILE.tmp"` then `mv` into place; run `MUSIC_DIR`/`LOG_FILE` through
   `jq -Rn --arg v "$MUSIC_DIR" '$v'` (or similar) before embedding.

6. **Environment/setup warnings are conflated with per-run processing errors in the exit-code decision.**
   The pre-existing chromaprint/fpcalc auto-install fallback (`update_music_metadata.sh:180-182`,
   e.g. `err "WARNING: fpcalc not found and apt-get not available. Fingerprinting will be
   disabled."`) now flows through the *same* `err()` that increments `ERROR_COUNT`
   (`update_music_metadata.sh:84-88`), which alone decides the final exit code
   (`update_music_metadata.sh:335-338`: `if [ $ERROR_COUNT -gt 0 ]; then EXIT_CODE=2; fi`). On any
   host without sudo/apt-get (i.e. most minimal/rootless containers), this one-time environment
   warning fires on *every single run*, forcing exit code 2 ("partial_success") forever — even on
   runs where 100% of files were tagged with zero per-file errors. This defeats the stated purpose
   of exit codes for orchestration/alerting (an operator watching for "2 = something needs
   attention" gets permanent noise instead of signal).
   **Fix:** track infra/setup warnings separately from per-file tagging errors, and only let
   genuine per-file failures affect `ERROR_COUNT`/exit code.

7. **New hard dependencies on `jq` and `bc` are introduced without availability checks or documentation, unlike `curl`.**
   `update_music_metadata.sh:106` (`... | bc`) and `:118` (`jq -R . | jq -s .`) assume `jq`/`bc`
   are installed, but only `curl` gets a `command -v curl` guard (`update_music_metadata.sh:144`).
   If `bc` is missing, `duration_sec` silently becomes empty, producing invalid JSON
   (`"duration_seconds": ,`) with no warning at all. If `jq` is missing, the `warnings_json`
   assignment falls back to `"[]"` (`|| echo "[]"` at line 118), silently discarding all captured
   warning messages from the payload with no indication to the operator. Neither dependency is
   added to the script's header "Requires" list (`update_music_metadata.sh:15-23`) or to the
   README prerequisites.
   **Fix:** add `jq`/`bc` to the documented requirements, and guard both with `command -v` checks
   consistent with the existing `curl` handling.

8. **Exit-code documentation is internally inconsistent.**
   The script's own header comment (`update_music_metadata.sh:13`) states:
   `2 = partial success (some files processed, but errors occurred or no files needed updating)`
   but the actual "no files needed updating" code path (`update_music_metadata.sh:280-284`) returns
   **exit 0**, not 2. The README's "Exit Codes" section doesn't mention this case for code 2 either
   (it only says "Some files processed, but warnings/errors occurred"). This is a straightforward
   comment/implementation mismatch that will confuse anyone relying on the documented contract.
   **Fix:** correct the header comment to match the actual behavior (0 covers "nothing to do").

### Low / Nitpicks

9. **`local end_time=$(date +%s%N)` masks the command substitution's exit status.**
   `update_music_metadata.sh:104` — the classic bash gotcha where `local var=$(cmd)` reports the
   exit status of the `local` builtin, not `cmd`. Low risk in practice since `date` essentially
   never fails, but worth noting given the script otherwise checks tool availability carefully
   (`curl`, `python3`).

10. **Fragile text-scraping coupling between shell and Python is a systemic anti-pattern, independent of Critical #1.**
    Parsing a human-readable log line (`"{total} audio files checked, {len(incomplete)}
    incomplete."`, `scan_incomplete.py:227`) with a bash regex (`update_music_metadata.sh:274`)
    couples the monitoring feature to incidental wording in an unrelated file. Any future rewording
    of that print statement will silently zero out all scan metrics with no error raised. Structured
    output (e.g., a small JSON summary file written by `scan_incomplete.py`) would be far more
    robust — and would also remove the temptation to re-invoke the scan a second time.

11. **`warnings` array mixes hard errors and soft warnings under a single undifferentiated label.**
    `err()` (`update_music_metadata.sh:83-88`) is used both for fatal preconditions ("ERROR: python3
    is required...") and soft, expected conditions ("WARNING: fpcalc not found..."); both land in
    the same `WARNING_MESSAGES` array/JSON `warnings` field with no `level`/`severity` distinction,
    making it harder for a consumer to triage the payload.

## Test Coverage Assessment

There is **no test coverage at all** for anything added in this commit. The only test file in the
repository at this commit, `tests/test_scan_incomplete.py`, exercises `scan_incomplete.py`'s
completeness-checking logic and predates this commit; it does not (and cannot, being Python-only)
exercise `update_music_metadata.sh`'s new exit-code logic, `emit_metrics()`, the webhook POST path,
or the interaction between the duplicated scan invocation and the mtime cache that produces Critical
Finding #1. Given that this finding is a severe, easily-reproducible regression, it would very
likely have been caught by even a minimal integration test (e.g., a bats/shellspec test or a
Python-level test asserting that running the scan twice against the same mtime DB does not zero
out the incomplete count) run against a small fixture directory with a couple of incomplete files.
This is a clear gap worth calling out explicitly: the bash orchestration script — arguably the
highest-risk, most behavior-critical part of the tool — has zero automated coverage.

## Positive Notes

- Exit-code contract (0/1/2) is a sensible, minimal design for CronJob/CI integration, and is
  applied at all of the intended control-flow points (missing music dir, missing python3, nothing
  to do, end-of-run).
- `emit_metrics()` is called consistently before every `exit`, so a `metrics.json` is always
  produced for automation to consume, including on early failures.
- The webhook failure path is handled defensively — a failed/absent `curl` degrades to a warning
  rather than crashing the whole script (modulo the missing timeout, Finding #2).
- The `warnings` JSON array is correctly escaped via `jq -R . | jq -s .`, unlike the raw string
  fields — showing the author was at least aware of JSON-escaping concerns for some fields.
- README documentation is clear, includes a concrete example payload, and covers both CLI and
  docker-compose usage for `WEBHOOK_URL`.

## Recommendations

1. **(Must fix before merge/deploy)** Remove the redundant second `scan_incomplete.py` invocation;
   source `INCOMPLETE_FILES` from `wc -l < "$INCOMPLETE_LIST"` and get `TOTAL_FILES` without a second,
   side-effecting scan (Critical #1).
2. Add `--max-time`/`--connect-timeout` to the webhook `curl` call (High #2).
3. Stop equating `tags_updated` with `files_processed`; make the metric reflect actual tagging
   outcomes, or rename it to avoid implying success where none was verified (High #3).
4. Document `WEBHOOK_URL` as a trusted-operator-only setting and consider scheme/destination
   restrictions if this could ever be set by a less-trusted layer (High #4).
5. Make the `metrics.json` write atomic (temp file + `mv`) and JSON-escape all interpolated string
   fields (Medium #5).
6. Separate infra/environment warnings from per-file processing errors so the exit code stays a
   meaningful signal (Medium #6).
7. Add `jq`/`bc` to documented requirements and guard both with availability checks like the
   existing `curl` check (Medium #7).
8. Fix the stale exit-code-2 description in the script header comment (Medium #8).
9. Add at least a minimal integration test around the exit-code/metrics logic — in particular a
   regression test that would have caught Critical #1 (repeated scans against a populated mtime
   cache must not lose track of already-known-incomplete files).
