"""Load the resource descriptions used by the Beckn publisher."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

_DEFAULT_PROFILE = Path(__file__).with_name("profiles") / "beckn-catalogs.json"


class CatalogSchemaPin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*:[A-Za-z][A-Za-z0-9_-]*$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CatalogProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_name: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    catalog_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    resource_name: str = Field(min_length=1)
    resource_attributes: dict
    schema_pack: CatalogSchemaPin | None = None


@lru_cache(maxsize=4)
def _read_catalog_profile(path: Path) -> dict[str, CatalogProjection]:
    return TypeAdapter(dict[str, CatalogProjection]).validate_json(path.read_bytes())


def load_catalog_profile() -> dict[str, CatalogProjection]:
    override = os.environ.get("BECKN_CATALOG_PROFILE_PATH")
    return _read_catalog_profile(Path(override) if override else _DEFAULT_PROFILE)


def catalog_names() -> dict[str, str]:
    return {kind: projection.catalog_name for kind, projection in load_catalog_profile().items()}
