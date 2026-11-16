"""Install the active schema and apply later numbered migrations once."""

import logging
import re
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATION_FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_ADVISORY_LOCK_KEY = 7_204_911_338_105_221
_BOOTSTRAP_VERSION = "0022"
_BOOTSTRAP_FILES = (
    "00_shared.sql",
    "10_instances_access.sql",
    "20_documents_processing.sql",
    "25_workflows.sql",
    "30_intake_source_evidence.sql",
    "40_search_audit_catalog.sql",
    "50_network_registry.sql",
)


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE_RE.match(path.name)
        if not match:
            raise ValueError(f"Migration file name must be NNNN_name.sql: {path.name}")
        version = match.group(1)
        if found and found[-1][0] == version:
            raise ValueError(f"Duplicate migration version: {version}")
        found.append((version, path))
    return found


def _has_bootstrap(directory: Path) -> bool:
    return directory == MIGRATIONS_DIR or (directory / "bootstrap").is_dir()


def _install_bootstrap(conn: psycopg.Connection, directory: Path, done: set[str]) -> None:
    has_documents = conn.execute("SELECT to_regclass('public.documents')").fetchone()[0] is not None
    if done or has_documents:
        raise RuntimeError("Pre-release schema detected. Run the archived Postgres upgrades from the migration kit first.")

    with conn.transaction():
        for name in _BOOTSTRAP_FILES:
            conn.execute((directory / "bootstrap" / name).read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
            (_BOOTSTRAP_VERSION, "active_schema_baseline"),
        )
    logger.info("installed the grouped application schema at version %s", _BOOTSTRAP_VERSION)


def apply_migrations(conninfo: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply the baseline and pending migrations under one startup lock."""
    migrations = list_migrations(directory)
    has_bootstrap = _has_bootstrap(directory)
    if has_bootstrap and migrations and migrations[0][0] <= _BOOTSTRAP_VERSION:
        raise ValueError("Runtime migrations must follow the active schema baseline")

    applied: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            if has_bootstrap and _BOOTSTRAP_VERSION not in done:
                _install_bootstrap(conn, directory, done)
                applied.append(_BOOTSTRAP_VERSION)
            for version, path in migrations:
                if version in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                        (version, path.name),
                    )
                logger.info("applied migration %s", path.name)
                applied.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    return applied


def current_version(conninfo: str) -> str | None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]
        if not exists:
            return None
        row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
        return row[0]


def is_current(conninfo: str, directory: Path = MIGRATIONS_DIR) -> bool:
    migrations = list_migrations(directory)
    if migrations:
        expected = migrations[-1][0]
    elif _has_bootstrap(directory):
        expected = _BOOTSTRAP_VERSION
    else:
        return True
    return current_version(conninfo) == expected
