# Code Review: 02fb807 — feat: selective tagging modes (cover_only, strict, aggressive, default)

## Summary

This commit introduces a new `tagging_modes.py` module defining four tagging profiles (`cover_only`, `strict`, `aggressive`, `default`) as a plain dict of magic-number thresholds, plus a `find_incomplete_by_mode()` wrapper around the existing `scan_incomplete.find_incomplete()`, and 7 unit tests. The commit message is explicit that wiring (`TAGGING_MODE` env var in `update_music_metadata.sh`) is future work, and indeed nothing in the tree calls this module yet — it is inert, self-contained scaffolding. As standalone code it is reasonable but has a real logic inconsistency (silent vs. loud handling of unknown mode strings), an inefficiency/error-swallowing issue in the `cover_only` filtering path, an under-specified config schema, and test coverage that never exercises the one function with actual branching logic (`find_incomplete_by_mode`). None of these are severe given the code is not yet called from production paths, but they should be fixed before the module is wired up in a later commit.

## Files Changed

- `tagging_modes.py` (new, 82 lines) — defines `TAGGING_MODES` dict with per-mode thresholds/flags, `validate_mode()`, `get_mode_config()`, and `find_incomplete_by_mode()` which filters `scan_incomplete.find_incomplete()` output for `cover_only` mode.
- `tests/test_tagging_modes.py` (new, 69 lines) — 7 tests covering `validate_mode`, `get_mode_config`, config presence/shape, and threshold ordering between `strict`/`aggressive`. No test calls `find_incomplete_by_mode`.

## Findings

### Critical
None found.

### High
None found.

### Medium

1. **Inconsistent handling of unknown mode strings within the same module** (`tagging_modes.py:49-51` vs. `tagging_modes.py:64-65`).
   - `get_mode_config()` silently falls back to `"default"` for any unrecognized mode:
     ```python
     def get_mode_config(mode):
         """Get configuration for a tagging mode."""
         return TAGGING_MODES.get(mode, TAGGING_MODES["default"])
     ```
   - `find_incomplete_by_mode()` instead calls `validate_mode()` first and raises loudly on the same condition:
     ```python
     if not validate_mode(mode):
         raise ValueError(f"Unknown tagging mode: {mode}. Valid modes: {', '.join(TAGGING_MODES.keys())}")
     ```
   - Concrete failure scenario: once `update_music_metadata.sh` wires `TAGGING_MODE=stirct` (typo) through to a future call site that uses `get_mode_config()` directly (e.g. for building the beets config), the typo will be silently swallowed and the user's whole library gets tagged with `default` thresholds instead of `strict` — with zero warning that their env var was ignored. Meanwhile the exact same typo passed to `find_incomplete_by_mode()` raises and aborts the run. Two call paths into the same module, two different behaviors for the identical bug class.
   - Suggested fix: pick one policy and apply it everywhere — either have `get_mode_config()` raise (or log a clear warning) on unknown mode, or have `find_incomplete_by_mode()` also silently fall back. Given this is user-facing configuration (typo-prone env var), raising/failing loudly is the safer default; the current silent fallback in `get_mode_config()` is the one that should be removed or made explicit (e.g. `get_mode_config(mode, strict=True)`).

2. **`cover_only` filtering re-reads every file with mutagen and silently drops files on any error, duplicating work already done by `find_incomplete()`** (`tagging_modes.py:70-80`).
   ```python
   if mode_config["skip_tagging"]:
       cover_only = []
       for filepath in incomplete:
           try:
               mf = si.MutagenFile(filepath)
               if mf and not si.has_cover(mf):
                   cover_only.append(filepath)
           except Exception:
               pass  # If error reading, skip it
       incomplete = cover_only
   ```
   - `scan_incomplete._check_file_with_retry()` (called internally by `find_incomplete()`) already computes `missing_cover = not has_cover(mf)` for every file (`scan_incomplete.py:259`), but only returns the aggregated boolean `is_incomplete`, discarding the more granular reason. `find_incomplete_by_mode()` then re-opens and re-parses every "incomplete" file a second time from scratch just to recompute the same `has_cover()` check — doubling I/O/CPU cost for `cover_only` mode on potentially large libraries, and doing so outside of the retry/backoff/blacklist machinery that the rest of `scan_incomplete.py` uses for exactly this kind of flaky-mount scenario.
   - The bare `except Exception: pass` means any transient I/O error on the second read (the same class of error `scan_incomplete.py` goes to considerable lengths to retry and record via `error_telemetry`/blacklist) causes the file to be **silently excluded** from the `cover_only` result set with no telemetry, no retry, and no warning — inconsistent with the rest of the codebase's error-handling philosophy (contrast with `_check_file_with_retry`'s `MAX_RETRIES` + `record_error` + stderr warning).
   - Concrete failure scenario: a file on a flaky NFS/SFTP mount that transiently fails to open on this second pass gets dropped from the `cover_only` incomplete list even though `find_incomplete()` correctly flagged it and even recorded/retried it earlier — the mode-specific filter silently undoes the resilience work the rest of the module invested in.
   - Suggested fix: have `_check_file_with_retry`/`find_incomplete` return the `missing_cover`/`missing_tags` breakdown alongside `is_incomplete` (or return an enum/reason) so `find_incomplete_by_mode` can filter without a second file read, and reuse the same error-telemetry path instead of swallowing exceptions.

