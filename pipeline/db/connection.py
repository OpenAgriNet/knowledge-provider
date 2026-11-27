"""Create the shared Psycopg pool and check database readiness."""

import logging
import os
import threading
from contextlib import contextmanager
from typing import Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from pipeline import migrate

logger = logging.getLogger(__name__)


DEFAULT_DATABASE_URL = "postgresql://knowledge_provider@localhost:5432/knowledge_provider"


_pool: Optional[ConnectionPool] = None


_pool_url: Optional[str] = None


_pool_lock = threading.Lock()


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def get_pool() -> ConnectionPool:
    global _pool, _pool_url
    url = database_url()
    if _pool is not None and _pool_url == url:
        return _pool
    with _pool_lock:
        if _pool is not None and _pool_url != url:
            _pool.close()
            _pool = None
        if _pool is None:
            pool = ConnectionPool(
                conninfo=url,
                min_size=int(os.environ.get("DB_POOL_MIN_SIZE", "1")),
                max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "10")),
                timeout=float(os.environ.get("DB_POOL_TIMEOUT_SECONDS", "30")),
                kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
                open=False,
            )
            pool.open(wait=True)
            _pool = pool
            _pool_url = url
            logger.info("db pool opened: %s", pool.get_stats())
    return _pool


def close_pool() -> None:
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_url = None


@contextmanager
def get_connection():
    """A pooled connection; the transaction commits on exit and rolls back on error."""
    with get_pool().connection() as conn:
        yield conn


def init_db() -> None:
    """Apply pending migrations and open the connection pool."""
    from . import catalog

    applied = migrate.apply_migrations(database_url())
    current = migrate.current_version(database_url())
    logger.info("db migrations current=%s applied_now=%s", current, applied)
    get_pool()
    created = catalog.sync_catalogs_for_all_instances()
    if created:
        logger.info("catalog sync created %d missing catalogs", created)


def check_health() -> dict:
    """Reachability and migration state, for the /health endpoint."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
        return {"reachable": True, "migrations_current": migrate.is_current(database_url())}
    except Exception:
        logger.exception("database health check failed")
        return {"reachable": False, "migrations_current": False}
