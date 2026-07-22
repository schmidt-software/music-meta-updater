# Code Review: 192b444 — feat: incremental cover fallback chain with configurable sources

## Summary

This commit adds a new, self-contained `cover_sources.py` module (158 lines) plus 11 tests (100 lines) implementing parsing, validation, YAML-config generation, and human-readable description for a configurable cover-art fallback chain. The code is clean and internally consistent with prior modules in the series (e.g. the `(valid, error)` tuple convention from `schedule_utils.py`), and basic happy-path parsing works correctly (including case-insensitivity and whitespace trimming). However, the module is **completely unwired**: nothing in `update_music_metadata.sh` (or anywhere else) calls `parse_cover_sources_string()` or `generate_beets_fetchart_config()`, and the actual beets config generation in `update_music_metadata.sh` still hardcodes its `fetchart:` section with no `sources:` key at all — so `COVER_SOURCES` currently has zero effect on runtime behavior. The commit message also overstates the diff, claiming a `.env.example` update that in fact happened in the *previous* commit (3ffeea4). Validation logic has real gaps (no duplicate detection, confusing error messages on malformed input, and `local`/`placeholder` being silently dropped from the generated beets config despite being advertised as part of the "extended chain"). No security-relevant YAML injection is currently possible only because the interpolated values are drawn from a small hardcoded internal map, not directly from user input — but the manual f-string YAML construction is fragile and should not be relied upon if the mapping ever becomes more dynamic.

## Files Changed

