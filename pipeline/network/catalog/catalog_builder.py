"""Build a Beckn catalog from a deployment's resource projections."""

import copy
from typing import Optional

from pipeline.network.catalog.catalog_profile import CatalogProjection, load_catalog_profile
from pipeline.network.network_constants import DEFAULT_DOCUMENT_KIND
from pipeline.network.network_provider import Group


def normalize_document_kind(document_kind: Optional[str]) -> str:
    """Normalize the stored document kind for lookup."""
    return (document_kind or DEFAULT_DOCUMENT_KIND).strip().lower()


def is_publishable_kind(document_kind: Optional[str]) -> bool:
    return normalize_document_kind(document_kind) in load_catalog_profile()


def build_catalog(
    document_kind: Optional[str],
    provider: dict,
    catalog_name: str,
    group: Optional[Group] = None,
    *,
    spec: CatalogProjection | None = None,
) -> Optional[dict]:
    """Return the catalog to announce for `document_kind`, or None.

    None means the profile has no entry for this kind. The transport skips
    publication rather than sending an empty catalogs array.
    """
    spec = spec or load_catalog_profile().get(normalize_document_kind(document_kind))
    if spec is None:
        return None

    catalog_id = spec.catalog_id
    resource_id = spec.resource_id
    provider = copy.deepcopy(provider)
    if group is not None:
        prefix = f"{group.code}-"
        catalog_id = prefix + catalog_id
        resource_id = prefix + resource_id

    catalog = {
        "id": catalog_id,
        "descriptor": {"name": catalog_name},
        "isActive": True,
        "provider": provider,
        "resources": [
            {
                "id": resource_id,
                "descriptor": {"name": spec.resource_name},
                "resourceAttributes": copy.deepcopy(spec.resource_attributes),
            }
        ],
    }
    return catalog
