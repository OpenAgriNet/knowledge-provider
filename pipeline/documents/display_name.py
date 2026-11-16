MAX_LENGTH = 150

LOCKED_STAGES = frozenset(
    {"ingesting", "approval_for_prod", "ingesting_prod", "publishing_to_network", "completed"}
)


class DisplayNameLocked(Exception):
    pass


def normalize(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise ValueError("Display name cannot be blank.")
    if len(text) > MAX_LENGTH:
        raise ValueError(f"Display name must be at most {MAX_LENGTH} characters.")
    return text


def ensure_editable(doc: dict) -> None:
    if (doc.get("stage") or "") in LOCKED_STAGES or doc.get("ingested_at"):
        raise DisplayNameLocked(
            "Display name cannot change once the document has been published to Dev."
        )
