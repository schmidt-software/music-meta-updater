# Code Review: 3ffeea4 — feat: musikbrainz matching confidence tuning

## Summary

This commit adds a `STRONG_REC_THRESH` configuration knob (documented in `.env.example`), plus two new pure functions in `schedule_utils.py` — `validate_threshold()` and `describe_threshold()` — with 5 accompanying tests. Boundary handling for the 0.0–1.0 range is correct and well tested, but the commit ships **documentation and validation logic for a feature that has no production wiring at all**: nothing in `update_music_metadata.sh` or elsewhere reads `STRONG_REC_THRESH`/`TAGGING_MODE` from the environment or injects it into the generated beets config, and the repo's own README still lists this exact capability as an open TODO. There is also a genuine off-by-one bug in `describe_threshold()`'s bucket boundaries that contradicts the commit's own `.env.example` documentation, and an unrelated feature (`COVER_SOURCES`) gets documented prematurely. Overall verdict: **do not merge as "done"** — this is scaffolding (validator + docs) mislabeled as a working feature; it needs either the actual wiring or a scope-down of the commit message/docs to "prep work."

## Files Changed

- `.env.example` (new content, +69 lines) — adds "Performance Tuning", "Tagging Behavior" (`TAGGING_MODE`, `STRONG_REC_THRESH`), "Recurring Execution", "Monitoring", and "Cover Art Fetching" documentation blocks in one shot.
- `schedule_utils.py` (+44 lines) — adds `validate_threshold(threshold_str)` and `describe_threshold(threshold)` appended after the module's `if __name__ == "__main__":` block.
- `tests/test_schedule_utils.py` (+59 lines) — adds 5 tests covering valid thresholds, invalid formats, out-of-range values, description text, and explicit boundary values.

## Findings

### Critical

**1. `STRONG_REC_THRESH` / `TAGGING_MODE` are documented and validated but never consumed anywhere — the feature does not exist at runtime.**

`update_music_metadata.sh` (unchanged by this commit) builds the beets config via a heredoc that only sets `move`, `copy`, `write`, `autotag`, `quiet`, `resume`, `log`, and a `match.preferred.media` list — there is no `match.strong_rec_thresh` key, and the script never reads `$STRONG_REC_THRESH` or `$TAGGING_MODE` from the environment:

```bash
match:
  preferred:
    media: ['CD', 'Digital Media']
```

Confirmed by repo-wide search at this commit:

