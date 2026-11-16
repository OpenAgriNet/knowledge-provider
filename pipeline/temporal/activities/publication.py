"""Activities for network publication and revision selection."""

import asyncio
import os
from uuid import (
    NAMESPACE_URL,
    uuid5,
)

from temporalio import activity
from temporalio.exceptions import ApplicationError

from pipeline.db.index_state import index_key_for
from pipeline.documents.document_repository import DocumentRepository
from pipeline.network.release_policy import managed_release_targets, publish_to_network_enabled
from pipeline.documents.models import PUBLISH_SKIPPED_DISABLED, PUBLISH_SKIPPED_UNMAPPED_KIND
from pipeline.network.network_constants import RESULT_ACCEPTED
from pipeline.network.network_publisher import (
    PublicationReceipt,
    PublishConfigError,
    get_network_publisher,
)
from pipeline.search.passage_projection import _release_passages
from pipeline.temporal.activities.artifacts import _upload_artifact, _write_json_temp
from pipeline.temporal.activities.indexing import (
    _drop_superseded_points,
    _provider_id_from_doc,
    _release_target_for_job,
    ingest_to_vector_db,
)


@activity.defn
async def publish_catalog_to_network(
    workflow_id: str, transaction_id: str, release_network_id: str | None = None,
    require_matched_receipt: bool = False,
    publication_network_id: str | None = None,
    publication_revision_id: str | None = None,
) -> dict:
    """Publish through the configured network publisher and record the exchange.

    The catalog depends on the document's knowledge kind, read here rather than
    passed in as an activity argument - that keeps every workflow call site, and
    any workflow already in flight, untouched.
    """
    from pipeline import db

    if release_network_id:
        doc = db.get_document(workflow_id)
        if not doc or doc.get("is_disabled"):
            raise ApplicationError("The document is unavailable for release", non_retryable=True)
        job = db.get_document_job(doc["latest_job_id"])
        try:
            target = _release_target_for_job(doc, job, workflow_id, release_network_id)
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        if not target or target[0] != release_network_id:
            raise ApplicationError("The upload release network changed", non_retryable=True)
        index_state = db.get_document_index_status(workflow_id, target[1])
        if not index_state or index_state["status"] != "indexed" or not index_state["chunk_count_indexed"]:
            raise ApplicationError("The automatic release has no indexed content", non_retryable=True)

    repository = DocumentRepository()
    document_kind = (repository.get_document_kind(workflow_id) or "document").strip().lower()

    target_network_id = release_network_id or publication_network_id
    if publish_to_network_enabled():
        group = repository.get_document_group(workflow_id)
        try:
            if target_network_id:
                from pipeline.network.publication_delivery import deliver_publication

                publication_document = doc if release_network_id else db.get_document(workflow_id)
                if not publication_document:
                    raise PublishConfigError("The document is unavailable for publication")
                receipt = await asyncio.to_thread(
                    deliver_publication, target_network_id, transaction_id, publication_document["id"],
                    workflow_id, document_kind, group, publication_revision_id,
                    require_staged_revision=bool(release_network_id),
                )
            else:
                publisher = get_network_publisher()
                receipt = await asyncio.to_thread(
                    publisher.publish, transaction_id, document_kind, workflow_id, group
                )
        except PublishConfigError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        skip = None
        if receipt.exchange is None:
            skip = (PUBLISH_SKIPPED_UNMAPPED_KIND, f"No network catalog is mapped to document kind '{document_kind}'.")
    else:
        receipt = PublicationReceipt()
        skip = (PUBLISH_SKIPPED_DISABLED, "Publish to Network is disabled (ENABLE_PUBLISH_TO_NETWORK is not true).")

    if skip:
        artifact_type = "network_publish_skipped"
        filename = "network_publish_skipped.json"
        exchange = {
            "skipped": True,
            "document_kind": document_kind,
            "reason_code": skip[0],
            "reason": skip[1],
        }
    else:
        artifact_type = "network_publish_payload"
        filename = "network_publish.json"
        exchange = {**receipt.exchange, "result_status": receipt.result_status, "context_matches": receipt.context_matches}
        exchange.pop("failure_reason", None)
        if receipt.rejection:
            exchange["failure_reason"] = receipt.rejection
        elif release_network_id or require_matched_receipt:
            if not receipt.context_matches:
                exchange.update(result_status=None, failure_reason="Receipt context did not match the release")
            elif receipt.result_status != RESULT_ACCEPTED:
                exchange["failure_reason"] = "The network did not accept the release"

    # Recorded even when skipped: add_document_artifact requires a storage_uri,
    # and an empty one 404s the artifact-content endpoint if an operator clicks it.
    artifact_identity = {}
    if target_network_id:
        artifact_id = str(uuid5(NAMESPACE_URL, f"kp-publication:{target_network_id}:{transaction_id}"))
        artifact_identity = {"artifact_id": artifact_id}
        filename = f"{artifact_type}-{artifact_id}.json"
    exchange_path = _write_json_temp(exchange)
    try:
        exchange_uri, exchange_size, exchange_mime = _upload_artifact(
            exchange_path, workflow_id, artifact_type, filename
        )
    finally:
        if os.path.exists(exchange_path):
            os.remove(exchange_path)

    latest_job = db.get_latest_document_job(workflow_id)
    db.add_document_artifact(
        workflow_id=workflow_id,
        job_id=latest_job["id"] if latest_job else None,
        artifact_type=artifact_type,
        stage="publishing_to_network",
        storage_uri=exchange_uri,
        mime_type=exchange_mime,
        filename=filename,
        size_bytes=exchange_size,
        metadata=exchange,
        **artifact_identity,
    )

    if receipt.rejection:
        # Record the exchange before reporting the deterministic rejection.
        raise ApplicationError(receipt.rejection, non_retryable=True)
    if (release_network_id or require_matched_receipt) and (
        receipt.result_status != RESULT_ACCEPTED or not receipt.context_matches
    ):
        raise ApplicationError("The network did not accept the automatic release", non_retryable=True)
    if release_network_id:
        try:
            db.activate_network_release(doc["id"], workflow_id, release_network_id, transaction_id)
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        await _drop_superseded_points(
            index_key_for(target[1]), workflow_id, doc, index_state["doc_id"], target[1],
        )

    return {
        "status": "skipped" if skip else "published",
        "reason_code": skip[0] if skip else None,
        "document_kind": document_kind,
        "transaction_id": transaction_id,
        "message_id": receipt.message_id,
        "result_status": receipt.result_status,
    }


