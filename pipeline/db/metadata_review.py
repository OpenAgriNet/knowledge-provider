"""Store suggestions and accepted evidence with the source they describe."""

import hashlib
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from pipeline.documents import document_validity
from pipeline.metadata.extraction import (
    extract_metadata,
    extraction_config,
    source_digest,
    text_blocks,
)
from pipeline.metadata.metadata_edits import MetadataEdit, supplied_facts
from pipeline.metadata.schema_packs import validate_subject_category

from . import audit, connection, document_reads, document_writes


class StaleMetadata(ValueError):
    """The input or proposal changed while review was in progress."""


def _read(conn, doc_id: str, chunk_number: int | None, *, lock: bool = False):
    if lock:
        conn.execute("SELECT id FROM documents WHERE id = %s::uuid FOR UPDATE", (doc_id,))
    suffix = " FOR SHARE" if lock else ""
    if chunk_number is not None:
        row = conn.execute(
            """SELECT id, chunk_number, original_text, edited_text, metadata_review
               FROM chunks WHERE document_id = %s::uuid AND chunk_number = %s""" + suffix,
            (doc_id, chunk_number),
        ).fetchone()
        if row is None:
            raise LookupError("Chunk not found")
        return str(row["id"]), row["metadata_review"], text_blocks([row], pages=False), None
    row = conn.execute(
        "SELECT metadata FROM documents WHERE id = %s::uuid" + suffix,
        (doc_id,),
    ).fetchone()
    if row is None:
        raise LookupError("Document not found")
    pages = conn.execute(
        """SELECT page_number, original_markdown, edited_markdown
           FROM pages WHERE document_id = %s::uuid ORDER BY page_number""" + suffix, (doc_id,),
    ).fetchall()
    blocks = text_blocks(pages, pages=True)
    if not blocks:
        chunks = conn.execute(
            """SELECT chunk_number, original_text, edited_text FROM chunks
               WHERE document_id = %s::uuid AND NOT is_excluded ORDER BY chunk_number""" + suffix, (doc_id,),
        ).fetchall()
        blocks = text_blocks(chunks, pages=False)
    metadata = row["metadata"]
    validity = document_validity.storage_value(document_validity.read_validity(metadata))
    return doc_id, metadata.get("metadata_review") or {}, blocks, validity


