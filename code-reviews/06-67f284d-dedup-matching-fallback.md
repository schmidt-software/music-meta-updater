# Code Review: 67f284d — feat: deduplization + matching fallback with heuristics

## Summary
This commit adds a new `metadata_fallback.py` module with a single heuristic function, `extract_from_path()`, that guesses artist/album metadata from folder-name patterns, plus a new `failed_matches` SQLite table in `scan_incomplete.py` (`init_failed_matches_db`/`record_failed_match`/`get_failed_match`) to track files that failed to match. All new SQL is correctly parameterized and the attempt-increment logic is functionally correct for single-threaded use. However, `extract_from_path()` has a real, easily-triggered logic bug: it only ever looks at the *last two* path segments, so any extra nesting (a "Disc 1"/"Side A"/box-set subfolder — extremely common in real libraries) causes it to misparse the whole "Artist - Album" folder name as the artist and the disc-subfolder as the album, and the documented "Artist/track.mp3 (artist only)" pattern is effectively unreachable for any realistic absolute path. Test coverage is present (7 new tests) but mostly asserts weak/tautological conditions (`isinstance(result, dict)`, `'x' in result or 'y' in result`) that would pass even with the buggy output demonstrated below, so it does not actually protect against regressions. Despite the commit title and module docstring both saying "deduplicate"/"Deduplicate", there is no deduplication logic anywhere in this diff — it is 100% fallback-matching and failure-tracking code; this is a misleading commit message. None of the new code is wired into `main()` or `update_music_metadata.sh` yet, which is consistent with the commit message's stated "foundation" framing and is noted here for visibility rather than penalized.

## Files Changed
- `metadata_fallback.py` (new, 72 lines) — `extract_from_path()`: heuristic folder-name parser for fallback artist/album extraction; includes a `__main__` smoke-test block.
- `scan_incomplete.py` (+46 lines, appended after `main()`) — `init_failed_matches_db()`, `record_failed_match()`, `get_failed_match()`: new `failed_matches` SQLite table and CRUD-style helpers.
- `tests/test_metadata_fallback.py` (new, 44 lines) — 5 tests for `extract_from_path()`.
- `tests/test_scan_incomplete.py` (+49 lines) — 2 tests for the new failed-matches DB functions.

## Findings

### Critical
None found.

### High

1. **`extract_from_path()` misparses any path with more than 2 directory levels below the library root, mangling the "Artist - Album" pattern it claims to support.**
   File: `metadata_fallback.py:21-52` (post-commit content at `git show 67f284d:metadata_fallback.py`).
   The function only inspects `path_parts[-1]` and `path_parts[-2]` (the last two directory components), with no concept of a library root to anchor "depth." As soon as a release has one extra level of nesting — e.g. multi-disc sets with `Disc 1`/`Side A`/`CD1` subfolders, which are common in real music collections — the hyphen-pattern branch (meant to catch `"Artist - Album"`) never even sees the folder that contains the hyphen, because that folder is now `path_parts[-2]`, not `path_parts[-1]`. It falls through to the generic two-folder branch, which then treats the *entire* `"Artist - Album"` string as the artist and the disc subfolder as the album. Verified empirically:
   ```
   extract_from_path("/mnt/music/Pink Floyd - The Wall/Side A/01 - In The Flesh.flac")
   -> {'artist': 'Pink Floyd - The Wall', 'album': 'Side A'}
   ```
   Expected: `artist='Pink Floyd'`, `album='The Wall'`. This is exactly the third example baked into the module's own `if __name__ == "__main__":` smoke-test block (`metadata_fallback.py:65-71`, path `"/mnt/music/Pink Floyd - The Wall/Side A/01 - In The Flesh.flac"`), meaning the author's own manual test case would have shown the wrong answer if actually inspected — but no test asserts on this case, and the smoke-test block just prints instead of asserting.
   Suggested fix: search all path components under a known library-root boundary (or at minimum, scan upward past subfolders that look like disc/side/volume markers — `disc\s*\d+`, `cd\s*\d+`, `side\s*[a-d]`, `part\s*\d+`, `bonus`, etc.) for the actual `"Artist - Album"` or `Artist/Album` folder before falling back to the naive "last two components" heuristic.

