# TODOs

Known debt that is out of scope for the change that found it. One entry per item, newest first.

## Test isolation: search tests fail when run alone

Found 2026-10-14 while splitting `pipeline/api` (story #202). Pre-existing, not caused by that change.

Five tests in `tests/test_api.py` fail when selected on their own but pass in the full suite:

```
uv run pytest tests/test_api.py -k "search or valid_on or expired"
# 5 failed, 2 passed — sqlite3.OperationalError: no such table: settings
```

Root cause: `tests/conftest.py` sets `DOCUMENT_DB_PATH=:memory:`, and the `test_client` fixture calls `db.init_db()` against it. A SQLite in-memory database lives per connection, so the tables vanish before the route opens its own connection. The tests pass in the full suite only because an earlier `db_connection` fixture left `db.DB_PATH` pointing at a temp file. Likely fix: have `test_client` depend on `db_connection` (a file-backed temp DB), as `tests/test_search_group_integration.py` does. Worth its own ticket.
