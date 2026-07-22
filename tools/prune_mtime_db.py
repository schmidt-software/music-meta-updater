#!/usr/bin/env python3
"""Utility to prune stale rows from the mtime tracking DB.

Usage: python3 tools/prune_mtime_db.py /path/to/mtime.db

This calls the same helpers used by the scanner to remove rows for files
that no longer exist on disk and commits the change.
"""
import sys
import os

# Import helpers from the package module
try:
    from scan_incomplete import init_mtime_db, prune_stale_mtime_rows, commit_mtime_db
except Exception:
    # If module import fails when called from outside the repo root, attempt
    # to add repo root to path and retry.
    repo_root = os.path.dirname(os.path.dirname(__file__))
    sys.path.insert(0, repo_root)
    from scan_incomplete import init_mtime_db, prune_stale_mtime_rows, commit_mtime_db


def main():
    if len(sys.argv) < 2:
        print("Usage: prune_mtime_db.py /path/to/mtime.db")
        sys.exit(2)
    db_path = sys.argv[1]
    if not os.path.exists(db_path):
        print(f"Error: DB not found: {db_path}")
        sys.exit(1)
    conn = init_mtime_db(db_path)
    try:
        prune_stale_mtime_rows(conn)
        commit_mtime_db(conn)
        print("Pruned stale rows and committed.")
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