2. **The documented "Artist/track.mp3 (artist only)" pattern is unreachable for realistic absolute paths, contradicting the docstring.**
   File: `metadata_fallback.py:14-16` (docstring) vs. `metadata_fallback.py:31-46` (implementation).
   The docstring advertises three patterns, the third being a bare `Artist/track.mp3` (no album folder). In the implementation, the `if second_last:` branch (line 33) fires and treats `second_last` as artist / `last_folder` as album *whenever `path_parts` has ≥2 entries*, which is true for essentially every real absolute path with more than one directory level. The "artist only" fallback (the final `if path_parts:` block, lines 49-52) is only reached when `second_last` is falsy, which in practice only happens when the path has exactly one non-empty directory component before the filename (e.g. a top-level `"/Artist/track.mp3"`). Verified empirically:
   ```
   extract_from_path("/mnt/music/artist/track.mp3")   -> {'artist': 'music', 'album': 'artist'}
   extract_from_path("relative/path/track.mp3")        -> {'artist': 'relative', 'album': 'path'}
   ```
   In both cases there is no album at all — the file sits directly in an artist folder — yet the function fabricates an "album" out of whatever the parent-of-parent directory happens to be (here, a generic mount-point/library-root segment like `music`, or an arbitrary path prefix). This is a correctness bug that will pollute `fallback_album` with garbage (mount points, library-root names, arbitrary path prefixes) for exactly the "single-level" library layout the docstring claims to support. Fix: only take the two-folder branch when there is independent evidence the path actually has an album layer (e.g., require the grandparent-of-file component to itself sit under a recognizable library root, or otherwise make "artist only" reachable by design rather than by accident of string-split arithmetic).

3. **Commit message and module docstring both claim "deduplication" that does not exist in this diff.**
   File: commit message (title: "feat: deduplization + matching fallback with heuristics") and `metadata_fallback.py:2` (`"""Deduplicate and extract metadata from folder structure for fallback matching.`).
   Nothing in this diff identifies duplicate tracks, compares fingerprints/hashes, or removes/merges duplicate entries. The entire change is (a) a folder-name-based metadata-extraction heuristic and (b) a failed-match tracking table. This is a documentation/process problem: a future reader (or `git log --grep=dedup`) will believe deduplication logic was introduced here and will not find it, and the module docstring actively misdescribes its own contents. Fix: rename the commit (if not yet pushed/rewritten history is acceptable) or at minimum correct the module docstring to describe only fallback extraction, and drop "deduplication" from both until an actual dedup commit lands.

### Medium

4. **Two weak (tautological) test assertions provide false confidence and would not catch either High finding above.**
   File: `tests/test_metadata_fallback.py:10-15` and `:24-28`.
   ```python
   def test_extract_artist_album_from_slash_pattern():
       ...
       assert result.get('artist') == 'Abbey Road' or result.get('artist') == 'The Beatles'
   ```
   and
   ```python
   def test_extract_with_year():
       ...
       assert 'artist' in result or 'album' in result
   ```
   These assertions accept either a correct or an incorrect answer as "passing" (the first literally allows the artist/album fields to be swapped; the second only checks that *a* key exists, not that its value is right). `test_extract_minimal` (`:31-36`) and `test_extract_empty_on_no_match` (`:39-43`) only assert `isinstance(result, dict)`, which is true even for the garbage output shown in Findings #1 and #2. None of the 5 new tests exercises a nested (multi-level, disc/side-subfolder) path, so the bug in Finding #1 has zero test coverage, and the "artist-only" test path (`/music/artist/track.mp3`, `:33`) exercises exactly the buggy case in Finding #2 without asserting on its (wrong) output. Recommend tightening these assertions to exact expected dicts and adding an explicit nested-folder regression test.

