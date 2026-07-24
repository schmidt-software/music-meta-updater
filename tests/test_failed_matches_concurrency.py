import tempfile
import os
import concurrent.futures

from scan_incomplete import init_failed_matches_db, record_failed_match, get_failed_match


def test_record_failed_match_concurrent():
    """Simulate concurrent failed-match recordings to ensure atomic upsert increments."""
    tmp = tempfile.NamedTemporaryFile(delete=False)
    db_path = tmp.name
    tmp.close()

    try:
        conn = init_failed_matches_db(db_path)
        filepath = "/music/TestArtist/TestAlbum/01 - Track.mp3"
        calls = 50

        def worker():
            # Use the shared connection; record_failed_match should be safe
            record_failed_match(conn, filepath, "no_confident_match")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(worker) for _ in range(calls)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        # Commit to ensure changes are durable
        conn.commit()

        row = get_failed_match(conn, filepath)
        assert row is not None
        error_reason, match_attempts, last_time = row
        assert error_reason == "no_confident_match"
        assert match_attempts == calls
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            os.unlink(db_path)
        except Exception:
            pass