3. **`TAGGING_MODES` dict has no enforced schema; `cover_only` entry is missing keys other modes require** (`tagging_modes.py:13-41`).
   - `strict`, `aggressive`, and `default` all define `strong_rec_thresh`; `cover_only` does not (its `match_strength` is `None` with a `# N/A` comment, and there is no `strong_rec_thresh` key at all).
   - `test_all_modes_consistently_configured` (test file, line ~66) only asserts `{"description", "skip_tagging", "skip_cover"}.issubset(config.keys())` — it does not require `strong_rec_thresh`, so this asymmetry is not even caught by the "consistency" test despite its name.
   - Concrete failure scenario: a near-future commit (per the message, "Dynamic beets config generation based on mode") that unconditionally does `config["strong_rec_thresh"]` for every mode will raise `KeyError` the first time someone runs `cover_only` mode.
   - Suggested fix: use a dataclass or `NamedTuple` with an explicit `Optional[float]` (or a required field with a documented sentinel) for `strong_rec_thresh`, or normalize all four dict entries to always include the key (e.g. `None` for `cover_only`) so downstream code can rely on the key existing.

4. **Duplicated, drifting source of truth for the same threshold values across modules** (`tagging_modes.py:25,32,39` vs. `schedule_utils.py` `describe_threshold()` introduced two commits later in 3ffeea4).
   - This commit hardcodes `strong_rec_thresh` as 0.95 / 0.85 / 0.70 for strict/default/aggressive directly in `TAGGING_MODES`.
   - The later commit 3ffeea4 adds `schedule_utils.validate_threshold()`/`describe_threshold()` with its own independent bucket boundaries (`>=0.95` ultra-conservative, `>=0.90` conservative, `>=0.80` balanced, `>=0.60` aggressive, else very-aggressive) and its commit message explicitly claims it "Integrates with existing TAGGING_MODE system" — but there is no import or shared constant between `tagging_modes.py` and `schedule_utils.py`; they are two independent literals describing the same three numbers.
   - Concrete risk: if a future change tunes `strict` from 0.95 to 0.92 in `tagging_modes.py`, `schedule_utils.describe_threshold(0.92)` will still report "Conservative" (its own hardcoded `>=0.90` bucket) while `tagging_modes.py`'s docstring/description strings still say "Very high threshold" — the two systems can silently drift out of sync with no test or code path preventing it, despite the commit message's claim of integration.
   - Suggested fix: define the canonical threshold values once (e.g. in `tagging_modes.py`, or a shared `constants.py`) and have `schedule_utils.py` import/derive from them instead of maintaining a parallel hardcoded set of bucket boundaries.

### Low / Nitpicks

1. **`tagging_modes.py` is entirely dead code at this point in the series** (whole file). Nothing in `update_music_metadata.sh` or `scan_incomplete.py` imports `tagging_modes` yet (verified via `grep -rn tagging_modes` across the tree at this commit — only the test file references it). This is explicitly called out as future work in the commit message ("Foundation for: Environment variable TAGGING_MODE in update_music_metadata.sh"), so it is not penalized as a defect, but it is worth flagging explicitly: as of this commit the module is unreachable production code, verified only by its own unit tests.
2. **Unused import** in `tests/test_tagging_modes.py:3` — `import tempfile` is never referenced anywhere in the file. Minor lint noise (would trip `flake8`/`ruff` F401).
3. **Trailing whitespace** in module docstring, `tagging_modes.py:8`: `"  - aggressive: Tag files with lower-confidence matches  "` has two trailing spaces.
4. **`match_strength` field appears decorative/unused.** `tagging_modes.py:18,24,31,38` set `"strong"`/`"weak"`/`"balanced"`/`None` string labels that duplicate information already conveyed more precisely by `strong_rec_thresh`. Nothing reads `match_strength` anywhere yet (including the tests, beyond a general dict-shape check), and once real logic consumes the config there will be two parallel ways to express "how strict is this mode" (a string label and a numeric threshold) that can drift independently of each other, similar to Medium finding #4 but within the same file.
5. **Return-tuple shape divergence not documented as intentional.** `find_incomplete_by_mode()` returns a 4-tuple `(total, incomplete, error_telemetry, mode_config)` vs. `scan_incomplete.find_incomplete()`'s 3-tuple. This is fine functionally (no shared caller yet), but worth a one-line note in the docstring clarifying this is a deliberate superset return, so future maintainers don't assume it's a drop-in replacement.

