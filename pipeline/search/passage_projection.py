"""Prepare search passages and frozen revision content from stored documents."""

import hashlib
import os
import re
from pathlib import Path

from pipeline.documents import document_validity
from pipeline.documents.document_text import _infer_section, is_reference_section
from pipeline.metadata.extraction import (
    accepted_metadata,
    chunk_projection,
    document_projection,
    normalized_key,
    text_blocks,
)
from pipeline.metadata.knowledge_metadata import KnowledgeMetadata
from pipeline.metadata.localization import project_chunk_localization
from pipeline.network.catalog import scheme_catalog

# These fields represent the effective passage metadata. A document-scoped
# value is stored only when it differs, including an explicit empty list.
SCOPED_SEARCH_FIELDS = ("subject_categories", "crop_keys", "applicability_groups")
REVISION_SEARCH_FIELDS = (*SCOPED_SEARCH_FIELDS, "document_scope", "coverage_area_keys", "season_keys", "metadata", "subjects", "agriculture_subject_ids")


def _scoped_fields(projected: dict, category_labels: dict) -> dict:
    return {
        "subject_categories": [category_labels.get(key, key.title()) for key in projected.get("categories", [])],
        "crop_keys": projected.get("crops", []),
        "applicability_groups": [
            {target: group[key] for key, target in (
                ("areas", "coverage_area_keys"), ("seasons", "season_keys"), ("all_areas", "all_areas"),
            ) if key in group}
            for group in projected.get("context", [])
        ],
    }


def apply_search_metadata(record: dict, document: dict, effective: dict, *, category_labels: dict | None = None) -> dict:
    labels = {normalized_key(label): label for label in record.get("subject_categories", [])}
    labels.update({"crop": "Crop", "livestock": "Livestock", "scheme": "Scheme"})
    labels.update(category_labels or {})
    baseline = _scoped_fields(document, labels)
    chunk = _scoped_fields(effective, labels)
    for key, value in chunk.items():
        if value:
            record[key] = value
        else:
            record.pop(key, None)
    groups = chunk["applicability_groups"]
    for key in ("coverage_area_keys", "season_keys"):
        keys = list(dict.fromkeys(value for group in groups for value in group.get(key, [])))
        if keys:
            record[key] = keys
        else:
            record.pop(key, None)
    difference = {key: value for key, value in baseline.items() if value != chunk[key]}
    if difference:
        record["document_scope"] = {"fields": list(difference), **difference}
    else:
        record.pop("document_scope", None)
    return record


def effective_subjects(subjects: list[dict], crops: list[str], reviews: list[dict], *, untyped_are_crops: bool) -> list[dict]:
    """Resolve crop edits before projecting identities, attributes and lookup keys."""
    labels = {
        normalized_key(fact["value"]): fact["value"]
        for review in reviews for fact in review.get("facts", []) if fact["field"] == "crop"
    }
    overridden = any("crop" in review.get("overrides", []) for review in reviews)
    result = []
    represented = set()
    for subject in subjects:
        name = (subject.get("descriptor") or {}).get("name")
        key = normalized_key(name) if name else None
        if subject.get("type") == "crop" or (not subject.get("type") and untyped_are_crops):
            if (key is not None and key not in crops) or (key is None and overridden):
                continue
            if key:
                represented.add(key)
        result.append(subject)
    result.extend(
        {"type": "crop", "descriptor": {"name": labels.get(key, key)}}
        for key in crops if key not in represented
        and not any(normalized_key((s.get("descriptor") or {}).get("name") or "") == key for s in result)
    )
    return result


def migrate_passage_metadata(record: dict, *, reviewed: bool = False) -> dict:
    """Translate a frozen revision from the retired dual projection."""
    if reviewed or "metadata_document" in record or "metadata_chunk" in record:
        record = dict(record)
        document = record.pop("metadata_document", {})
        effective = record.pop("metadata_chunk", {})
        return apply_search_metadata(record, document, effective)
    return record


