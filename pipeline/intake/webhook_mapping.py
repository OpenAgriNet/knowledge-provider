"""Bounded JSON Pointer mapping for provider-owned webhook sources."""

import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from pipeline.intake.source_intake import NormalizedSourceDocument, canonical_json_sha256


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def decode_source_json(data: bytes | bytearray) -> object:
    return json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant, object_pairs_hook=_unique_object)


def parse_canonical_document(data: bytes) -> NormalizedSourceDocument:
    return NormalizedSourceDocument.model_validate(decode_source_json(data))


MAX_RECORDS = 250
JsonPointer = Annotated[str, StringConstraints(max_length=512, pattern=r"^(?:/(?:[^~/]|~[01])*)*$")]


class Selector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["record", "root"] = "record"
    path: JsonPointer


class EqualsCondition(Selector):
    equals: str | bool | int | None


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: JsonPointer | Selector
    title: JsonPointer | Selector
    text: JsonPointer | Selector
    kind: JsonPointer | Selector | None = None
    source_url: JsonPointer | Selector | None = None
    language: JsonPointer | Selector | None = None
    translations: JsonPointer | Selector | None = None
    crop_subjects: JsonPointer | Selector | None = None
    knowledge_metadata: JsonPointer | Selector | None = None
    validity: JsonPointer | Selector | None = None
    valid_from: JsonPointer | Selector | None = None
    valid_to: JsonPointer | Selector | None = None


class WebhookMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: Literal["json-pointer-v1"] = "json-pointer-v1"
    records: JsonPointer = ""
    each_root: bool = False
    fields: FieldMapping
    filters: list[EqualsCondition] = Field(default_factory=list, max_length=12)


class CanonicalMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: Literal["canonical-v1"] = "canonical-v1"


class JsonataMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: Literal["jsonata-v1"]
    expression: str = Field(min_length=1, max_length=8192)


SourceMapping = CanonicalMapping | WebhookMapping | JsonataMapping
SOURCE_MAPPING = TypeAdapter(SourceMapping)
CanonicalBatch = Annotated[list[NormalizedSourceDocument], Field(min_length=1, max_length=MAX_RECORDS)]
CANONICAL_SUBMISSION = TypeAdapter(NormalizedSourceDocument | CanonicalBatch)


def load_intake_template(name: str, *, provider: str | None = None) -> SourceMapping:
    if provider is not None:
        from pipeline.intake.provider_sources import stored_settings

        row = stored_settings(provider)
        if row and name in row["settings"]["templates"]:
            return SOURCE_MAPPING.validate_python(row["settings"]["templates"][name])
    path = os.environ.get("KNOWLEDGE_INTAKE_TEMPLATES_FILE")
    if not path:
        raise ValueError("No intake templates are configured")
    limit = 2 * 1024 * 1024
    with Path(path).open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Intake template configuration exceeds its size limit")
    try:
        templates = decode_source_json(data)
    except RecursionError as exc:
        raise ValueError("Intake template configuration is too deeply nested") from exc
    if not isinstance(templates, dict) or name not in templates:
        raise ValueError("Unknown intake template")
    return SOURCE_MAPPING.validate_python(templates[name])


def map_source_submission(payload: object, mapping: SourceMapping) -> list[tuple[object, NormalizedSourceDocument]]:
    if isinstance(mapping, WebhookMapping):
        return map_webhook(payload, mapping)
    if isinstance(mapping, JsonataMapping):
        from pipeline.intake.jsonata_runtime import evaluate

        transformed = evaluate(mapping.expression, payload)
        if transformed is None or transformed == []:
            return []
        submitted = CANONICAL_SUBMISSION.validate_python(transformed)
        documents = submitted if isinstance(submitted, list) else [submitted]
        if len({doc.external_id for doc in documents}) != len(documents):
            raise ValueError("Mapped external IDs must be unique within a delivery")
        # A general expression can aggregate many inputs. Preserve the original input once
        # on the delivery; receipts store its digest and the normalized result.
        evidence = {"source_payload_sha256": canonical_json_sha256(payload)}
        return [(evidence, document) for document in documents]
    submitted = CANONICAL_SUBMISSION.validate_python(payload)
    rows = payload if isinstance(payload, list) else [payload]
    documents = submitted if isinstance(submitted, list) else [submitted]
    if len({doc.external_id for doc in documents}) != len(documents):
        raise ValueError("External IDs must be unique within a submission")
    return list(zip(rows, documents))


_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]{0,8}")


def _read_path(value: object, path: str) -> object:
    if not path:
        return value
    for token in path.split("/")[1:]:
        part = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and _ARRAY_INDEX.fullmatch(part) and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _select(selector: JsonPointer | Selector, payload: object, record: dict) -> object:
    if isinstance(selector, str):
        return _read_path(record, selector)
    return _read_path(payload if selector.scope == "root" else record, selector.path)


def _matches(value: object, expected: object) -> bool:
    return type(value) is type(expected) and value == expected


def map_webhook(payload: object, mapping: WebhookMapping) -> list[tuple[dict, NormalizedSourceDocument]]:
    """Extract and validate the canonical fields, with no executable expressions."""
    roots = payload if mapping.each_root and isinstance(payload, list) else [payload]
    if len(roots) > MAX_RECORDS:
        raise ValueError(f"At most {MAX_RECORDS} root payloads are allowed")
    records = []
    for root in roots:
        selected = _read_path(root, mapping.records)
        selected = [selected] if isinstance(selected, dict) else selected
        if (not isinstance(selected, list) or len(records) + len(selected) > MAX_RECORDS
                or not all(isinstance(row, dict) for row in selected)):
            raise ValueError(f"The records pointer must select an object or up to {MAX_RECORDS} objects")
        records.extend((root, record) for record in selected)

    outputs = []
    identifiers = set()
    for root, record in records:
        if not all(_matches(_select(rule, root, record), rule.equals) for rule in mapping.filters):
            continue
        values = {
            field: _select(getattr(mapping.fields, field), root, record)
            for field in FieldMapping.model_fields if getattr(mapping.fields, field) is not None
        }
        if type(values.get("external_id")) is int:
            values["external_id"] = str(values["external_id"])
        document = NormalizedSourceDocument.model_validate({k: v for k, v in values.items() if v is not None})
        if document.external_id in identifiers:
            raise ValueError("Mapped external IDs must be unique within a delivery")
        identifiers.add(document.external_id)
        outputs.append((record, document))
    return outputs