5. **`record_failed_match()`'s read-then-write increment is not safe under concurrent access, despite the codebase's own parallel-scanning design.**
   File: `scan_incomplete.py:468-484`.
   ```python
   cursor = conn.execute(
       "SELECT match_attempts FROM failed_matches WHERE filepath = ?",
       (filepath,)
   )
   row = cursor.fetchone()
   attempts = (row[0] if row else 0) + 1

   conn.execute(
       """INSERT OR REPLACE INTO failed_matches
          (filepath, error_reason, match_attempts, last_attempt_time, fallback_artist, fallback_album)
          VALUES (?, ?, ?, ?, ?, ?)""",
       (filepath, error_reason, attempts, now, fallback_artist, fallback_album)
   )
   ```
   This SELECT-then-INSERT-OR-REPLACE is a classic TOCTOU race. `scan_incomplete.py`'s own module docstring advertises "Parallel file checking: uses ThreadPoolExecutor for fast multi-core scanning," and each worker presumably opens its own connection (or the shared connection is accessed from multiple threads) — if two attempts for the same `filepath` are ever recorded concurrently (e.g., a retried match within the same threaded scan), one increment can be lost. It is not exploitable today since nothing yet calls `record_failed_match()` from the parallel scan path, but it should be fixed before wiring this into the threaded scanner, e.g. via `UPDATE ... SET match_attempts = match_attempts + 1 WHERE filepath = ?` guarded by `INSERT OR IGNORE` first, or a single upsert statement (`INSERT ... ON CONFLICT(filepath) DO UPDATE SET match_attempts = match_attempts + 1, ...`), both of which are atomic within SQLite without needing the round-trip SELECT.

6. **No filtering of path components that begin with `.` (or are `..`) in the two-folder branch, unlike the single-folder "last resort" branch.**
   File: `metadata_fallback.py:31-46` vs. `:49-52`.
   The final fallback branch explicitly guards against hidden/dot folders (`if last_folder and not last_folder.startswith('.')`, line 51), but the earlier two-folder branch has no equivalent guard. Verified empirically:
   ```
   extract_from_path("/mnt/music/.hidden/track.mp3") -> {'artist': 'music', 'album': '.hidden'}
   ```
   and a crafted path with a literal `..` component:
   ```
   extract_from_path("/a/../track.mp3") -> {'artist': 'a', 'album': '..'}
   ```
   `extract_from_path()` itself does no filesystem I/O, so this is not an active path-traversal vulnerability in this commit, but it is an inconsistency (one branch filters dot-prefixed names, the other doesn't) and a latent risk: `fallback_artist`/`fallback_album` values are explicitly slated ("Foundation for: Applying fallback artist/album tags to files") to be written into audio tags or possibly used to construct destination paths in a later commit. A literal `".."` or hidden-folder name silently becoming a tag value or (later) a path segment should be filtered at the source. Recommend applying the same `startswith('.')` / `in ('.', '..')` guard uniformly across both branches.

### Low / Nitpicks

7. **No Unicode normalization applied to extracted names.**
   File: `metadata_fallback.py:26-52`. Folder names are only `.strip()`-ped, never normalized (e.g. via `unicodedata.normalize('NFC', ...)`). On macOS (the apparent dev/deployment environment, given `/mnt/music/...`-style Docker paths alongside a `Dockerfile`/`docker-compose.yml` in this repo), HFS+/APFS commonly presents decomposed (NFD) Unicode in filenames. Two visually identical artist names in different normalization forms would produce different `fallback_artist` strings, which could later cause spurious "different artist" mismatches once this data feeds into tagging or matching logic. Not a bug today (no downstream consumer yet), but worth normalizing at the extraction boundary before this data is used for matching.

8. **`second_last`'s `None` vs. falsy-empty-string handling is accidental, not intentional.**
   File: `metadata_fallback.py:27-28, 33`. `second_last = path_parts[-2] if len(path_parts) >= 2 else None` is written as if `None` is the only "absent" sentinel, but `path_parts[-2]` frequently evaluates to `""` (empty string) rather than `None` for paths with leading/doubled separators — and the code's correctness for the single-level absolute-path case (Finding #2's one working example, `/Artist/track.mp3`) depends entirely on `if second_last:` treating that empty string as falsy. This works by coincidence rather than by documented design; a future refactor that changes the sentinel to something falsy-but-non-empty (or that "fixes" the `None` check to `is not None`) would silently break the one case that currently works. Recommend making the depth/anchor logic explicit rather than relying on this.