## Test Coverage Assessment

The 7 new tests are reasonable for the *static configuration* surface (`validate_mode`, `get_mode_config`, presence of `description`, and the ordering `strict.strong_rec_thresh > aggressive.strong_rec_thresh`), but they leave the module's only actual control-flow function, `find_incomplete_by_mode()`, completely unexercised:

- No test calls `find_incomplete_by_mode()` at all — not with a mocked/temp directory, not with a stubbed `scan_incomplete.find_incomplete`, nothing. The cover_only re-filtering logic (Medium finding #2), the `ValueError` raised for unknown modes (Medium finding #1), and the 4-tuple return shape are all untested.
- No test asserts the `ValueError` message/behavior for an invalid mode reaching `find_incomplete_by_mode` (only the boolean-returning `validate_mode()` is tested for `"invalid_mode"`).
- No boundary-value tests on the threshold numbers themselves (e.g., nothing asserts `0.0 <= strong_rec_thresh <= 1.0` for every mode, nothing tests what happens with a hypothetical `0.0`/`1.0`/negative/`>1` value) — reasonable to omit *if* these are treated purely as fixed internal constants rather than user input at this stage, but combined with Medium finding #4 (a real `validate_threshold()` appearing two commits later in a different file) it suggests the validation logic that should apply to these same numbers is being built in the wrong module, disconnected from where the constants actually live.
- `test_mode_config_invalid_fallback` (silent fallback) and the fact that `find_incomplete_by_mode` raises for the same invalid input are two tests that pass individually but jointly document the module's self-contradiction (Medium #1) rather than catching it.

Net: coverage is adequate for "is the dict shaped right" but inadequate for "does the one piece of behavioral logic in this commit actually work," which is the more important thing to test.

## Positive Notes

- Clear, well-documented module docstring and per-function docstrings explaining intent.
- `find_incomplete_by_mode()` reuses `scan_incomplete.find_incomplete()` rather than reimplementing directory walking/mtime tracking/retry logic — the right instinct, even though the cover_only post-filter re-reads files (Medium #2).
- Explicit `ValueError` with the list of valid modes in the message (`tagging_modes.py:65`) is good practice for a user-facing config value, if it were the consistently-applied behavior.
- The commit is honest and appropriately scoped: it does not attempt to wire the env var yet, does not touch `update_music_metadata.sh`, and calls out the follow-up work explicitly in the commit message — reduces risk of a half-integrated feature reaching a shell script prematurely.
- Threshold ordering test (`test_strict_mode_high_threshold`) is a nice relative/semantic check rather than just asserting a magic number, making it more resilient to future retuning.

## Recommendations

1. **(Medium #1)** Before wiring `TAGGING_MODE` into `update_music_metadata.sh` in a later commit, unify `get_mode_config()` and `find_incomplete_by_mode()` on one policy for invalid mode strings — prefer failing loudly (raise or a clearly logged fallback) since this will be driven by a user-editable env var.
2. **(Medium #2)** Have `scan_incomplete._check_file_with_retry`/`find_incomplete` expose the `missing_cover`/`missing_tags` distinction it already computes, so `find_incomplete_by_mode` can filter without a second full-file read and without bypassing the existing retry/telemetry infrastructure.
3. **(Medium #3)** Normalize the `TAGGING_MODES` dict schema (all keys present for all modes, ideally via a dataclass/NamedTuple) so a future unconditional `config["strong_rec_thresh"]` access can't `KeyError` on `cover_only`.
4. **(Medium #4)** When integrating with `schedule_utils.validate_threshold()`/`describe_threshold()` (already added in 3ffeea4), make one module the source of truth for the 0.95/0.85/0.70 constants and their descriptive buckets; do not maintain two independently-hardcoded copies.
5. **(Low)** Add tests that actually invoke `find_incomplete_by_mode()` (with a temp dir and small fixture files, or a monkeypatched `scan_incomplete.find_incomplete`) covering: cover_only filtering behavior, the `ValueError` path for an invalid mode, and at least one case where a file errors out during the cover_only re-check.
6. **(Low)** Remove the unused `import tempfile` in `tests/test_tagging_modes.py` and the trailing whitespace in the module docstring.
