"""Protocol status codes and configured document catalog schemas."""

import json
import os
import re

BECKN_VERSION = "2.0.0"

# Per-catalog result status in an on_publish body; a 200 is only an ACK.
RESULT_ACCEPTED = "ACCEPTED"
RESULT_REJECTED = "REJECTED"

DEFAULT_DOCUMENT_KIND = "document"

PROVIDER_ID_TEMPLATE = "{instance}.{network_id}"
GROUP_CATALOG_NAME_TEMPLATE = "{group_name} {catalog_name}"

# Knowledge kinds the pipeline serves, and the network schema each maps to.
# A document kind is a catalog partition. Several kinds may share a schema.
KIND_SCHEME = "scheme"
KIND_ADVISORY = "advisory"
KIND_VIDEO = "video"
KIND_TO_CATALOG_TYPE = {
    KIND_SCHEME: "openagrinet:KnowledgeAdvisory",
    KIND_ADVISORY: "openagrinet:KnowledgeAdvisory",
    KIND_VIDEO: "openagrinet:VideoResource",
}


def document_kind_schemas() -> dict[str, str]:
    mapping = json.loads(os.environ.get("DOCUMENT_KIND_SCHEMAS", "{}"))
    if not isinstance(mapping, dict) or any(
        not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", kind)
        or not isinstance(schema, str) or not re.fullmatch(r"[a-z0-9]+:[A-Za-z0-9]+", schema)
        for kind, schema in mapping.items()
    ):
        raise ValueError("DOCUMENT_KIND_SCHEMAS must map lowercase kinds to qualified schema names")
    return {**KIND_TO_CATALOG_TYPE, **mapping}


def catalog_type_for_kind(kind: str) -> str:
    """``catalogs.type`` for a knowledge kind; ValueError for an unknown kind."""
    try:
        return document_kind_schemas()[(kind or "").strip().lower()]
    except KeyError:
        raise ValueError(
            f"Unknown document kind {kind!r}. Use one of: {', '.join(KIND_TO_CATALOG_TYPE)}"
        ) from None
