"""Store suggestions and accepted evidence with the source they describe."""

import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from uuid import uuid4

from psycopg.types.json import Jsonb

from pipeline.documents import document_validity
from pipeline.metadata.extraction import (
    MetadataModel,
    auto_accept_fields,
    chunk_tagging_timeout,
    extract_metadata,
    extraction_config,
    fact_key,
    pack_windows,
    source_digest,
    text_blocks,
)
from pipeline.metadata.metadata_edits import MetadataEdit, supplied_facts
from pipeline.metadata.schema_packs import validate_subject_category

from . import audit, connection, document_reads, document_writes

_MAX_FACTS = 256  # same review limit as accept_review and whole-document extraction


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


def _store_proposal(conn, doc_id, row_id, chunk_number, current, *, automatic, workflow_id) -> None:
    """Write current (holding the new pending proposal), auto-accepting configured fields."""
    proposal = current["pending"]
    auto_accepted = []
    if automatic and "accepted" not in current and (auto := auto_accept_fields()):
        # Reviewer-free acceptance only when nothing is accepted for this source yet, so a
        # reviewer's saved or accepted metadata is never replaced. Same category rule and
        # fact cap as accept_review; invalid suggestions are skipped, not fatal.
        for fact in proposal["facts"]:
            if fact["field"] not in auto:
                continue
            if fact["field"] == "category":
                try:
                    validate_subject_category(fact["value"])
                except ValueError:
                    continue
            auto_accepted.append(fact)
        if auto_accepted and len(auto_accepted) <= 256:
            current["accepted"] = {**proposal, "origin": "extracted-auto", "facts": auto_accepted}
        else:
            auto_accepted = []
    _write(conn, doc_id, row_id, chunk_number, current)
    if auto_accepted:
        audit.log_audit(
            workflow_id or doc_id, doc_id,
            "chunk_edit" if chunk_number is not None else "document_metadata_edit",
            entity_type="chunk" if chunk_number is not None else "document", entity_id=chunk_number,
            field_name="metadata_review", actor_username="system:auto-accept",
            metadata={"proposal_id": proposal["id"], "accepted_count": len(auto_accepted), "origin": "extracted-auto"},
            _conn=conn,
        )


async def generate_review(
    doc_id: str, chunk_number: int | None = None, *, automatic: bool = False, workflow_id: str | None = None,
) -> dict:
    with connection.get_connection() as conn:
        row_id, state, blocks, validity = _read(conn, doc_id, chunk_number)
    digest = source_digest(blocks)
    model = extraction_config()[1]
    if automatic and any(
        item.get("source_digest") == digest and item.get("model") == model for item in state.values()
    ):
        return state
    proposal = await extract_metadata(blocks)
    current, current_validity = _save_proposal(
        doc_id, chunk_number, row_id, state, digest, proposal, automatic=automatic, workflow_id=workflow_id,
    )
    return current if automatic else _response(row_id, current, blocks, current_validity, document=chunk_number is None)


def _save_proposal(doc_id, chunk_number, row_id, state, digest, proposal, *, automatic, workflow_id):
    """Store a proposal made from state and source digest, unless either changed meanwhile."""
    proposal["id"] = str(uuid4())
    with connection.get_connection() as conn:
        current_id, current, current_blocks, current_validity = _read(conn, doc_id, chunk_number, lock=True)
        if current_id != row_id or source_digest(current_blocks) != digest or current != state:
            raise StaleMetadata("Source or metadata changed. Generate metadata again.")
        current = {**{key: value for key, value in current.items() if value.get("source_digest") == digest}, "pending": proposal}
        _store_proposal(conn, doc_id, row_id, chunk_number, current, automatic=automatic, workflow_id=workflow_id)
    return current, current_validity


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
        if chunk_number is not None:
            _combine(conn, doc_id, workflow_id, chunk_edit=True)
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
        if chunk_number is not None:
            _combine(conn, doc_id, workflow_id, chunk_edit=True)
    return _response(row_id, state, blocks, validity, document=chunk_number is None)


def document_id_for_workflow(workflow_id: str) -> str:
    with connection.get_connection() as conn:
        return document_reads._require_document_uuid(conn, workflow_id)


