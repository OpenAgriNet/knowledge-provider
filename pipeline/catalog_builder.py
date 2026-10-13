"""
Builds the Network Catalog Envelope's catalog for a document's knowledge kind.

One catalog holding exactly one OnDemand resource per kind, identical for every
document of that kind - a seeker that matches the resource comes back to ask the
actual question, so there is nothing per-document to put on the wire. A Group's
documents get that same catalog under the Group's own provider, with ids
prefixed by the group code; documents with no Group keep the Shared Provider.

Pure: no env, no I/O. Values live in network_constants.py and the transport in
discovery_publish_service.py. See
docs/ADR/0003-static-ondemand-network-catalog-per-knowledge-kind.md and
docs/ADR/0006-publish-to-network-under-per-group-provider.md.
"""

import copy
from dataclasses import dataclass
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
        "group_label": "Knowledge Advisories",
        "catalog_id": ADVISORY_CATALOG_ID,
        "catalog_name": ADVISORY_CATALOG_NAME,
        "resource_id": ADVISORY_RESOURCE_ID,
        "resource_name": ADVISORY_RESOURCE_NAME,
        "resource_attributes": _ADVISORY_RESOURCE_ATTRIBUTES,
    },
    "scheme": {
        "group_label": "Knowledge Schemes",
        "catalog_id": SCHEMES_CATALOG_ID,
        "catalog_name": SCHEMES_CATALOG_NAME,
        "resource_id": SCHEMES_RESOURCE_ID,
        "resource_name": SCHEMES_RESOURCE_NAME,
        "resource_attributes": _SCHEME_RESOURCE_ATTRIBUTES,
    },
}

PUBLISHABLE_DOCUMENT_KINDS = tuple(_KNOWLEDGE_KIND_CATALOGS)


@dataclass(frozen=True)
class GroupProvider:
    """The Group a catalog is announced for, and the provider id it publishes as."""

    code: str
    name: str
    provider_id: str


def normalize_document_kind(document_kind: Optional[str]) -> str:
    """Coerce a stored `documents.document_kind` to its lookup form."""
    return (document_kind or DEFAULT_DOCUMENT_KIND).strip().lower()


def build_catalog(
    document_kind: Optional[str],
    group_provider: Optional[GroupProvider] = None,
) -> Optional[dict]:
    """Return the catalog to announce for `document_kind`, or None.

    With a `group_provider`, the catalog and resource ids carry the group code
    and both catalog and provider are named for the Group; without one, the
    catalog is announced under the Shared Provider exactly as before.

    None means this kind has nothing to announce - `document`, `video` and any
    operator-entered custom slug. Callers must skip the publish rather than send
    an empty `catalogs` array, which the spec rejects (`minItems: 1`).
    """
    spec = _KNOWLEDGE_KIND_CATALOGS.get(normalize_document_kind(document_kind))
    if spec is None:
        return None

    catalog_id = spec["catalog_id"]
    catalog_name = spec["catalog_name"]
    resource_id = spec["resource_id"]
    provider = copy.deepcopy(CATALOG_PROVIDER)
    if group_provider is not None:
        prefix = f"{group_provider.code}-"
        catalog_id = prefix + catalog_id
        resource_id = prefix + resource_id
        catalog_name = f"{group_provider.name} - {spec['group_label']}"
        provider = {
            "id": group_provider.provider_id,
            "descriptor": {
                "code": prefix + CATALOG_PROVIDER["descriptor"]["code"],
                "name": catalog_name,
            },
        }

    catalog = {
        "id": catalog_id,
        "descriptor": {"name": catalog_name},
        "isActive": True,
        "provider": provider,
        "resources": [
            {
                "id": resource_id,
                "descriptor": {"name": spec["resource_name"]},
                "resourceAttributes": copy.deepcopy(spec["resource_attributes"]),
            }
        ],
    }
    return catalog
