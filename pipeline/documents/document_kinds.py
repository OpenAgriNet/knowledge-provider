"""Deployment configuration for document classification."""

import os
import re

from pipeline.network.network_constants import document_kind_schemas

_KIND = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def configured_document_kinds() -> tuple[str, ...]:
    raw = os.environ.get("DOCUMENT_KINDS", "scheme,advisory,video")
    kinds = tuple(item.strip().lower() for item in raw.split(","))
    if len(set(kinds)) != len(kinds) or any(len(kind) > 64 or not _KIND.fullmatch(kind) for kind in kinds):
        raise ValueError("DOCUMENT_KINDS must list distinct, lowercase document kinds")
    if set(kinds) - document_kind_schemas().keys():
        raise ValueError("Every DOCUMENT_KINDS entry requires a DOCUMENT_KIND_SCHEMAS mapping")
    return kinds


def validate_document_kind(value: str) -> str:
    kind = value.strip().lower()
    allowed = configured_document_kinds()
    if kind not in allowed:
        raise ValueError(f"Unknown document kind {value!r}. Use one of: {', '.join(allowed)}")
    return kind
