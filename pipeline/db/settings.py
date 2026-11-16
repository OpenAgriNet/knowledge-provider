"""Persist retrieval settings and their audit trail."""

from typing import Any

from psycopg.types.json import Jsonb

from pipeline.documents.models import (
    CandidateSettings,
    QuerySettings,
    ResultSettings,
    RetrievalSettings,
)

from . import audit, connection

# =============================================================================
# Settings Functions
# =============================================================================

_SETTING_GROUPS: dict[str, tuple[str, type]] = {
    "searchMethod": ("retrieval", RetrievalSettings),
    "alpha": ("retrieval", RetrievalSettings),
    "rankingMethod": ("retrieval", RetrievalSettings),
    "hybridRrfK": ("retrieval", RetrievalSettings),
    "indexName": ("candidates", CandidateSettings),
    "efSearch": ("candidates", CandidateSettings),
    "candidateCap": ("candidates", CandidateSettings),
    "candidateMultiplier": ("candidates", CandidateSettings),
    "limit": ("results", ResultSettings),
    "maxChunksPerDoc": ("results", ResultSettings),
    "excludeReference": ("results", ResultSettings),
    "showHighlights": ("results", ResultSettings),
    "rerankMode": ("results", ResultSettings),
    "useE5Prefix": ("query", QuerySettings),
    "queryExpansionProfile": ("query", QuerySettings),
}


_GROUP_MODELS: dict[str, type] = {group: model for group, model in _SETTING_GROUPS.values()}


_SETTINGS_ROW_SQL = (
    "SELECT retrieval, candidates, results, query FROM search_settings WHERE singleton"
)


def _settings_row(conn, lock: bool = False) -> dict:
    sql = _SETTINGS_ROW_SQL + (" FOR UPDATE" if lock else "")
    row = conn.execute(sql).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO search_settings DEFAULT VALUES ON CONFLICT (singleton) DO NOTHING"
        )
        row = conn.execute(sql).fetchone()
    return row


def _flatten_settings(row: dict) -> dict:
    flat: dict[str, Any] = {}
    for group, model in _GROUP_MODELS.items():
        flat.update(model.model_validate(row[group] or {}).model_dump())
    return flat


def get_search_settings() -> dict:
    """Get all search settings as a flat dict (stored values merged over defaults)."""
    with connection.get_connection() as conn:
        return _flatten_settings(_settings_row(conn))


def _setting_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def update_search_settings(settings: dict) -> dict:
    """
    Update multiple search settings at once.

    Args:
        settings: Dict with keys like searchMethod, limit, alpha, etc.

    Returns:
        Updated search settings

    Raises:
        ValueError: a value fails the setting's validation rules.
    """
    unknown = sorted(set(settings) - set(_SETTING_GROUPS))
    if unknown:
        raise ValueError(f"Unknown search settings: {', '.join(unknown)}")
    changes: list[tuple[str, str, str]] = []
    with connection.get_connection() as conn:
        row = _settings_row(conn, lock=True)
        before = _flatten_settings(row)
        for group, model in _GROUP_MODELS.items():
            patch = {k: v for k, v in settings.items() if _SETTING_GROUPS[k][0] == group}
            if not patch:
                continue
            try:
                validated = model.model_validate(
                    {**model.model_validate(row[group] or {}).model_dump(), **patch}
                )
            except Exception as exc:
                raise ValueError(str(exc)) from exc
            stored = {**(row[group] or {}), **{k: getattr(validated, k) for k in patch}}
            conn.execute(
                f"UPDATE search_settings SET {group} = {group} || %s::jsonb WHERE singleton",
                (Jsonb({k: getattr(validated, k) for k in patch}),),
            )
            for key in patch:
                if before[key] != stored[key]:
                    changes.append(
                        (f"{group}.{key}", _setting_text(before[key]), _setting_text(stored[key]))
                    )
        for field_name, old_value, new_value in changes:
            conn.execute(
                """
                INSERT INTO audit_logs (action_type, entity_type, field_name, old_value, new_value)
                VALUES ('settings_change', 'setting', %s, %s, %s)
                """,
                (field_name, old_value, new_value),
            )
        return _flatten_settings(conn.execute(_SETTINGS_ROW_SQL).fetchone())


def get_settings_audit_logs(limit: int = 50, offset: int = 0) -> list[dict]:
    """Get audit logs for settings changes."""
    with connection.get_connection() as conn:
        rows = conn.execute(
            f"{audit._AUDIT_SELECT} WHERE a.entity_type = 'setting' "
            "ORDER BY a.created_at DESC, a.id LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
    return [audit._audit_row_to_legacy(r) for r in rows]


def get_settings_audit_count() -> int:
    """Get count of settings audit logs."""
    with connection.get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM audit_logs WHERE entity_type = 'setting'"
        ).fetchone()
    return int(row["cnt"]) if row else 0