@activity.defn
async def release_saved_revision(
    document_id: str, revision_id: str, network_id: str, transaction_id: str, expected_generation: str | None,
) -> dict:
    """Restore the frozen passages before selecting this revision on its network."""
    from pipeline import db

    document = db.get_document(document_id)
    try:
        collection = managed_release_targets((document or {}).get("kind")).get(network_id)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    if not collection:
        raise ApplicationError("This deployment cannot release to the selected network", non_retryable=True)
    revision = db.get_revision_snapshot(document_id, revision_id)
    if not document or document["is_disabled"] or not document["network_visible"] or not revision:
        raise ApplicationError("The revision is unavailable for release", non_retryable=True)

    provider_id = _provider_id_from_doc(document, network_id)
    outcome = {"revision_id": revision_id, "network_id": network_id, "transaction_id": transaction_id}
    try:
        staged = db.stage_saved_network_revision(
            document_id, network_id, revision_id, transaction_id, expected_generation, collection, provider_id,
        )
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    if not staged:
        return outcome
    records = _release_passages(revision, network_id, provider_id)
    result = await ingest_to_vector_db(records, index_name=collection)
    if not records or result.get("records_ingested") != len(records):
        raise ApplicationError("The release index did not ingest every eligible passage")
    receipt = await publish_catalog_to_network(
        document["workflow_id"], transaction_id, require_matched_receipt=True, publication_network_id=network_id,
        publication_revision_id=revision_id,
    )
    if receipt["result_status"] != RESULT_ACCEPTED:
        raise ApplicationError("The network did not accept the selected revision", non_retryable=True)
    try:
        db.select_network_revision(document_id, network_id, revision_id, transaction_id, expected_generation)
    except ValueError as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc
    return outcome