```
$ git grep -n "STRONG_REC_THRESH" 3ffeea4 -- .
3ffeea4:.env.example:43:# STRONG_REC_THRESH=0.85
```
— the *only* occurrence in the entire tree is the commented-out example line. `TAGGING_MODE` fares only slightly better (it appears in `tagging_modes.py`'s dict keys and a commented-out line in `docker-compose.yml`), but nothing ever calls `os.environ.get("TAGGING_MODE")` or `get_mode_config()` from `update_music_metadata.sh`'s code path. Likewise, `validate_threshold`/`describe_threshold` (schedule_utils.py:124-166) have **zero callers outside the test file** — the pre-existing `if __name__ == "__main__":` block only exercises the cron functions, not these new ones.

Compounding this, the repository's own `README.md` "Open items / possible next steps" section (unchanged by this commit) still reads:

```
- Finer control over matching thresholds in the beets config
  (`match.strong_rec_thresh` etc.), in case mismatches occur
```

i.e., the project's own documentation says this is *not yet done*, while the commit message for 3ffeea4 claims "Impact: Per-genre/per-library customization of matching behavior … Integrates with existing TAGGING_MODE system." That claim is false as of this commit — a user who sets `STRONG_REC_THRESH=0.95` in their `.env` and runs `./update_music_metadata.sh` will see **no behavioral change whatsoever**, silently. This is the single most important thing to fix or, at minimum, disclose (e.g. mark the `.env.example` block "planned, not yet wired in").

*Suggested fix*: either (a) wire the value through — read `STRONG_REC_THRESH`/`TAGGING_MODE` in `update_music_metadata.sh`, call `validate_threshold`/`tagging_modes.get_mode_config`, fail fast on invalid input, and inject the resolved value into the `match:` block of `$BEETS_CONFIG`; or (b) if this commit is intentionally "prep work" for a later commit, say so explicitly in the commit message and mark the `.env.example` entries as not-yet-active, rather than describing a shipped user-facing feature.

### High

**1. `describe_threshold()` boundary bug: the exact value documented as "strict mode default" (0.95) is classified into the wrong bucket.**

`schedule_utils.py:156-165`:

```python
def describe_threshold(threshold):
    if threshold >= 0.95:
        return "Ultra-conservative (almost no matches)"
    elif threshold >= 0.90:
        return "Conservative (strict matching, recommended for classical/jazz)"
    ...
```

But `.env.example:38-39` (added by this very commit) documents:

```
#   - 0.99: Ultra-conservative (almost no matches)
#   - 0.95: Conservative (strict mode default) - recommended for classical/jazz
```

and `tagging_modes.py` (from commit 02fb807) sets `TAGGING_MODES["strict"]["strong_rec_thresh"] = 0.95` specifically as the "strict"/classical-jazz default. Because the first branch uses `>= 0.95` instead of `> 0.95` (or a higher cutoff like `>= 0.99`), calling `describe_threshold(0.95)` returns `"Ultra-conservative (almost no matches)"`, not `"Conservative … recommended for classical/jazz"` as the code's own sibling documentation promises. Verified directly:

```
>>> describe_threshold(0.95)
'Ultra-conservative (almost no matches)'
```

This is a straightforward off-by-one on the bucket boundary. The included test (`test_describe_threshold`, `tests/test_schedule_utils.py:106-116`) fails to catch it because it only asserts a loose substring match (`"conservative" in "ultra-conservative"` is `True`), so the bug ships with a green test suite (see Test Coverage Assessment below).

*Suggested fix*: change the first condition to `threshold >= 0.99` (matching the documented "0.99 = Ultra-conservative" / "0.95 = Conservative" split), or otherwise realign the bucket boundaries with the `.env.example` table so the two pieces of documentation agree.

**2. Unrelated `COVER_SOURCES` feature is documented in `.env.example` before it is implemented anywhere in the codebase.**

This commit's `.env.example` addition (lines 61-78) documents a `COVER_SOURCES` fallback chain (`musicbrainz,amazon,discogs,local,placeholder`) with detailed per-source semantics. At this commit, however, there is no code anywhere that reads `COVER_SOURCES`, and no "amazon"/"discogs"/"local"/"placeholder" logic exists:

```
$ git grep -n "COVER_SOURCES\|amazon\|discogs\|placeholder" 3ffeea4 -- .
3ffeea4:.env.example:64-78   (documentation only)
```

That feature is only introduced later, in commit `192b444` ("feat: incremental cover fallback chain with configurable sources") — i.e., commit #10, one after this one. Bundling its documentation into commit #9 ("musicbrainz matching confidence tuning") is scope creep unrelated to this commit's stated purpose, and — like Critical #1 — creates a window where a user reading `.env.example` at this point in history is told about a knob that has no effect. `SCAN_WORKERS`/`BATCH_IMPORT_SIZE`/`SCHEDULE`/`WEBHOOK_URL` docs added in the same hunk are at least backed by working code from earlier commits (`aa259fc`, prior scan/batch logic), so only the `COVER_SOURCES` block is actually premature — but it sits in the same commit and same file section as the equally-unwired `STRONG_REC_THRESH`, suggesting `.env.example` is being used as a running wishlist rather than a snapshot of currently-supported configuration.

*Suggested fix*: move the `COVER_SOURCES` documentation into the commit that actually implements it (`192b444`), or clearly mark it "(planned)" if it must land early.

### Medium

**1. `validate_threshold()` silently accepts `NaN` as a "valid" threshold.**

`schedule_utils.py:141-142`:

```python
if threshold < 0.0 or threshold > 1.0:
    return False, f"Threshold must be between 0.0 and 1.0, got: {threshold}"
return True, threshold
```

`float("nan")` parses successfully, and NaN compares `False` to both `< 0.0` and `> 1.0`, so the range check is silently bypassed:

```
>>> validate_threshold("nan")
(True, nan)
```

If this value is ever wired into a beets config (see Critical #1) or otherwise persisted, a NaN "confidence threshold" is nonsensical and could cause unpredictable downstream comparisons. `inf`/`-inf` are correctly rejected (they fail the range check as expected), but `nan` slips through. None of the 5 new tests exercise this case.

*Suggested fix*: add `math.isnan(threshold)` (or `not math.isfinite(threshold)`) to the validation and reject it with a clear error message; add a regression test for `"nan"`.

**2. Architecture/cohesion: `schedule_utils.py` is becoming a dumping ground for unrelated validators.**

The module's own docstring is `"""Cron schedule validation and parsing utilities."""` (schedule_utils.py:2), and its only prior content (from `aa259fc`) is cron-expression parsing (`validate_cron_expression`, `describe_cron_expression`). This commit appends matching-confidence-threshold validation (`validate_threshold`, `describe_threshold`) to the same file, with no `import` or logical link between the two concerns beyond both being "a float/string validated from an env var." A module named for cron scheduling now also owns beets-matching-confidence semantics, which is a poor fit: the natural home for threshold validation is `tagging_modes.py` (which already owns `TAGGING_MODES` and the canonical `strong_rec_thresh` values per mode from commit `02fb807`), or a new small `threshold_utils.py`. Left unchecked, this pattern (bolting unrelated helpers onto whichever existing file has a similar "validate a string, return (bool, value/error)" shape) will make `schedule_utils.py` progressively harder to navigate and test in isolation.

*Suggested fix*: move `validate_threshold`/`describe_threshold` (and their tests) into `tagging_modes.py`/`test_tagging_modes.py`, since that module is the actual owner of `strong_rec_thresh` semantics.

**3. Undocumented/underspecified precedence between `STRONG_REC_THRESH` and `TAGGING_MODE`.**

`.env.example:30` states: `# Custom matching confidence threshold for beets (overrides TAGGING_MODE if set)`. This is the *only* place this precedence rule is written down — it isn't reflected in `tagging_modes.py`'s `get_mode_config()` (which has no awareness of an env-var override), isn't in the README, and (per Critical #1) isn't implemented in code at all. Even setting aside the "not wired in" problem, the precedence rule itself is asserted in a comment with no code to back it, so it's effectively an unverifiable claim. When this is eventually implemented, the interaction needs an actual code path (e.g., `get_mode_config(mode)` merged with an optional override) and a test asserting the override behavior, not just a comment.

### Low / Nitpicks

**1. Loose test assertions mask the boundary bug (see High #1).**

`tests/test_schedule_utils.py:106-116`:

```python
def test_describe_threshold():
    descriptions = {
        0.95: "Conservative",
        ...
    }
    for threshold, expected_keyword in descriptions.items():
        desc = su.describe_threshold(threshold)
        assert expected_keyword.lower() in desc.lower()
```

Using substring containment as the assertion means `"Conservative"` matches both the "Conservative" bucket and the "Ultra-conservative" bucket, so the test can't distinguish a bucket-boundary regression from a correct result. This is a "test that can't fail the way it should" — prefer an exact string equality check (or at least assert the "Ultra-" prefix is absent) so the test actually pins down which bucket a given value falls into.

**2. Duplicated (bool, value_or_error) tuple-return pattern with no shared abstraction.**

`validate_cron_expression`/`describe_cron_expression` (pre-existing) and `validate_threshold`/`describe_threshold` (this commit) independently reimplement the same "validate → (True, value) | (False, message)" shape with no shared helper. Not a bug, but a missed opportunity for a small shared utility (e.g., a generic `_validated(value, ok, err_msg)` tuple constructor) now that the pattern is used twice in the same file.

**3. `validate_threshold`'s `not threshold_str` check also rejects `"0"`-like falsy-but-valid inputs only coincidentally works today.**

`if not threshold_str:` (schedule_utils.py:133) relies on the input being a non-empty string; this is fine for the current call sites (all pass strings), but the function has no type-guard the way `validate_cron_expression` does (`if not schedule or not isinstance(schedule, str):`, line 20 of the same file). If `validate_threshold` is ever called with a non-string (e.g., `None`, or an `int`/`float` argument), `not threshold_str` behaves inconsistently (e.g., `not 0.0` is `True`, incorrectly reporting "cannot be empty" for a literal zero float) compared to its sibling function. Minor inconsistency worth aligning for defensive symmetry.

## Test Coverage Assessment

The 5 new tests are reasonably thorough for the "happy path" of `validate_threshold` but have real gaps:

- **Boundary values (0.0, 1.0)**: covered correctly and explicitly (`test_threshold_boundaries`), including the "just outside" cases (1.1, -0.1). Good.
- **Negative / >1.0 rejection**: covered (`test_validate_threshold_out_of_range`: "-0.5", "1.5", "2.0").
- **Non-numeric input**: covered for common cases ("abc", "1.5%", "", "0.85.50"), but **not** for `"nan"` or `"inf"`/`"-inf"`, which is exactly where the Medium #1 bug lives (NaN passes as "valid"). This is a real coverage gap, not just a nice-to-have.
- **Documented threshold buckets (0.95/0.85/0.70/0.50)**: nominally covered by `test_describe_threshold`, but the assertion style (substring containment) is weak enough that it does not actually catch the High #1 boundary bug — the test passes today despite `describe_threshold(0.95)` returning the wrong bucket. This is the most important coverage gap: the test looks like it validates the documented mapping, but doesn't.
- **No integration/wiring test**: there is no test anywhere that asserts `STRONG_REC_THRESH` or `TAGGING_MODE`, when set, actually change the generated beets config or the arguments passed to `beet import`. Given Critical #1, such a test does not exist because the feature does not exist; adding one would have caught the gap before merge.

Net assessment: the unit tests are well-organized and readable, but they test the validator’s internals in isolation without ever asking "does this validator's output do anything?" — which is precisely where the commit falls short.

## Positive Notes

- `validate_threshold`/`describe_threshold` are small, single-purpose, well-documented (clear docstrings with Args/Returns) pure functions — easy to unit test in isolation, which the commit does.
- The `(True, value) | (False, error_msg)` return convention is consistent with the pre-existing `validate_cron_expression` in the same file, reducing surprise for anyone already familiar with that module's API shape.
- Range-check boundary logic itself (`< 0.0 or > 1.0`, inclusive on both ends) is correct and matches the documented "Range: 0.0 to 1.0" in `.env.example`, and is explicitly tested at both ends.
- The `.env.example` tuning guidance (the 0.99/0.95/0.85/0.70/0.50 table) is genuinely useful, concrete, and would be good user-facing documentation once the feature is actually wired in.
- No secrets or real credentials are present in `.env.example` — all values are placeholders (`your-webhook.example.com`, empty API key), safe to commit.

## Recommendations

1. **(Blocking)** Wire `STRONG_REC_THRESH`/`TAGGING_MODE` into `update_music_metadata.sh`'s generated `$BEETS_CONFIG` (`match.strong_rec_thresh`), calling `validate_threshold`/`tagging_modes.get_mode_config` at script start and failing fast on invalid input — or, if this commit is meant purely as prep work, rewrite the commit message and `.env.example` comments to say so explicitly instead of claiming a shipped user-facing capability.
2. Fix the `describe_threshold` boundary bug: use `>= 0.99` (or equivalent) for the "Ultra-conservative" bucket so `0.95` maps to "Conservative," matching the commit's own `.env.example` table.
3. Add `math.isnan`/`math.isfinite` guard to `validate_threshold`, plus test cases for `"nan"`, `"inf"`, `"-inf"`.
4. Strengthen `test_describe_threshold` to assert exact/qualified strings (or explicitly check for absence of "Ultra-") so bucket-boundary regressions are actually caught.
5. Relocate `validate_threshold`/`describe_threshold` (and their tests) out of `schedule_utils.py` into `tagging_modes.py`, which already owns the `strong_rec_thresh` domain concept — keep `schedule_utils.py` scoped to cron parsing.
6. Move the `COVER_SOURCES` `.env.example` documentation block into the commit that implements it (`192b444`) rather than pre-announcing it here.
7. Once wired, document the `STRONG_REC_THRESH` vs. `TAGGING_MODE` precedence rule in code (not just a `.env.example` comment) and add a test asserting the override behavior.
