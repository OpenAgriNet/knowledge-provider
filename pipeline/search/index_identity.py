"""Document-owned vector keys, separate from the source file fingerprint."""

from uuid import UUID


def projection_doc_id(document: dict, fingerprint: str | None = None) -> str:
    """A retry keeps its key; another document or source version gets another key."""
    owner = str(UUID(str(document["id"])))
    source = fingerprint or document.get("source_file_fingerprint") or document["document_id"]
    return f"document:{owner}:{source}"


def projection_owner(identifier: str) -> str | None:
    """Recover the local document identity from a new vector key."""
    parts = identifier.split(":", 2)
    if len(parts) != 3 or parts[0] != "document" or not parts[2]:
        return None
    try:
        return str(UUID(parts[1]))
    except ValueError:
        return None


def recorded_doc_id(document: dict, environment: str) -> str:
    """Read the actual indexed key, including pre-migration fingerprint keys."""
    entry = (document.get("index_status") or {}).get(environment) or {}
    return entry.get("doc_id") or document.get("document_id") or ""
