"""Protocol status codes and the legacy PostgreSQL catalog type mapping."""

BECKN_VERSION = "2.0.0"

# Per-catalog result status in an on_publish body; a 200 is only an ACK.
RESULT_ACCEPTED = "ACCEPTED"
RESULT_REJECTED = "REJECTED"

DEFAULT_DOCUMENT_KIND = "document"

PROVIDER_ID_TEMPLATE = "{instance}.{network_id}"
GROUP_CATALOG_NAME_TEMPLATE = "{group_name} {catalog_name}"

# Knowledge kinds the pipeline serves, and the network schema each maps to.
# ``catalogs.type`` stores the schema name; code asks this map what a type means
# instead of comparing ``openagrinet:*`` strings. The scheme mapping is a
# placeholder (ADR 0003 types schemes as KnowledgeAdvisory) - change it here only.
KIND_SCHEME = "scheme"
KIND_ADVISORY = "advisory"
KIND_VIDEO = "video"
KIND_TO_CATALOG_TYPE = {
    KIND_SCHEME: "openagrinet:AgricultureResource",
    KIND_ADVISORY: "openagrinet:KnowledgeAdvisory",
    KIND_VIDEO: "openagrinet:VideoResource",
}
CATALOG_TYPE_TO_KIND = {schema: kind for kind, schema in KIND_TO_CATALOG_TYPE.items()}


def catalog_type_for_kind(kind: str) -> str:
    """``catalogs.type`` for a knowledge kind; ValueError for an unknown kind."""
    try:
        return KIND_TO_CATALOG_TYPE[(kind or "").strip().lower()]
    except KeyError:
        raise ValueError(
            f"Unknown document kind {kind!r}. Use one of: {', '.join(KIND_TO_CATALOG_TYPE)}"
        ) from None


def kind_for_catalog_type(catalog_type: str) -> str:
    """Knowledge kind a ``catalogs.type`` stands for; ValueError for an unknown type."""
    try:
        return CATALOG_TYPE_TO_KIND[catalog_type]
    except KeyError:
        raise ValueError(f"Unknown catalog type {catalog_type!r}") from None
