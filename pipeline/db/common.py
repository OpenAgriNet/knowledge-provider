"""Normalize identifiers and values shared by PostgreSQL queries."""

import hashlib
import os
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

# =============================================================================
# Helpers
# =============================================================================

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{32}$")


_REVIEW_STAGES = {
    "ocr_review",
    "translation_review",
    "chunk_review",
    "ready_for_ingestion",
    "approval_for_prod",
}


def _default_instance() -> str:
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def _normalize_instances(instances: list[str]) -> list[str]:
    default = _default_instance()
    return sorted({(i or "").strip().lower() or default for i in instances})


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _is_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _clean(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _clean_row(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    return {k: _clean(v) for k, v in row.items()}


def _csv_roles(roles: Any) -> list[str]:
    if roles is None:
        return []
    if isinstance(roles, (list, tuple, set)):
        return [str(r).strip() for r in roles if str(r).strip()]
    return [part.strip() for part in str(roles).split(",") if part.strip()]


def _fingerprint_for(
    source_file_fingerprint: Optional[str],
    document_id: Optional[str],
    workflow_id: Optional[str],
) -> str:
    for candidate in (source_file_fingerprint, document_id):
        if candidate and _FINGERPRINT_RE.match(candidate):
            return candidate
    return hashlib.md5((document_id or workflow_id or "").encode()).hexdigest()
