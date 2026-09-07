"""
Builds the Network Catalog Envelope's catalog for a document's knowledge kind.

This provider announces *what kind of knowledge it serves*, not individual
documents: one catalog holding exactly one `OnDemand` resource per knowledge
kind, identical for every document of that kind. A seeker that matches the
resource comes back to us to ask the actual question, so there is nothing
per-document to put on the wire.

Pure by design - no env, no I/O, no HTTP. `pipeline/discovery_publish_service.py`
owns the environment and the transport; this module only decides shape. See
`docs/ADR/0003-static-ondemand-network-catalog-per-knowledge-kind.md`.
"""

import copy
from typing import Optional

# Resource ids are permanent. `updateMode: MERGE` upserts resources by id, so
# changing one strands the old resource on the network forever rather than
# replacing it.
ADVISORY_CATALOG_ID = "oan.knowledgeprovider.advisory"
ADVISORY_RESOURCE_ID = "oan.knowledgeprovider.advisory.resource"
SCHEMES_CATALOG_ID = "oan.knowledgeprovider.schemes"
SCHEMES_RESOURCE_ID = "oan.knowledgeprovider.schemes.resource"

SCHEMA_CONTEXT_BASE = "https://schemas.openagrinet.global/schema"

# The whole point of the static catalog: we hold no advisory content ready to
# hand out, we answer when asked. Under OnDemand the schema *forbids* issuedAt,
# validity, recommendations, supportingResourceIds, rationale and source on an
# advisory, and knowledgeType, version, lifecycleStatus, content, validity,
# provenance and supersedes on a knowledge resource - so none are emitted.
INFORMATION_MODE = "OnDemand"

# Languages the pipeline's corpus is translated into and served in.
SERVED_LANGUAGES = ["en", "hi", "mr"]

DEFAULT_DOCUMENT_KIND = "document"

# v2: topics and subjectCategories become per-document values derived by an AI
# agent. Until then one fixed pair per kind describes the whole corpus, which is
# why they are constants here rather than configuration.
_ADVISORY_RESOURCE_ATTRIBUTES = {
    "@context": f"{SCHEMA_CONTEXT_BASE}/KnowledgeAdvisory/v0.1/context.jsonld",
    "@type": "openagrinet:KnowledgeAdvisory",
    "informationMode": INFORMATION_MODE,
    "subjectCategories": ["Crop"],
    "topics": ["Crop production"],
    "languages": SERVED_LANGUAGES,
}

# There is no KnowledgeScheme schema in the network specs - `Scheme` exists only
# as a subjectCategories value - so a scheme document is announced as reference
# knowledge. `supportedKnowledgeTypes` is required for KnowledgeResource under
# OnDemand, which KnowledgeAdvisory has no equivalent of.
_SCHEME_RESOURCE_ATTRIBUTES = {
    "@context": f"{SCHEMA_CONTEXT_BASE}/KnowledgeResource/v0.1/context.jsonld",
    "@type": "openagrinet:KnowledgeResource",
    "informationMode": INFORMATION_MODE,
    "subjectCategories": ["Scheme"],
    "supportedKnowledgeTypes": ["Reference"],
    "topics": ["Government schemes"],
    "languages": SERVED_LANGUAGES,
}

_KNOWLEDGE_KIND_CATALOGS = {
    "advisory": {
        "catalog_id": ADVISORY_CATALOG_ID,
        "catalog_name": "Agricultural advisory from documents",
        "resource_id": ADVISORY_RESOURCE_ID,
        "resource_name": "Agricultural advisory",
        "resource_attributes": _ADVISORY_RESOURCE_ATTRIBUTES,
    },
    "scheme": {
        "catalog_id": SCHEMES_CATALOG_ID,
        "catalog_name": "Schemes from documents",
        "resource_id": SCHEMES_RESOURCE_ID,
        "resource_name": "Government schemes",
        "resource_attributes": _SCHEME_RESOURCE_ATTRIBUTES,
    },
}

PUBLISHABLE_DOCUMENT_KINDS = tuple(_KNOWLEDGE_KIND_CATALOGS)


def normalize_document_kind(document_kind: Optional[str]) -> str:
    """Coerce a stored `documents.document_kind` to its lookup form.

    The column is nullable with a `'document'` default, and the same
    `or "document"` fallback is applied wherever the kind is read.
    """
    return (document_kind or DEFAULT_DOCUMENT_KIND).strip().lower()


def build_catalog(
    document_kind: Optional[str],
    bpp_id: str,
    bpp_uri: str,
) -> Optional[dict]:
    """Return the catalog to announce for `document_kind`, or None.

    None means this kind has nothing to announce - `document`, `video` and any
    operator-entered custom slug. Callers must skip the publish entirely rather
    than send an empty `catalogs` array, which the spec rejects (`minItems: 1`).
    """
    spec = _KNOWLEDGE_KIND_CATALOGS.get(normalize_document_kind(document_kind))
    if spec is None:
        return None

    return {
        "id": spec["catalog_id"],
        "bppId": bpp_id,
        "bppUri": bpp_uri,
        "descriptor": {"name": spec["catalog_name"]},
        "isActive": True,
        "resources": [
            {
                "id": spec["resource_id"],
                "descriptor": {"name": spec["resource_name"]},
                "resourceAttributes": copy.deepcopy(spec["resource_attributes"]),
            }
        ],
    }
