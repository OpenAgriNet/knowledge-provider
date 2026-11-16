"""Validate a schema pack against its approved, offline dependency bundle."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from pydantic import BaseModel, ConfigDict, Field
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

_MAX_FILE_SIZE = 512 * 1024
_MAX_BUNDLE_SIZE = 2 * 1024 * 1024


def _encoded(value: object) -> bytes:
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    encoded = bytearray()
    for chunk in encoder.iterencode(value):
        encoded.extend(chunk.encode())
        if len(encoded) > _MAX_BUNDLE_SIZE:
            raise ValueError("The schema bundle exceeds its size limit")
    return bytes(encoded)


class SchemaSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    capability: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*:[A-Za-z][A-Za-z0-9_-]*$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attributes: dict

    def validate_registered_pack(self, conn) -> None:
        if len(_encoded(self.attributes)) > 65_536:
            raise ValueError("Schema attributes exceed the size limit")
        get_schema_pack(conn, self.network_id, self.capability, self.digest).validate_attributes(self.attributes)


def validate_schema_selection(conn, metadata: dict | None) -> None:
    selection = (metadata or {}).get("schema_pack")
    if selection is not None:
        SchemaSelection.model_validate(selection).validate_registered_pack(conn)


class SchemaPackManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*:[A-Za-z][A-Za-z0-9_-]*$")
    version: str = Field(pattern=r"^v[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
    entrypoint: str
    component: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    files: dict[str, str] = Field(min_length=1, max_length=32)


def _load_file(root: Path, relative_path: str) -> dict:
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root) or path.suffix not in {".yaml", ".yml", ".json"}:
        raise ValueError("Each schema file must be a YAML or JSON artifact inside the bundle")
    with path.open("rb") as handle:
        contents = handle.read(_MAX_FILE_SIZE + 1)
    if len(contents) > _MAX_FILE_SIZE:
        raise ValueError("A schema artifact exceeds its size limit")
    value = yaml.safe_load(contents)
    if not isinstance(value, dict):
        raise ValueError("A schema artifact must contain an object")
    return value


def _references(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"}:
                yield item
            elif isinstance(item, (dict, list)):
                yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


class SchemaPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    version: str
    entrypoint: str
    component: str
    schemas: dict[str, dict]

    @property
    def digest(self) -> str:
        return hashlib.sha256(_encoded(self.model_dump())).hexdigest()

    def _registry(self) -> Registry:
        return Registry().with_resources(
            (uri, DRAFT202012.create_resource(schema)) for uri, schema in self.schemas.items()
        )

    def check(self) -> None:
        _encoded(self.model_dump())
        if self.entrypoint not in self.schemas or not re.fullmatch(r"[A-Za-z0-9_-]+", self.component):
            raise ValueError("The schema entrypoint or component is invalid")
        try:
            component = self.schemas[self.entrypoint]["components"]["schemas"][self.component]
        except (KeyError, TypeError):
            raise ValueError("The selected schema component is absent") from None
        Draft202012Validator.check_schema(component)

        registry = self._registry()
        for uri, artifact in self.schemas.items():
            target = urlsplit(uri)
            if target.scheme != "https" or not target.hostname or target.username or target.query or target.fragment:
                raise ValueError("Schema artifact identifiers must be absolute HTTPS URLs")
            for reference in _references(artifact):
                if not isinstance(reference, str) or urldefrag(urljoin(uri, reference)).url not in self.schemas:
                    raise ValueError("A schema references an artifact outside its approved bundle")
                try:
                    registry.resolver(uri).lookup(reference)
                except Unresolvable:
                    raise ValueError("A schema references an unknown bundled component") from None

    def validate_attributes(self, attributes: dict) -> None:
        reference = f"{self.entrypoint}#/components/schemas/{self.component}"
        validator = Draft202012Validator(
            {"type": "object", "$ref": reference},
            registry=self._registry(),
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        try:
            validator.validate(attributes)
        except ValidationError as exc:
            field = ".".join(map(str, exc.absolute_path)) or "attributes"
            raise ValueError(f"Schema pack {self.capability} rejected {field} ({exc.validator})") from None


def load_schema_pack(manifest_path: Path) -> SchemaPack:
    root = manifest_path.resolve().parent
    manifest = SchemaPackManifest.model_validate(_load_file(root, manifest_path.name))
    pack = SchemaPack(
        **manifest.model_dump(exclude={"files"}),
        schemas={uri: _load_file(root, path) for uri, path in manifest.files.items()},
    )
    pack.check()
    return pack


def install_schema_pack(conn, network_id: str, pack: SchemaPack) -> str:
    """Store a checked bundle without replacing a prior digest."""
    from psycopg.types.json import Jsonb

    pack.check()
    digest = pack.digest
    conn.execute(
        """
        INSERT INTO schema_pack_versions (network_id, capability, digest, version, bundle)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (network_id, capability, digest) DO NOTHING
        """,
        (network_id, pack.capability, digest, pack.version, Jsonb(pack.model_dump())),
    )
    return digest


def get_schema_pack(conn, network_id: str, capability: str, digest: str) -> SchemaPack:
    row = conn.execute(
        "SELECT bundle FROM schema_pack_versions WHERE network_id = %s AND capability = %s AND digest = %s",
        (network_id, capability, digest),
    ).fetchone()
    if not row:
        raise ValueError("The selected schema pack is not installed")
    pack = SchemaPack.model_validate(row["bundle"])
    if pack.digest != digest:
        raise ValueError("The stored schema pack does not match its pinned digest")
    return pack
