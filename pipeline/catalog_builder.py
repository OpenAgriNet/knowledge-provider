"""
Builds the Network Catalog Envelope's catalog for a document's knowledge kind.

One catalog holding exactly one OnDemand resource per kind, identical for every
document of that kind - a seeker that matches the resource comes back to ask the
actual question, so there is nothing per-document to put on the wire.

Pure: no env, no I/O. Values live in network_constants.py, the validity window
in network_validity.py, and the transport in discovery_publish_service.py. See
docs/ADR/0003-static-ondemand-network-catalog-per-knowledge-kind.md and
docs/ADR/0005-approver-set-validity-window-on-the-network-catalog.md.
"""

import copy
from typing import Optional

from .network_constants import (
    ADVISORY_CATALOG_ID,
    ADVISORY_CATALOG_NAME,
    ADVISORY_RESOURCE_ID,
    ADVISORY_RESOURCE_NAME,
    ADVISORY_SUBJECT_CATEGORIES,
    ADVISORY_TOPICS,
    CATALOG_PROVIDER,
    DEFAULT_DOCUMENT_KIND,
    INFORMATION_MODE,
    SCHEMA_CONTEXT_BASE,
    SCHEME_SUBJECT_CATEGORIES,
    SCHEME_TOPICS,
    SCHEMES_CATALOG_ID,
    SCHEMES_CATALOG_NAME,
    SCHEMES_RESOURCE_ID,
    SCHEMES_RESOURCE_NAME,
    SERVED_LANGUAGES,
)
from .network_validity import ValidityWindow, to_catalog_validity

_ADVISORY_RESOURCE_ATTRIBUTES = {
    "@context": f"{SCHEMA_CONTEXT_BASE}/KnowledgeAdvisory/v0.1/context.jsonld",
    "@type": "openagrinet:KnowledgeAdvisory",
    "informationMode": INFORMATION_MODE,
    "subjectCategories": ADVISORY_SUBJECT_CATEGORIES,
    "topics": ADVISORY_TOPICS,
    "languages": SERVED_LANGUAGES,
}

# No KnowledgeScheme schema exists in the network specs - Scheme is only a
# subjectCategories value - so a scheme document is announced on the same
# KnowledgeAdvisory schema as advisories, distinguished only by
# subjectCategories: ["Scheme"].
_SCHEME_RESOURCE_ATTRIBUTES = {
    "@context": f"{SCHEMA_CONTEXT_BASE}/KnowledgeAdvisory/v0.1/context.jsonld",
    "@type": "openagrinet:KnowledgeAdvisory",
    "informationMode": INFORMATION_MODE,
    "subjectCategories": SCHEME_SUBJECT_CATEGORIES,
    "topics": SCHEME_TOPICS,
    "languages": SERVED_LANGUAGES,
}

_KNOWLEDGE_KIND_CATALOGS = {
    "advisory": {
        "catalog_id": ADVISORY_CATALOG_ID,
        "catalog_name": ADVISORY_CATALOG_NAME,
        "resource_id": ADVISORY_RESOURCE_ID,
        "resource_name": ADVISORY_RESOURCE_NAME,
        "resource_attributes": _ADVISORY_RESOURCE_ATTRIBUTES,
    },
    "scheme": {
        "catalog_id": SCHEMES_CATALOG_ID,
        "catalog_name": SCHEMES_CATALOG_NAME,
        "resource_id": SCHEMES_RESOURCE_ID,
        "resource_name": SCHEMES_RESOURCE_NAME,
        "resource_attributes": _SCHEME_RESOURCE_ATTRIBUTES,
    },
}

PUBLISHABLE_DOCUMENT_KINDS = tuple(_KNOWLEDGE_KIND_CATALOGS)


def normalize_document_kind(document_kind: Optional[str]) -> str:
    """Coerce a stored `documents.document_kind` to its lookup form."""
    return (document_kind or DEFAULT_DOCUMENT_KIND).strip().lower()


def build_catalog(
    document_kind: Optional[str],
    validity: Optional[ValidityWindow] = None,
) -> Optional[dict]:
    """Return the catalog to announce for `document_kind`, or None.

    None means this kind has nothing to announce - `document`, `video` and any
    operator-entered custom slug. Callers must skip the publish rather than send
    an empty `catalogs` array, which the spec rejects (`minItems: 1`).

    `validity` is the lifetime an approver set for the document being published.
    It lands on the **catalog**, not on `resourceAttributes`: the OnDemand
    KnowledgeAdvisory profile omits a resource-level `validity` (ADR 0003), and
    catalog-level `validity` is where the network specs' own provider-catalog
    examples put a published announcement's lifetime. Omitted entirely when
    None, which leaves a pre-existing announcement's lifetime untouched under
    `updateMode: MERGE`.
    """
    spec = _KNOWLEDGE_KIND_CATALOGS.get(normalize_document_kind(document_kind))
    if spec is None:
        return None

    catalog = {
        "id": spec["catalog_id"],
        "descriptor": {"name": spec["catalog_name"]},
        "isActive": True,
        "provider": copy.deepcopy(CATALOG_PROVIDER),
        "resources": [
            {
                "id": spec["resource_id"],
                "descriptor": {"name": spec["resource_name"]},
                "resourceAttributes": copy.deepcopy(spec["resource_attributes"]),
            }
        ],
    }
    if validity is not None:
        catalog["validity"] = to_catalog_validity(validity)
    return catalog
