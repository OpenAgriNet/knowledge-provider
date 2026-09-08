"""Fixed values sent on the network. v2 replaces the per-kind ones with AI-derived values."""

BECKN_VERSION = "2.0.0"

SCHEMA_CONTEXT_BASE = "https://schemas.openagrinet.global/schema"

# Permanent: updateMode MERGE upserts resources by id, so a changed id strands
# the old resource on the network instead of replacing it.
ADVISORY_CATALOG_ID = "cat-oan-knowledge-provider-advisories"
ADVISORY_RESOURCE_ID = "res-oan-knowledge-provider-advisories"
SCHEMES_CATALOG_ID = "cat-oan-knowledge-provider-schemes"
SCHEMES_RESOURCE_ID = "res-oan-knowledge-provider-schemes"

ADVISORY_CATALOG_NAME = "Agricultural advisory from documents"
ADVISORY_RESOURCE_NAME = "Agricultural advisory"
SCHEMES_CATALOG_NAME = "Schemes from documents"
SCHEMES_RESOURCE_NAME = "Government schemes"

# OnDemand means "invoke me for the specifics", and the schema forbids the
# fields that would carry them.
INFORMATION_MODE = "OnDemand"

SERVED_LANGUAGES = ["en"]

ADVISORY_SUBJECT_CATEGORIES = ["Crop"]
ADVISORY_TOPICS = ["Crop production"]
SCHEME_SUBJECT_CATEGORIES = ["Scheme"]
SCHEME_TOPICS = ["Government schemes"]

DEFAULT_DOCUMENT_KIND = "document"