def _review_version(row_id: str, state: dict, blocks: list[tuple[int, str]], validity: dict | None) -> str:
    value = json.dumps([row_id, state, source_digest(blocks), validity], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def _response(row_id, state, blocks, validity, *, document=False):
    digest = source_digest(blocks)
    result = {key: value for key, value in state.items() if value.get("source_digest") == digest}
    if document:
        period = document_validity.read_validity({"validity": validity})
        result["validity"] = validity
        result["validity_days"] = period.calendar_days() if period else None
    result["version"] = _review_version(row_id, state, blocks, validity)
    return result


def get_review(doc_id: str, chunk_number: int | None, *, editable: bool = False) -> dict:
    with connection.get_connection() as conn:
        row_id, state, blocks, validity = _read(conn, doc_id, chunk_number)
    digest = source_digest(blocks)
    result = {key: value for key, value in state.items() if value.get("source_digest") == digest}
    if chunk_number is None:
        period = document_validity.read_validity({"validity": validity})
        result["validity"] = validity
        result["validity_days"] = period.calendar_days() if period else None
    if editable:
        result["version"] = _review_version(row_id, state, blocks, validity)
    return result


def _write(conn, doc_id, row_id, chunk_number, state):
    if chunk_number is None:
        conn.execute(
            "UPDATE documents SET metadata = jsonb_set(metadata, '{metadata_review}', %s) WHERE id = %s::uuid",
            (Jsonb(state), doc_id),
        )
    else:
        conn.execute("UPDATE chunks SET metadata_review = %s WHERE id = %s::uuid", (Jsonb(state), row_id))


async def generate_review(doc_id: str, chunk_number: int | None = None, *, automatic: bool = False) -> dict:
    with connection.get_connection() as conn:
        row_id, state, blocks, validity = _read(conn, doc_id, chunk_number)
    digest = source_digest(blocks)
    model = extraction_config()[1]
    if automatic and any(
        item.get("source_digest") == digest and item.get("model") == model for item in state.values()
    ):
        return state
    proposal = await extract_metadata(blocks)
    proposal["id"] = str(uuid4())
    with connection.get_connection() as conn:
        current_id, current, current_blocks, current_validity = _read(conn, doc_id, chunk_number, lock=True)
        if current_id != row_id or source_digest(current_blocks) != digest or current != state:
            raise StaleMetadata("Source or metadata changed. Generate metadata again.")
        current = {**{key: value for key, value in current.items() if value.get("source_digest") == digest}, "pending": proposal}
        _write(conn, doc_id, row_id, chunk_number, current)
    return current if automatic else _response(row_id, current, blocks, current_validity, document=chunk_number is None)


def accept_review(doc_id: str, workflow_id: str, chunk_number: int | None,
                  proposal_id: str, selected: list[int], *, actor_user_id: str) -> dict:
    with connection.get_connection() as conn:
        row_id, current, blocks, validity = _read(conn, doc_id, chunk_number, lock=True)
        pending = current.get("pending")
        if not pending or pending["id"] != proposal_id or pending["source_digest"] != source_digest(blocks):
            raise StaleMetadata("Source or metadata changed. Generate metadata again.")
        if any(index < 0 or index >= len(pending["facts"]) for index in selected):
            raise ValueError("Selected metadata is outside the proposal")
        facts = [pending["facts"][index] for index in sorted(set(selected))]
        fields = {fact["field"] for fact in facts}
        prior = current.get("accepted") or {}
        if prior.get("source_digest") != source_digest(blocks):
            prior = {}
        known_categories = {fact["value"] for fact in prior.get("facts", []) if fact["field"] == "category"}
        known_categories.update(fact["value"] for fact in supplied_facts(document_reads._fetch_document(conn, doc_id)) if fact["field"] == "category")
        for fact in facts:
            if fact["field"] == "category" and fact["value"] not in known_categories:
                validate_subject_category(fact["value"])
        accepted = {**pending, "facts": [fact for fact in prior.get("facts", []) if fact["field"] not in fields] + facts}
        if len(accepted["facts"]) > 256:
            raise ValueError("Metadata cannot contain more than 256 facts")
        if "overrides" in prior:
            accepted["overrides"] = sorted(set(prior["overrides"]) | fields)
        next_state = {"accepted": accepted}
        _write(conn, doc_id, row_id, chunk_number, next_state)
        _record_edit(conn, doc_id, workflow_id, chunk_number, accepted, actor_user_id)
    return _response(row_id, next_state, blocks, validity, document=chunk_number is None)


def _record_edit(conn, doc_id, workflow_id, chunk_number, accepted, actor_user_id, *, validity_change=None):
    conn.execute(
        "UPDATE documents SET metadata = metadata || %s WHERE id = %s::uuid",
        (Jsonb({"reindex_required": True, "reindex_reason": "Accepted knowledge metadata changed"}), doc_id),
    )
    audit.log_audit(
        workflow_id, doc_id, "chunk_edit" if chunk_number is not None else "document_metadata_edit",
        entity_type="chunk" if chunk_number is not None else "document", entity_id=chunk_number,
        field_name="metadata_review", metadata={
            "proposal_id": accepted["id"], "accepted_count": len(accepted["facts"]), "origin": accepted["origin"],
            **({"validity": validity_change} if validity_change is not None else {}),
        },
        actor_user_id=actor_user_id, _conn=conn,
    )


def save_review(doc_id: str, workflow_id: str, chunk_number: int | None,
                edit: MetadataEdit, *, actor_user_id: str) -> dict:
    with connection.get_connection() as conn:
        row_id, current, blocks, validity = _read(conn, doc_id, chunk_number, lock=True)
        if _review_version(row_id, current, blocks, validity) != edit.version:
            raise StaleMetadata("Source or metadata changed. Reopen the metadata editor.")
        validity_edited = "validity" in edit.model_fields_set
        if chunk_number is not None and validity_edited:
            raise ValueError("Validity is edited on the document")
        if not blocks and (edit.facts or edit.overrides or not validity_edited):
            raise ValueError("Save the source text before editing its metadata")
        digest = source_digest(blocks)
        known = [
            fact for key, item in current.items() if key == "accepted" and item.get("source_digest") == digest
            for fact in item.get("facts", [])
        ]
        known.extend(supplied_facts(document_reads._fetch_document(conn, doc_id)))
        evidence_facts = [
            fact for item in current.values() if item.get("source_digest") == digest
            for fact in item.get("facts", [])
        ]
        facts = []
        for item in edit.facts:
            if item.field == "category" and not any(fact["field"] == "category" and fact["value"] == item.value for fact in known):
                validate_subject_category(item.value)
            fact = item.model_dump(exclude_none=True, exclude={"evidence"})
            proof = item.evidence.model_dump() if item.evidence else None
            if proof and any(
                prior["field"] == item.field and prior["value"] == item.value
                and prior.get("area") == fact.get("area") and prior.get("evidence") == proof
                for prior in evidence_facts
            ):
                fact["evidence"] = proof
            facts.append(fact)
        accepted = {
            "id": str(uuid4()), "source_digest": digest, "origin": "edited",
            "facts": facts, "overrides": edit.overrides,
        }
        state = {"accepted": accepted}
        _write(conn, doc_id, row_id, chunk_number, state)
        validity_change = None
        if validity_edited:
            new_validity = document_validity.storage_value(edit.validity)
            if new_validity != validity or new_validity is None:
                validity_change = {"before": validity, "after": new_validity}
                document_writes.update_document_fields(doc_id, validity=new_validity, _conn=conn)
                validity = new_validity
        _record_edit(
            conn, doc_id, workflow_id, chunk_number, accepted, actor_user_id, validity_change=validity_change,
        )
    return _response(row_id, state, blocks, validity, document=chunk_number is None)


def document_id_for_workflow(workflow_id: str) -> str:
    with connection.get_connection() as conn:
        return document_reads._require_document_uuid(conn, workflow_id)
