"""Prepare search passages and frozen revision content from stored documents."""

import hashlib
import os
import re
from pathlib import Path

from pipeline.documents import document_validity
from pipeline.network.catalog import scheme_catalog
from pipeline.documents.document_text import _infer_section, is_reference_section
from pipeline.network.release_policy import instance_display_name
from pipeline.metadata.knowledge_metadata import KnowledgeMetadata
from pipeline.metadata.localization import project_chunk_localization


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
    provider_id: str | None = None,
    display_name: str | None = None,
    crop_subjects: list[dict] | None = None,
    knowledge_metadata: dict | None = None,
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
        else []
    )
    instance_name = instance_display_name(resolved_instance)
    # One period for the whole document, resolved before the loop: every chunk
    # of a document expires together, and re-deriving it per chunk would let a
    # midnight boundary split one document across two periods.
    validity = document_validity.period_from_row(valid_from, valid_to)
    metadata = KnowledgeMetadata.model_validate(knowledge_metadata or {})
    categories = list(metadata.subject_categories)
    if crop_subjects and "Crop" not in categories:
        categories.append("Crop")
    if is_scheme and "Scheme" not in categories:
        categories.append("Scheme")
    subject_ids = [entry.get("subjectId") for entry in crop_subjects or []]
    subject_ids.extend(subject.subjectId for subject in metadata.agriculture_subjects)
    applicability = {
        "subject_categories": categories,
        "agriculture_subject_ids": list(dict.fromkeys(subject_id for subject_id in subject_ids if subject_id)),
        "coverage_area_keys": [area.lookup_key() for area in metadata.coverage_areas],
        "excluded_coverage_area_keys": [area.lookup_key() for area in metadata.excluded_coverage_areas],
        "season_keys": [season.casefold() for season in metadata.seasons],
        "applicability_groups": [group.index_value() for group in metadata.applicability_groups],
        "tag_keys": [tag.casefold() for tag in metadata.tags],
    }

    records = []
    for chunk in chunks:
        if chunk.get("is_excluded", False):
            continue

        raw_text = chunk.get("edited_text") or chunk.get("original_text", "")
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
            "instance_name": instance_name,
            "provider_id": provider_id,
            "crop_subjects": crop_subjects or [],
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
        if validity is not None:
            record["start_date"] = validity.start_date
            # Omitted, not null, when the document never expires: the search
            # filter's "end_date missing" branch is what keeps it answerable
            # forever, and _record_payload drops None anyway.
            if validity.end_date is not None:
                record["end_date"] = validity.end_date
        record["applicability_scope"] = "broad" if metadata.broad_applicability else "declared"
        record.update({key: values for key, values in applicability.items() if values})
        if chunk.get("tags"):
            record["chunk_tags"] = chunk["tags"]
            record["tag_keys"] = list(dict.fromkeys([
                *applicability["tag_keys"], *(tag.casefold() for tag in chunk["tags"]),
            ]))
        if knowledge_metadata:
            record["knowledge_metadata"] = metadata.storage_value()
        if source_url:
            record["source_url"] = source_url
        if is_scheme:
            record["scheme_code"] = (code or "").strip().lower()
            record["scheme_name"] = scheme_name
            record["scheme_aliases"] = resolved_aliases
        if include_e5_prefix_field:
            record["text_for_embedding"] = f"passage: {text}" if text else "passage:"
        if source_first and pages:
            record.update(project_chunk_localization(chunk, pages, source_first=True))
        records.append(record)

    return records


def _scheme_fields_from_doc(doc: dict | None) -> dict:
    """Extract kind/code/name/aliases kwargs for _prepare_records from a documents row."""
    doc = doc or {}
    return {
        "kind": (doc.get("kind") or "document"),
        "code": doc.get("code"),
        "name": doc.get("name"),
        "aliases": [str(a) for a in (doc.get("aliases") or [])],
        "crop_subjects": doc.get("crop_subjects") or [],
        "knowledge_metadata": doc.get("knowledge_metadata") or {},
    }


def _validity_fields_from_doc(doc: dict | None) -> dict:
    """Extract the validity kwargs for `_prepare_records` from a documents row.

    A row with no stored period is stamped with the default anchored on its
    upload day - start there, no end - so every chunk written from here on
    carries a start. Old points already in the index keep none until their
    document is reingested, which is what keeps them searchable meanwhile.
    """
    doc = doc or {}
    period = document_validity.period_from_row(
        doc.get("valid_from"), doc.get("valid_to")
    )
    if period is None:
        period = document_validity.period_from_upload_timestamp(doc.get("created_at"))
    return {"valid_from": period.start_date, "valid_to": period.end_date}


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
        {"name": "instance_name", "type": "text", "features": ["filter"]},
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
        {"name": "priority", "type": "float", "features": ["score_modifier", "filter"]},
        # Document validity - filtered on at search time, so the day a chunk
        # starts and stops answering travels with the chunk itself.
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


def _revision_snapshot(doc, pages, records, vector_doc_id, source_url):
    return {
        "vector_doc_id": vector_doc_id,
        "source": {
            **{key: doc.get(key) for key in ("filename", "filepath", "source_file_fingerprint")},
            "url": source_url,
        },
        "metadata": {key: doc.get(key) for key in (
            "instance", "kind", "code", "name", "aliases", "valid_from", "valid_to", "crop_subjects", "knowledge_metadata",
        )},
        "pages": [{key: page.get(key) for key in (
            "page_number", "detected_language", "original_markdown", "edited_markdown", "translations", "metadata",
        )} for page in pages],
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
    return [
        {**record, "revision_id": revision["id"], "release_network_id": network_id, "provider_id": provider_id}
        for record in revision["snapshot"]["passages"]
    ]