async def tag_chunks(doc_id: str, workflow_id: str | None = None, progress=None) -> dict:
    """Suggest metadata for every included chunk, then combine them into the document's suggestion.

    Chunks travel several to a request (numbered blocks, as pages do), so a long document
    needs few requests; each fact keeps the chunk it came from. Chunks already tagged for
    their current text by this model are skipped, so a retry resumes where the last attempt
    stopped. A failed request is reported for its chunks, not fatal.
    """
    with connection.get_connection() as conn:
        rows = conn.execute(
            """SELECT id, chunk_number, original_text, edited_text, metadata_review FROM chunks
               WHERE document_id = %s::uuid AND NOT is_excluded ORDER BY chunk_number""",
            (doc_id,),
        ).fetchall()
    if not rows:
        return {"chunks": 0, "failed": []}
    model = MetadataModel()
    todo, blocks = {}, []
    for row in rows:
        chunk_blocks = text_blocks([row], pages=False)
        state = row["metadata_review"] or {}
        digest = source_digest(chunk_blocks)
        if not chunk_blocks or any(
            item.get("source_digest") == digest and item.get("model") == model.model for item in state.values()
        ):
            continue
        todo[row["chunk_number"]] = (str(row["id"]), state, digest)
        blocks.extend(chunk_blocks)
    windows = pack_windows(blocks)
    # A chunk longer than one window spans several; it is saved once all of them answer.
    remaining = Counter(number for window in windows for number in {block for block, _, _ in window})
    found, errors, failed, saved = defaultdict(list), {}, [], set()

    def save(number):
        if number in errors:
            failed.append((number, errors[number]))
            return
        unique = {}
        for fact in found.pop(number, []):
            unique.setdefault(fact_key(fact), fact)
        if len(unique) > _MAX_FACTS:
            failed.append((number, "TooManyFacts"))
            return
        row_id, state, digest = todo[number]
        try:
            _save_proposal(doc_id, number, row_id, state, digest, model.proposal(digest, list(unique.values())),
                           automatic=True, workflow_id=workflow_id)
        except Exception as exc:
            failed.append((number, type(exc).__name__))
        else:
            saved.add(number)

    async def run(window):
        numbers = sorted({block for block, _, _ in window})
        try:
            for fact in await model.facts(window):
                found[fact["evidence"]["block"]].append(fact)
        except Exception as exc:
            errors.update({number: type(exc).__name__ for number in numbers})
        for number in numbers:
            remaining[number] -= 1
            if not remaining[number]:
                save(number)
        if progress:
            progress(len(todo) - sum(1 for left in remaining.values() if left), len(todo))

    # Saved chunks stay saved: on timeout the rest are reported as failed and a retry resumes them.
    try:
        async with model:
            await asyncio.wait_for(asyncio.gather(*(run(window) for window in windows)), chunk_tagging_timeout())
    except asyncio.TimeoutError:
        done = saved | {number for number, _ in failed}
        failed.extend((number, "Timeout") for number in todo if number not in done)
    combine_chunk_reviews(doc_id, workflow_id)
    return {"chunks": len(rows), "failed": sorted(failed)}


def _chunk_facts(chunk: dict) -> list[dict]:
    """A reviewer's accepted chunk metadata wins; otherwise the full machine suggestion."""
    digest = source_digest(text_blocks([chunk], pages=False))
    fresh = {key: value for key, value in (chunk["metadata_review"] or {}).items() if value.get("source_digest") == digest}
    accepted = fresh.get("accepted")
    review = accepted if accepted and accepted.get("origin") != "extracted-auto" else fresh.get("pending") or accepted
    return (review or {}).get("facts", [])


def combine_chunk_reviews(doc_id: str, workflow_id: str | None = None) -> dict:
    """Make the document suggestion the union of its chunks' facts, one per crop or category."""
    with connection.get_connection() as conn:
        return _combine(conn, doc_id, workflow_id)


def _combine(conn, doc_id: str, workflow_id: str | None, *, chunk_edit: bool = False) -> dict:
    row_id, current, blocks, _ = _read(conn, doc_id, None, lock=True)
    if not blocks:
        return current
    digest = source_digest(blocks)
    fresh = {key: value for key, value in current.items() if value.get("source_digest") == digest}
    if chunk_edit and not any(value.get("origin") == "chunks" for value in fresh.values()):
        # The document list was not built from chunks, or a reviewer saved their own.
        return current
    chunks = conn.execute(
        """SELECT chunk_number, original_text, edited_text, metadata_review FROM chunks
           WHERE document_id = %s::uuid AND NOT is_excluded ORDER BY chunk_number""",
        (doc_id,),
    ).fetchall()
    facts = {}
    for chunk in chunks:
        for fact in _chunk_facts(chunk):
            facts.setdefault(fact_key(fact), fact)
    # Evidence blocks here are chunk numbers: each fact keeps the passage it came from.
    proposal = {
        "id": str(uuid4()), "source_digest": digest, "origin": "chunks",
        "model": extraction_config()[1], "facts": list(facts.values()),
    }
    if len(proposal["facts"]) > _MAX_FACTS:
        # Keep the earliest facts and say so: a reviewer adds anything missed with Edit metadata.
        proposal["facts_total"] = len(proposal["facts"])
        proposal["facts"] = proposal["facts"][:_MAX_FACTS]
    current = {**fresh, "pending": proposal}
    if (current.get("accepted") or {}).get("origin") == "extracted-auto":
        # A machine-accepted list follows the chunks; a reviewer's accepted list is kept.
        del current["accepted"]
    _store_proposal(conn, doc_id, row_id, None, current, automatic=True, workflow_id=workflow_id)
    return current


async def generate_document_review(doc_id: str, workflow_id: str | None = None) -> dict:
    """Document Generate: tag chunks still missing suggestions, then combine them.

    A document without chunks yet is read directly.
    """
    result = await tag_chunks(doc_id, workflow_id)
    if not result["chunks"]:
        return await generate_review(doc_id)
    if len(result["failed"]) == result["chunks"]:
        raise RuntimeError("Metadata generation failed for every chunk")
    with connection.get_connection() as conn:
        row_id, state, blocks, validity = _read(conn, doc_id, None)
    response = _response(row_id, state, blocks, validity, document=True)
    if result["failed"]:
        response["failed_chunks"] = [number for number, _ in result["failed"]]
    return response
