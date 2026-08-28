#!/usr/bin/env python3
"""
Drop the legacy marqo_doc_id column from document_index_status.

Context: the Marqo backend was removed and document_index_status.marqo_doc_id
was superseded by a generically-named doc_id column, backfilled once at
startup (see pipeline/db.py init_db()). marqo_doc_id has been left in
place, unwritten, so the schema stayed usable by the pre-removal code during
rollout. This script is the followup: run it manually, once you've confirmed
the app has been stable on the new code for a while — it's a one-way door
(SQLite's DROP COLUMN can't be undone short of restoring a backup).

No other legacy Marqo-only tables/columns were found in this codebase as of
this writing (only this one column) — if a future migration needs the same
treatment, extend this script rather than writing a new one.

Requires SQLite 3.35+ for ALTER TABLE ... DROP COLUMN (bundled with Python
3.9+ on most platforms; the script checks and aborts with a clear message on
older versions rather than attempting a table-rebuild workaround).

Usage:
    uv run python scripts/drop_legacy_marqo_columns.py [--yes]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from pipeline import db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    args = parser.parse_args()

    if sqlite3.sqlite_version_info < (3, 35, 0):
        print(
            f"ERROR: SQLite {sqlite3.sqlite_version} does not support "
            "ALTER TABLE ... DROP COLUMN (needs 3.35+). Aborting — this "
            "script does not implement a table-rebuild fallback.",
            file=sys.stderr,
        )
        sys.exit(1)

    with db.get_connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(document_index_status)")}
        if "marqo_doc_id" not in columns:
            print("document_index_status.marqo_doc_id doesn't exist — nothing to drop.")
            sys.exit(0)

        stale = conn.execute(
            """
            SELECT COUNT(*) FROM document_index_status
            WHERE doc_id IS NULL AND marqo_doc_id IS NOT NULL
            """
        ).fetchone()[0]
        if stale:
            print(
                f"ERROR: {stale} row(s) in document_index_status have "
                "marqo_doc_id set but doc_id NULL — the backfill in "
                "init_db() hasn't run against this database yet. Start the "
                "app once (so init_db() runs) before dropping the column.",
                file=sys.stderr,
            )
            sys.exit(1)

        total = conn.execute("SELECT COUNT(*) FROM document_index_status").fetchone()[0]
        print(f"document_index_status: {total} row(s), all backfilled to doc_id.")

        if not args.yes:
            answer = input(
                "Drop the legacy marqo_doc_id column now? This cannot be undone "
                "short of restoring a backup. [y/N] "
            ).strip().lower()
            if answer != "y":
                print("Aborted.")
                sys.exit(0)

        conn.execute("ALTER TABLE document_index_status DROP COLUMN marqo_doc_id")
        conn.commit()
        print("Dropped document_index_status.marqo_doc_id.")


if __name__ == "__main__":
    main()
