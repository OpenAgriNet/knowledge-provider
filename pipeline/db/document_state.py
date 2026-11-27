"""Mirror processing stages and reconcile persisted workflow state."""

from typing import Optional

from . import audit, common, connection, document_reads, jobs


def update_document_stage(
    workflow_id: str,
    stage: str,
    page_count: Optional[int] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None,
):
    """Update the stage (of the document's current workflow).

    ``page_count`` and ``chunk_count`` are accepted for call-site compatibility;
    both are counted from ``pages`` and ``chunks``, so only the audit entry keeps them.
    """
    old_stage = None
    audit_workflow_id = workflow_id
    with connection.get_connection() as conn:
        doc_uuid = document_reads._resolve_document_uuid(conn, workflow_id)
        if doc_uuid is None:
            return
        cw = conn.execute(
            "SELECT id, current_stage FROM document_current_workflow WHERE document_id = %s::uuid",
            (doc_uuid,),
        ).fetchone()
        if cw is None:
            return
        old_stage = cw["current_stage"]

        assignments = ["current_stage = %s"]
        params: list = [stage]
        if error_message is not None:
            assignments.append("error_message = %s")
            params.append(error_message)
        stamp_key = {
            "ocr_review": "ocr",
            "translation_review": "translation",
            "chunk_review": "chunks",
        }.get(stage)
        if stamp_key:
            assignments.append(
                "stage_timestamps = jsonb_build_object(%s::text, %s::text) || stage_timestamps"
            )
            params.extend([stamp_key, common._iso_now()])
        if stage == "completed":
            assignments.append("completed_at = now()")
        params.append(str(cw["id"]))
        conn.execute(f"UPDATE workflows SET {', '.join(assignments)} WHERE id = %s::uuid", params)

        conn.execute("UPDATE documents SET updated_at = now() WHERE id = %s::uuid", (doc_uuid,))
        audit_workflow_id = jobs.root_workflow_id(conn, doc_uuid) or workflow_id

    if old_stage and old_stage != stage:
        audit.log_audit(
            workflow_id=audit_workflow_id,
            document_id=audit_workflow_id,
            action_type="stage_change",
            field_name="stage",
            old_value=old_stage,
            new_value=stage,
            metadata={"page_count": page_count, "chunk_count": chunk_count},
        )


def reconcile_materialized_state(workflow_id: str) -> Optional[dict]:
    """
    Reconcile a document row against already-persisted page/chunk content.

    This is intentionally conservative: it only moves a document forward into
    review states when the corresponding materialized content already exists.
    """
    doc = document_reads.get_document(workflow_id)
    if not doc:
        return None
    workflow_id = doc["workflow_id"]

    with connection.get_connection() as conn:
        page_stats = conn.execute(
            """
            SELECT COUNT(*) AS page_rows, COALESCE(MAX(page_number), 0) AS max_page_number
            FROM pages WHERE document_id = %s::uuid
            """,
            (doc["id"],),
        ).fetchone()
        chunk_stats = conn.execute(
            "SELECT COUNT(*) AS chunk_rows FROM chunks WHERE document_id = %s::uuid",
            (doc["id"],),
        ).fetchone()

    page_count = int((page_stats["page_rows"] if page_stats else 0) or 0)
    max_page_number = int((page_stats["max_page_number"] if page_stats else 0) or 0)
    materialized_pages = max(page_count, max_page_number, int(doc.get("page_count") or 0))
    chunk_count = int((chunk_stats["chunk_rows"] if chunk_stats else 0) or 0)

    current_stage = doc.get("stage")
    latest_job = jobs.get_latest_document_job(workflow_id)
    updated = False
    now = common._iso_now()

    # Highest-confidence reconciliation first: if chunks exist, the document has
    # already reached chunk review materiality even if the workflow stalled.
    if chunk_count > 0 and current_stage not in {
        "chunk_review",
        "ready_for_ingestion",
        "ingesting",
        "approval_for_prod",
        "completed",
    }:
        update_document_stage(
            workflow_id=workflow_id,
            stage="chunk_review",
            page_count=materialized_pages,
            chunk_count=chunk_count,
            error_message=None,
        )
        if latest_job:
            jobs.update_document_job(
                latest_job["id"],
                current_stage="chunk_review",
                status="waiting_review",
                error_message=None,
            )
        doc = document_reads.get_document(workflow_id) or doc
        current_stage = doc.get("stage")
        updated = True

    # Lower-confidence but still safe: if OCR pages exist and the document is
    # somehow still marked as pre-review, move it into OCR review.
    elif materialized_pages > 0 and current_stage in {"registered", "ocr_processing"}:
        update_document_stage(
            workflow_id=workflow_id,
            stage="ocr_review",
            page_count=materialized_pages,
            chunk_count=int(doc.get("chunk_count") or 0),
            error_message=None,
        )
        if latest_job:
            jobs.update_document_job(
                latest_job["id"],
                current_stage="ocr_review",
                status="waiting_review",
                error_message=None,
            )
        updated = True

    # Even when stage is already correct, keep counts truthful.
    elif materialized_pages != int(doc.get("page_count") or 0) or chunk_count != int(
        doc.get("chunk_count") or 0
    ):
        update_document_stage(
            workflow_id=workflow_id,
            stage=current_stage,
            page_count=materialized_pages,
            chunk_count=chunk_count,
            error_message=doc.get("error_message"),
        )
        updated = True

    if not updated:
        return {
            "workflow_id": workflow_id,
            "updated": False,
            "stage": current_stage,
            "page_count": int(doc.get("page_count") or 0),
            "chunk_count": int(doc.get("chunk_count") or 0),
        }

    refreshed = document_reads.get_document(workflow_id) or doc
    refreshed_job = jobs.get_latest_document_job(workflow_id)
    return {
        "workflow_id": workflow_id,
        "updated": True,
        "stage": refreshed.get("stage"),
        "page_count": int(refreshed.get("page_count") or 0),
        "chunk_count": int(refreshed.get("chunk_count") or 0),
        "job_status": refreshed_job.get("status") if refreshed_job else None,
        "job_stage": refreshed_job.get("current_stage") if refreshed_job else None,
        "reconciled_at": now,
    }