def clean_text_for_ingestion(text: str) -> str:
    """Clean translation preambles from text before ingestion."""
    if not text:
        return text

    result = text
    result = re.sub(
        r"^Here is the translated text from \*\*[^*]+\*\* to English[^:]*:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the translated text from [^:]+?:\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the translated text[^:]*:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the (?:English )?translation[^:]*:?\s*\n*",
        "",
        result,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    result = re.sub(
        r"^Here is the translated text with[^:]+:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )

    prefixes = [
        r"^(?:the\s+)?english\s+translation:?\s*\n*",
        r"^(?:the\s+)?translation:?\s*\n*",
        r"^translated\s+(?:text|content):?\s*\n*",
        r"^##?\s*(?:english\s+)?translation\s*\n+",
        r"^---+\s*\n+",
        r"^\*\*Translation:?\*\*\s*\n*",
    ]
    for pattern in prefixes:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE | re.MULTILINE)

    result = re.sub(r"\n*-{3,}\s*$", "", result)
    return result.strip()


def _normalize_instance(value: str | None) -> str:
    """Ingest-side instance normalizer (mirrors auth.tenancy without importing FastAPI)."""
    text = (value or "").strip().lower()
    if text:
        return text
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def _prepare_records(
    document_id: str,
    filename: str,
    chunks: list[dict],
    workflow_id: str | None = None,
    description: str | None = None,
    include_e5_prefix_field: bool = True,
    instance: str | None = None,
    kind: str = "document",
    code: str | None = None,
    name: str | None = None,
    aliases: list[str] | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    validity: dict | None = None,
    provider_id: str | None = None,
    display_name: str | None = None,
    crop_subjects: list[dict] | None = None,
    knowledge_metadata: dict | None = None,
    metadata_review: dict | None = None,
    source_url: str | None = None,
    pages: list[dict] | None = None,
    source_first: bool = False,
) -> list[dict]:
    resolved_instance = _normalize_instance(instance)
    doc_hash = hashlib.md5(document_id.encode()).hexdigest()
    external_slug = workflow_id or filename
    display_title = display_name or name or Path(filename).stem

    kind = (kind or "document").strip().lower()
    is_scheme = kind == "scheme" and bool((code or "").strip())
    scheme_name = name or display_title
    # Merge curator-supplied, bootstrap and derived aliases so a scheme is never
    # indexed with an empty alias list (it would then only match its exact name).
    resolved_aliases = (
        scheme_catalog.resolve_scheme_aliases(code, scheme_name, aliases)
        if is_scheme
        else aliases or []
    )
    # One period for the whole document, resolved before the loop: every chunk
    # of a document expires together, and re-deriving it per chunk would let a
    # midnight boundary split one document across two periods.
    period = document_validity.read_validity(
        {"validity": validity} if validity is not None else {"valid_from": valid_from, "valid_to": valid_to}
    )
    days = period.calendar_days() if period else {}
    metadata = KnowledgeMetadata.model_validate(knowledge_metadata or {})
    subjects = [subject.model_dump(exclude_none=True, exclude_defaults=True) for subject in metadata.subjects]
    for crop in crop_subjects or []:
        reference = {**crop, "type": "crop"}
        if reference not in subjects:
            subjects.append(reference)
    categories = list(metadata.subject_categories)
    if any(subject["type"] == "crop" for subject in subjects) and "Crop" not in categories:
        categories.append("Crop")
    if is_scheme and "Scheme" not in categories:
        categories.append("Scheme")
    known_references = [{key: value for key, value in subject.items() if key not in {"type", "attributes"}} for subject in subjects]
    for subject in metadata.agriculture_subjects:
        reference = subject.model_dump(exclude_none=True)
        if reference not in known_references:
            subjects.append(reference)
    applicability = {
        "subject_categories": categories,
        "excluded_coverage_area_keys": [area.lookup_key() for area in metadata.excluded_coverage_areas],
        "tag_keys": [tag.casefold() for tag in metadata.tags],
    }

    document_review = accepted_metadata(
        metadata_review or {}, text_blocks(pages or [], pages=True) or text_blocks([chunk for chunk in chunks if not chunk.get("is_excluded")], pages=False),
    )
    document_payload = document_projection(document_review, metadata, subjects, categories)
    records = []
    for chunk in chunks:
        if chunk.get("is_excluded", False):
            continue

        raw_text = chunk.get("edited_text") if chunk.get("edited_text") is not None else chunk.get("original_text", "")
        chunk_num = chunk.get("chunk_number", 0)
        text = clean_text_for_ingestion(raw_text)
        is_ref = is_reference_section(text)

        section = _infer_section(text, chunk.get("section_title"))
        point_id = hashlib.md5(f"{doc_hash}_{chunk_num}_{text[:50]}".encode()).hexdigest()
        record = {
            "_id": point_id,
            "doc_id": document_id,
            "workflow_id": workflow_id or "",
            "instance": resolved_instance,  # vector payload key, kept for consumers
            "provider_id": provider_id,
            "type": kind,
            "source": "docs-pipeline",
            "filename": external_slug,
            "name": display_title,
            "description": description or metadata.description or "",
            "text": text,
            "chunk_num": chunk_num,
            "chunk_id": point_id,
            "chunk_index": chunk_num,
            "section": section,
            "token_count": chunk.get("token_count", 0),
            "page_start": chunk.get("page_start", 1),
            "page_end": chunk.get("page_end", 1),
            "is_reference": is_ref,
        }
        if period is not None:
            record["validity"] = document_validity.storage_value(period)
            if days["valid_from"]:
                record["start_date"] = days["valid_from"]
            if days["valid_to"]:
                record["end_date"] = days["valid_to"]
        record["applicability_scope"] = "broad" if metadata.broad_applicability else "declared"
        record.update({key: values for key, values in applicability.items() if values})
        if chunk.get("tags"):
            record["chunk_tags"] = chunk["tags"]
            record["tag_keys"] = list(dict.fromkeys([
                *applicability["tag_keys"], *(tag.casefold() for tag in chunk["tags"]),
            ]))
        if metadata.localized:
            record["localized_labels"] = {
                tag: value.model_dump(exclude_none=True) for tag, value in metadata.localized.items()
            }
        if metadata.topics:
            record["topics"] = metadata.topics
        if metadata.tags:
            record["document_tags"] = metadata.tags
        chunk_review = accepted_metadata(chunk.get("metadata_review") or {}, text_blocks([chunk], pages=False))
        effective = chunk_projection(chunk_review, document_payload)
        resolved = effective_subjects(
            subjects, effective.get("crops", []), [document_review, chunk_review], untyped_are_crops=categories == ["Crop"],
        )
        if resolved:
            record["subjects"] = [{key: value for key, value in subject.items() if key != "attributes"} for subject in resolved]
        subject_ids = list(dict.fromkeys(subject["subjectId"] for subject in resolved if subject.get("subjectId")))
        if subject_ids:
            record["agriculture_subject_ids"] = subject_ids
        extra_metadata = {key: value for key, value in (
            ("attributes", metadata.attributes),
            ("subjects", [subject for subject in resolved if subject.get("attributes")]),
        ) if value}
        category_labels = {normalized_key(fact["value"]): fact["value"]
                           for review in (document_review, chunk_review) for fact in review.get("facts", [])
                           if fact["field"] == "category"}
        apply_search_metadata(record, document_payload, effective, category_labels=category_labels)
        if extra_metadata:
            record["metadata"] = extra_metadata
        if source_url:
            record["source_url"] = source_url
        if code:
            record["document_identity"] = {
                "code": code.strip().lower(),
                "name": scheme_name,
                "aliases": resolved_aliases,
            }
        if include_e5_prefix_field:
            record["text_for_embedding"] = f"passage: {text}" if text else "passage:"
        if source_first and pages:
            record.update(project_chunk_localization(chunk, pages, source_first=True))
        records.append(record)

    return records


def _document_fields_from_doc(doc: dict | None) -> dict:
    """Extract kind/code/name/aliases kwargs for _prepare_records from a documents row."""
    doc = doc or {}
    return {
        "kind": (doc.get("kind") or "document"),
        "code": doc.get("code"),
        "name": doc.get("name"),
        "aliases": [str(a) for a in (doc.get("aliases") or [])],
        "crop_subjects": doc.get("crop_subjects") or [],
        "knowledge_metadata": doc.get("knowledge_metadata") or {},
        "metadata_review": doc.get("metadata_review") or {},
    }


def _validity_fields_from_doc(doc: dict | None) -> dict:
    """Project the canonical period, including periods on frozen legacy revisions."""
    doc = doc or {}
    if "validity" in doc:
        return {"validity": doc["validity"]}
    if doc.get("validity_unspecified"):
        return {"validity": None}
    return {"validity": document_validity.storage_value(document_validity.read_validity(doc))}


def prepare_ingestion_records(
    document_id: str,
    filename: str,
    chunks: list[dict],
    workflow_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    instance: str | None = None,
) -> list[dict]:
    """Public helper used by tests and scripts when preparing vector-index payloads."""
    return _prepare_records(
        document_id,
        filename,
        chunks,
        workflow_id=workflow_id,
        name=name,
        description=description,
        instance=instance,
    )


def _passage_schema_field_names() -> set[str]:
    """Field names for the canonical passage schema (E5 text_for_embedding + full metadata)."""
    settings = _passage_schema_definition(use_tensor_prefix_field=True)
    return {f.get("name") for f in settings.get("allFields", []) if isinstance(f, dict) and f.get("name")}


def _passage_schema_definition(use_tensor_prefix_field: bool = True) -> dict:
    tensor_field = "text_for_embedding" if use_tensor_prefix_field else "text"
    all_fields = [
        {"name": "doc_id", "type": "text", "features": ["filter"]},
        {"name": "workflow_id", "type": "text", "features": ["filter"]},
        {"name": "instance", "type": "text", "features": ["filter"]},
        {"name": "provider_id", "type": "text", "features": ["filter"]},
        {"name": "type", "type": "text", "features": ["filter"]},
        {"name": "source", "type": "text", "features": ["filter"]},
        {"name": "filename", "type": "text", "features": ["filter"]},
        {"name": "name", "type": "text", "features": ["lexical_search"]},
        {"name": "description", "type": "text", "features": ["lexical_search"]},
        {"name": "chunk_num", "type": "int", "features": ["filter"]},
        {"name": "section", "type": "text", "features": ["filter"]},
        {"name": "token_count", "type": "int", "features": ["filter"]},
        {"name": "page_start", "type": "int", "features": ["filter"]},
        {"name": "page_end", "type": "int", "features": ["filter"]},
        {"name": "is_reference", "type": "bool", "features": ["filter"]},
        {"name": "text", "type": "text", "features": ["lexical_search"]},
        # Document validity - filtered on at search time, so the day a chunk
        # starts and stops answering travels with the chunk itself.
        {"name": "validity", "type": "object"},
        {"name": "start_date", "type": "date", "features": ["filter"]},
        {"name": "end_date", "type": "date", "features": ["filter"]},
    ]
    if use_tensor_prefix_field:
        all_fields.append({"name": "text_for_embedding", "type": "text"})

    return {
        "type": "structured",
        "vectorNumericType": "float",
        "model": "hf/multilingual-e5-large",
        "normalizeEmbeddings": False,
        "textPreprocessing": {"splitLength": 3, "splitOverlap": 1, "splitMethod": "sentence"},
        "allFields": all_fields,
        "tensorFields": [tensor_field],
    }


def _revision_snapshot(doc, pages, records, vector_doc_id, source_url, *, chunks=()):
    blocks = text_blocks(pages or [], pages=True) or text_blocks([chunk for chunk in chunks if not chunk.get("is_excluded")], pages=False)
    document_annotations = accepted_metadata(doc.get("metadata_review") or {}, blocks)
    chunk_annotations = {
        str(chunk["chunk_number"]): approved
        for chunk in chunks
        if (approved := accepted_metadata(chunk.get("metadata_review") or {}, text_blocks([chunk], pages=False)))
    }
    return {
        "vector_doc_id": vector_doc_id,
        "search_metadata_version": 2,
        "source": {
            **{key: doc.get(key) for key in ("filename", "filepath", "source_file_fingerprint")},
            "url": source_url,
        },
        "metadata": {
            **{key: doc.get(key) for key in (
                "instance", "kind", "code", "name", "aliases", "crop_subjects", "knowledge_metadata",
            )},
            **_validity_fields_from_doc(doc),
        },
        "pages": [{key: page.get(key) for key in (
            "page_number", "detected_language", "original_markdown", "edited_markdown", "translations", "metadata",
        )} for page in pages],
        **({"annotations": {"document": document_annotations, "chunks": chunk_annotations}}
           if document_annotations or chunk_annotations else {}),
        "passages": records,
    }


def _draft_revision_passages(records: list[dict], revision_doc_id: str) -> list[dict]:
    passages = []
    for record in records:
        if not record["text"].strip():
            continue
        point_id = hashlib.md5(f"{revision_doc_id}:{record['_id']}".encode()).hexdigest()
        passages.append({
            **{key: value for key, value in record.items() if key != "provider_id"},
            "_id": point_id,
            "chunk_id": point_id,
            "doc_id": revision_doc_id,
        })
    return passages


def _release_passages(revision: dict, network_id: str, provider_id: str | None) -> list[dict]:
    snapshot = revision["snapshot"]
    reviewed = snapshot.get("search_metadata_version") != 2 and bool(snapshot.get("annotations"))
    return [
        {**migrate_passage_metadata(record, reviewed=reviewed), "revision_id": revision["id"],
         "release_network_id": network_id, "provider_id": provider_id}
        for record in snapshot["passages"]
    ]