9. **Commit-message typo:** "deduplization" should be "deduplication" (compounding Finding #3 — the word is misspelled in addition to being inapplicable).

10. **No README documentation for the new `failed_matches` table or `metadata_fallback` module.** `README.md` (228 lines at this commit) has no mention of `fallback`, `failed_match`, or `dedup` anywhere. Given the commit is explicitly framed as a "foundation" for future work, deferring README updates until the feature is actually wired up and user-facing is reasonable and is not counted against this commit — flagged here only for completeness/traceability.

11. **Dead code / not yet wired in (expected, per commit message, but noted for visibility).** `extract_from_path()` is called only from `metadata_fallback.py`'s own `__main__` block and from its test file; `init_failed_matches_db`/`record_failed_match`/`get_failed_match` are called only from their tests. Neither `scan_incomplete.py:main()` nor `update_music_metadata.sh` references any of the new functionality. This matches the commit message's own "Foundation for: Applying fallback artist/album tags to files after beet match fails ... (future)" framing, so it is not treated as a defect, but is called out since the task explicitly asks to flag unused/unwired new functions.

## Test Coverage Assessment
7 new tests were added (5 for `metadata_fallback`, 2 for `failed_matches`), and the DB-side tests (`test_init_failed_matches_db`, `test_record_failed_match`) are solid — they assert exact values, verify table creation, and correctly check the increment behavior (`attempts == 1` then `attempts == 2`) using real SQLite round-trips. The `metadata_fallback` tests are weaker: only `test_extract_from_hyphen_pattern` asserts exact expected values; the other four either accept multiple possible answers as correct (`test_extract_artist_album_from_slash_pattern`, `test_extract_with_year`) or only check `isinstance(result, dict)` (`test_extract_minimal`, `test_extract_empty_on_no_match`), which passes regardless of whether the extracted values are actually right. As a direct consequence, none of the 5 tests catches either High-severity bug found above (multi-level/disc-subfolder mis-parsing, or the unreachable "artist only" pattern for realistic paths) even though both were reproduced with minimal, plausible real-world paths. The suite covers the happy path for the flat two-level and hyphenated one-level cases only; it does not cover nested/multi-disc paths, hidden folders, non-ASCII names, trailing slashes, or the "artist-only" pattern the docstring claims to support.

## Positive Notes
- All new SQL in `scan_incomplete.py` (`init_failed_matches_db`, `record_failed_match`, `get_failed_match`) uses parameterized queries (`?` placeholders) throughout — no string-formatted SQL, no injection risk in the DB layer.
- The `failed_matches` schema is sensible and minimal (filepath as primary key, `NOT NULL` on `error_reason`/`match_attempts`/`last_attempt_time`), and the increment logic (`row[0] + 1` on first write, defaulting to `0 + 1 = 1` when absent) is logically correct for the non-concurrent case, and is directly verified by `test_record_failed_match`'s two-call increment check.
- The commit is honest about scope in its body ("Foundation for: ... (future)"), which appropriately sets expectations that fallback-tag application and CSV import are not yet implemented.
- `extract_from_path()` includes a runnable `__main__` smoke-test block with realistic example paths, which is a nice habit even though (per Finding #1) its own third example silently demonstrates the bug.

## Recommendations
1. **(High priority)** Fix `extract_from_path()` to anchor on the actual artist/album folder pair rather than blindly using "last two path components" — at minimum, skip over folders that look like disc/side/part markers before applying the hyphen or two-folder heuristics (Finding #1), and make the "artist only" pattern reachable by design, not by string-split accident (Finding #2).
2. **(High priority)** Rename/reword the commit message and the `metadata_fallback.py` module docstring to drop "deduplicate/deduplication," since no such logic exists in this diff (Finding #3). If this commit series is later squashed/rewritten before pushing, consider fixing the title now while it is still local-only.
3. **(Medium)** Replace the tautological test assertions in `test_extract_artist_album_from_slash_pattern`, `test_extract_with_year`, `test_extract_minimal`, and `test_extract_empty_on_no_match` with exact expected-value assertions, and add a dedicated regression test for nested/disc-subfolder paths (Finding #4).
4. **(Medium)** Before wiring `record_failed_match()` into the threaded scan path, replace the SELECT-then-INSERT-OR-REPLACE with an atomic upsert (`INSERT ... ON CONFLICT DO UPDATE SET match_attempts = match_attempts + 1`) to avoid lost increments under concurrent calls (Finding #5).
5. **(Medium)** Apply the same dot-folder/`..`-filtering guard used in the last-resort branch to the two-folder branch as well, given these values are slated to become tag values or path inputs in future commits (Finding #6).
6. **(Low)** Consider Unicode normalization (`unicodedata.normalize('NFC', ...)`) on extracted artist/album strings before they are persisted or used for matching (Finding #7), and document the depth-anchoring assumption explicitly rather than relying on the `second_last` empty-string coincidence (Finding #8).