- `cover_sources.py` (new, 158 lines): defines `DEFAULT_COVER_SOURCES`/`EXTENDED_COVER_SOURCES` lists, `validate_cover_sources()`, `parse_cover_sources_string()`, `generate_beets_fetchart_config()`, `describe_cover_sources()`, plus a `__main__` self-test block.
- `tests/test_cover_sources.py` (new, 100 lines): 11 unit tests covering validation and parsing happy/invalid paths, config generation, and descriptions.
- `.env.example`: **not actually changed by this commit** (see Medium finding #1) despite the commit message claiming "Updated .env.example with COVER_SOURCES parameter + documentation" — that documentation was added in the prior commit 3ffeea4.

## Findings

### Critical

None found.

### High

1. **Feature is entirely dead code / not wired into the actual beets config path** — `cover_sources.py:75-98` (`generate_beets_fetchart_config`).
   A repo-wide search confirms `cover_sources.py`'s functions are referenced nowhere outside the module itself and its test file:
   ```
   $ grep -rln "COVER_SOURCES\|cover_sources" --include="*" . | grep -v .git/
   cover_sources.py
   .env.example
   tests/test_cover_sources.py
   ```
   Meanwhile `update_music_metadata.sh:216-244` generates the real beets config used at runtime, with a hardcoded `fetchart:` block that has **no `sources:` key at all**:
   ```sh
   fetchart:
     auto: yes
     force: no
     enforce_ratio: no
   ```
   `$COVER_SOURCES` is never read by `update_music_metadata.sh`. So despite `.env.example` documenting `COVER_SOURCES=musicbrainz,amazon,discogs,local,placeholder` as a supported, tunable setting, setting this environment variable today has **zero effect** on the running tool. This is consistent with the pattern of prior "foundation" commits in this series (e.g. tagging modes, schedule config) that ship validation/parsing logic ahead of integration, but it should be called out explicitly here since the commit message ("Impact: Higher cover art success rate through fallback chain") implies the feature is live, which it is not.

2. **`EXTENDED_COVER_SOURCES` silently degrades to `DEFAULT_COVER_SOURCES` in the generated config, with no warning** — `cover_sources.py:107-118` (`source_plugins` mapping and filter loop).
   ```python
   source_plugins = {
       "musicbrainz": "MusicBrainz",
       "amazon": "Amazon",
       "discogs": "Discogs",
   }
   beets_sources = []
   for source in sources:
       if source in source_plugins:
           beets_sources.append(source_plugins[source])
   ```
   Verified empirically:
   ```
   >>> cs.generate_beets_fetchart_config(cs.EXTENDED_COVER_SOURCES)
   fetchart:
     ...
     sources: ['MusicBrainz', 'Amazon', 'Discogs']
   ```
   i.e. passing the "extended chain with local cache and placeholder" (the headline feature described in the commit message: "Support for both standard (online) and extended (with cache) modes") produces a config that is byte-for-byte identical (modulo whitespace) to the default chain — `local` and `placeholder` are silently dropped, with no exception, warning, or log message. A user who configures `COVER_SOURCES=musicbrainz,amazon,discogs,local,placeholder` believing they've enabled offline/cache fallback gets no such behavior and no indication anything was ignored. If `local`/`placeholder` are intentionally not beets-plugin concepts and are meant to be handled elsewhere (outside beets), that should be documented in the function's docstring; as written it reads as an oversight.

### Medium

1. **Commit message misrepresents its own diff regarding `.env.example`.**
   The commit message states: *"Updated .env.example with COVER_SOURCES parameter + documentation … Tuning tips for different genres … Examples: standard chain vs. extended with local cache."* However `git show --stat 192b444` shows only two files changed: `cover_sources.py` and `tests/test_cover_sources.py`. The `COVER_SOURCES` documentation in `.env.example` (lines 61-78) was actually introduced in the prior commit `3ffeea4` ("feat: musikbrainz matching confidence tuning"):
   ```
   $ git show 3ffeea4 -- .env.example | grep -n COVER_SOURCES
   96:+#   COVER_SOURCES=musicbrainz,amazon,discogs
   97:+#   COVER_SOURCES=musicbrainz,amazon,discogs,local,placeholder
   98:+# COVER_SOURCES=musicbrainz,amazon,discogs
   ```
   This is a documentation/provenance accuracy issue in the commit history itself — worth fixing/squashing before this series is pushed, since it will confuse anyone reading `git log` later trying to understand when the `COVER_SOURCES` contract was introduced vs. when it was implemented.

2. **`validate_cover_sources()` does not check chain-level semantics — duplicates are silently accepted** — `cover_sources.py:32-49`.
   ```python
   for source in sources:
       if source not in valid_sources:
           return False, f"Unknown cover source: {source}. Valid: {', '.join(valid_sources)}"
   return True, None
   ```
   Verified: `validate_cover_sources(["musicbrainz", "musicbrainz"])` returns `(True, None)`. The function only validates each element in isolation against the allowed set; it never checks the list as a whole (no duplicate detection, no minimum/maximum length beyond non-empty, no requirement that at least one *usable* beets source is present). A duplicate entry like `musicbrainz,musicbrainz,amazon` passes validation, then reaches `generate_beets_fetchart_config()` and produces `sources: ['MusicBrainz', 'MusicBrainz', 'Amazon']` — harmless to beets itself, but it indicates the "chain validation" advertised in the commit message ("validate_cover_sources(): chain validation") is really just per-item membership checking, not true chain validation. Given the task description explicitly asks about this, it's worth flagging: the docstring's use of the word "chain" oversells what's checked.

3. **Malformed comma-separated input produces a confusing, low-quality error message** — `cover_sources.py:86-101` (`parse_cover_sources_string`).
   ```python
   sources = [s.strip().lower() for s in sources_str.split(",")]
   valid, error = validate_cover_sources(sources)
   ```
   A trailing comma (`"musicbrainz,amazon,"`) or a double comma (`"musicbrainz,,amazon"`) produces an empty-string element in the list, which fails validation with:
   ```
   Unknown cover source: . Valid: amazon, discogs, placeholder, local, musicbrainz
   ```
   The empty source name renders as nothing after the colon, making the error confusing to an end user editing `.env` (they won't obviously see *why* it's failing — a stray trailing comma is an easy, very plausible typo). A friendlier implementation would either (a) filter out empty tokens produced by `split(",")` before validation (treating trailing/doubled commas as benign), or (b) special-case empty tokens with a clearer message like `"Empty source name found — check for stray commas."` Additionally, `', '.join(valid_sources)` iterates over a `set`, so the "Valid: ..." list order is non-deterministic between runs/Python versions (cosmetic, but worth using a sorted list for stable, testable error messages).

4. **YAML generated via raw f-string interpolation of a Python list, not a YAML serializer** — `cover_sources.py:137-146`.
   ```python
   config = f"""fetchart:
     auto: yes
     force: no
     enforce_ratio: no
     sources: {beets_sources}
     ...
   """
   ```
   `{beets_sources}` embeds the Python `repr()` of a list (e.g. `['MusicBrainz', 'Amazon']`), which happens to also be valid YAML flow-sequence syntax with single-quoted scalars, so `yaml.safe_load()` currently parses it correctly. However, this is incidental, not guaranteed: it works only because (a) `beets_sources` values are always drawn from the fixed, hardcoded `source_plugins` dict (never straight from user input) and (b) none of those three fixed strings contain a character that would break YAML/Python repr equivalence (quotes, colons, backslashes). There is no actual injection vector today given the current fixed mapping, but this is a latent landmine: if a future change makes `source_plugins` more dynamic (e.g. allowing user-supplied display names, or expanding to arbitrary plugin config), the same pattern would become YAML-injectable (a value containing `\n` or `: ` could inject arbitrary YAML keys into the beets config that beets will then parse and act on). Recommend switching to `yaml.safe_dump({"fetchart": {...}})` (or at minimum `yaml.safe_dump(beets_sources)` for just the list) now, while the fix is cheap, rather than waiting until it's a real vulnerability.

### Low / Nitpicks

1. **`describe_cover_sources()` silently drops unrecognized entries instead of surfacing an error** — `cover_sources.py:151-173`.
   ```python
   for source in sources:
       if source in descriptions:
           parts.append(descriptions[source])
   return " → ".join(parts) if parts else "No sources configured"
   ```
   Unlike `validate_cover_sources`/`generate_beets_fetchart_config`, this function has no validation step and will quietly omit unknown source names from the description with no indication that something was dropped. If called directly on unvalidated input (there's nothing in the API that prevents this — it's a public module-level function), a typo'd source just vanishes from the human-readable output rather than showing up as an error, which could mask misconfiguration from a user reading a status/summary line.

2. **`validate_cover_sources` error message says "list" but the empty-check fires before the type-check** — `cover_sources.py:73-77`.
   ```python
   if not sources:
       return False, "Cover sources list cannot be empty"
   if not isinstance(sources, list):
       return False, "Cover sources must be a list"
   ```
   Passing `sources=""` (empty string) or `sources={}` (empty dict) hits the first branch and returns "Cover sources list cannot be empty" even though the actual problem is also a type mismatch — a minor message-precision nit, not a functional bug, but slightly misleading when debugging.

3. **`__main__` self-test block duplicates the pytest suite's coverage with print-based ad hoc checks** — `cover_sources.py:176-190`. Not harmful, but it's parallel/duplicate test infrastructure (a print-and-eyeball smoke test) alongside a proper pytest suite for the same module; consider dropping it once the pytest suite is trusted, to avoid two sources of truth for "does this work."

4. **`valid_sources` recomputed as a literal set on every call** — `cover_sources.py:71`. Minor: could be hoisted to a module-level constant (`VALID_COVER_SOURCES = {"musicbrainz", ...}`) shared by both `validate_cover_sources` and `parse_cover_sources_string`'s error path, and reused for the sorted "Valid: ..." message (see Medium #3) — avoids recomputing the set on every call and gives one place to add a new source name in the future instead of needing to keep `valid_sources` here in sync with `DEFAULT_COVER_SOURCES`/`EXTENDED_COVER_SOURCES`/`source_plugins`/`descriptions` (four separate places currently list the same five source names — see Recommendations).

## Test Coverage Assessment

The 11 tests are real unit tests (not just "returns a string" smoke checks) and do cover several important paths: empty-string-to-default fallback, invalid source rejection (including mixed valid/invalid), non-list rejection, and the fact that `generate_beets_fetchart_config` raises `ValueError` on invalid input. That said, coverage has notable gaps directly relevant to the module's stated purpose:

- **No test exercises malformed comma-separated strings** (trailing comma, double comma, leading comma) — exactly the input-hygiene concern called out in the task, and confirmed above to produce a confusing error message. This is an easy, high-value test to add and it's missing.
- **No test exercises whitespace/case normalization** (e.g. `" MusicBrainz , AMAZON "`) even though `parse_cover_sources_string` explicitly does `.strip().lower()` — the one behavior most worth locking down with a regression test is untested.
- **No test for duplicate source names** — since `validate_cover_sources` currently accepts duplicates, there's no test asserting either the current (permissive) behavior or a desired stricter one; the behavior is effectively unspecified.
- **`generate_beets_fetchart_config` is never tested with `EXTENDED_COVER_SOURCES` or any `local`/`placeholder`-containing input** — the exact scenario shown above where `local`/`placeholder` get silently dropped is completely uncovered. Given "extended chain" is one of the two headline features of this commit, this is the most significant coverage gap.
- Tests check substring membership (`"fetchart:" in config`, `"MusicBrainz" in config`) rather than parsing the output as YAML (e.g. `yaml.safe_load(config)["fetchart"]["sources"] == [...]`) or asserting exact source order is preserved. Substring checks would pass even if the YAML were subtly malformed elsewhere in the string.

## Positive Notes

- Consistent, idiomatic use of the `(valid_or_value, error)` tuple-return convention already established by `schedule_utils.py` in this series — good cross-module consistency rather than inventing a new error-handling style.
- `parse_cover_sources_string`'s `.strip().lower()` normalization is a genuinely good defensive default for a comma-separated env-var-sourced config value, and it does work correctly for the whitespace/case cases tested manually above.
- `DEFAULT_COVER_SOURCES` vs. `EXTENDED_COVER_SOURCES` ordering matches the documented priority (musicbrainz → amazon → discogs, + local + placeholder) exactly as described in both the commit message and the module docstring.
- Module is small, single-purpose, and easy to read; docstrings on every public function state args/returns clearly.
- Graceful degradation in `generate_beets_fetchart_config` (falling back to `["MusicBrainz"]` if no sources map to a beets plugin) is a sensible defensive default, even though it currently can't be reached via any valid input other than an all-`local`/`placeholder` chain.

## Recommendations

1. **Wire the module in before merging/pushing further, or explicitly label it experimental/unused.** As it stands, `COVER_SOURCES` is documented in `.env.example` and fully implemented in `cover_sources.py` but has no effect at runtime because `update_music_metadata.sh` never calls it. Either integrate `generate_beets_fetchart_config()`'s output into the `BEETS_CONFIG` heredoc in `update_music_metadata.sh` (replacing/augmenting the current hardcoded `fetchart:` block around line 236), or add a `# NOT YET WIRED IN` note to `.env.example` so users don't believe the setting works today.
2. **Fix or document the `local`/`placeholder` silent-drop behavior** in `generate_beets_fetchart_config` — either raise/warn when non-beets sources are present in the input, or clarify in the docstring that these two entries are handled outside of beets entirely (and explain where/how) and thus intentionally excluded from the generated YAML.
3. **Add duplicate detection to `validate_cover_sources`** (e.g. `if len(sources) != len(set(sources))`) if "chain validation" is meant to mean more than per-item membership checking.
4. **Correct the commit history/description issue** before this series is pushed — either amend this commit's message to stop claiming a `.env.example` change that didn't happen here, or note in follow-up documentation that the env var was documented ahead of implementation across two commits.
5. **Add the missing tests** identified above: malformed comma strings (trailing/double commas), whitespace+case normalization, duplicate sources, and `generate_beets_fetchart_config(EXTENDED_COVER_SOURCES)` to lock down (or intentionally fix) the silent-drop behavior.
6. **Replace manual f-string YAML construction with `yaml.safe_dump`**, now while it's cheap, to remove the latent injection risk described in Medium #4 and to make the generated config trivially more correct/robust (e.g. it would no longer rely on Python list-repr happening to match YAML flow-sequence syntax).
7. **Consolidate the "five valid source names" list**, which currently exists redundantly in four places (`valid_sources` in `validate_cover_sources`, `DEFAULT_COVER_SOURCES`/`EXTENDED_COVER_SOURCES`, `source_plugins` keys, and `descriptions` keys) into a single module-level constant to prevent drift as sources are added/removed.
