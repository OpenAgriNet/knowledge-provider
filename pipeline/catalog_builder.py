"""
Builds the Network Catalog Envelope's catalog for a document's knowledge kind.

One catalog holding exactly one OnDemand resource per kind, identical for every
document of that kind - a seeker that matches the resource comes back to ask the
actual question, so there is nothing per-document to put on the wire.

Pure: no env, no I/O. Values live in network_constants.py and the transport in
discovery_publish_service.py. See
docs/ADR/0003-static-ondemand-network-catalog-per-knowledge-kind.md.
"""

import copy
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .network_constants import (
    ADVISORY_CATALOG_ID,
    ADVISORY_CATALOG_NAME,
    ADVISORY_PROVIDER_NAME,
    ADVISORY_RESOURCE_ID,
    ADVISORY_RESOURCE_NAME,
    ADVISORY_SUBJECT_CATEGORIES,
    ADVISORY_TOPICS,
    DEFAULT_DOCUMENT_KIND,
    GROUP_PROVIDER_NAME_TEMPLATE,
    INFORMATION_MODE,
    PROVIDER_ID_TEMPLATE,
    SCHEMA_CONTEXT_BASE,
    SCHEME_SUBJECT_CATEGORIES,
    SCHEME_TOPICS,
    SCHEMES_CATALOG_ID,
    SCHEMES_CATALOG_NAME,
    SCHEMES_PROVIDER_NAME,
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


@dataclass(frozen=True)
class Group:
    code: str
    name: str


@dataclass(frozen=True)
class ProviderNaming:
    shared_instance: str = "bv"
    id_template: str = PROVIDER_ID_TEMPLATE
    group_name_template: str = GROUP_PROVIDER_NAME_TEMPLATE
    provider_names: Mapping[str, str] = field(
        default_factory=lambda: {
            "advisory": ADVISORY_PROVIDER_NAME,
            "scheme": SCHEMES_PROVIDER_NAME,
        }
    )

    def provider(self, kind: str, group: Optional[Group]) -> dict:
        instance = group.code if group is not None else self.shared_instance
        provider_id = self.id_template.format(kind=kind, instance=instance)
        name = self.provider_names[kind]
        if group is not None:
            name = self.group_name_template.format(group_name=group.name, provider_name=name)
        return {"id": provider_id, "descriptor": {"code": provider_id, "name": name}}


def normalize_document_kind(document_kind: Optional[str]) -> str:
    """Coerce a stored `documents.document_kind` to its lookup form."""
    return (document_kind or DEFAULT_DOCUMENT_KIND).strip().lower()


def is_publishable_kind(document_kind: Optional[str]) -> bool:
    return normalize_document_kind(document_kind) in _KNOWLEDGE_KIND_CATALOGS


def build_catalog(
    document_kind: Optional[str],
    group: Optional[Group] = None,
    naming: ProviderNaming = ProviderNaming(),
) -> Optional[dict]:
    """Return the catalog to announce for `document_kind`, or None.

    None means this kind has nothing to announce - `document`, `video` and any
    operator-entered custom slug. Callers must skip the publish rather than send
    an empty `catalogs` array, which the spec rejects (`minItems: 1`).
    """
    kind = normalize_document_kind(document_kind)
    spec = _KNOWLEDGE_KIND_CATALOGS.get(kind)
    if spec is None:
        return None

    catalog_id = spec["catalog_id"]
    catalog_name = spec["catalog_name"]
    resource_id = spec["resource_id"]
    provider = naming.provider(kind, group)
    if group is not None:
        prefix = f"{group.code}-"
        catalog_id = prefix + catalog_id
        resource_id = prefix + resource_id
        catalog_name = provider["descriptor"]["name"]

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
