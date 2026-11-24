"""Activities for persisting document and workflow state."""

from datetime import datetime

from temporalio import activity


@activity.defn
async def update_document_state(
    workflow_id: str,
    stage: str,
    page_count: int = 0,
    chunk_count: int = 0,
    error_message: str = None,
) -> dict:
    """Update document state in Postgres."""
    from pipeline import db

    activity.logger.info(f"Updating state for {workflow_id}: stage={stage}")
    db.update_document_stage(
        workflow_id=workflow_id,
        stage=stage,
        page_count=page_count,
        chunk_count=chunk_count,
        error_message=error_message,
    )
    latest_job = db.get_latest_document_job(workflow_id)
    if latest_job:
        job_updates = {"current_stage": stage}
        if stage in {
            "ocr_review",
            "translation_review",
            "chunk_review",
            "ready_for_ingestion",
            "approval_for_prod",
        }:
            job_updates["status"] = "waiting_review"
        elif stage == "completed":
            job_updates["status"] = "completed"
            job_updates["completed_at"] = datetime.utcnow().isoformat()
        elif stage == "failed":
            job_updates["status"] = "failed"
            job_updates["completed_at"] = datetime.utcnow().isoformat()
            job_updates["error_message"] = error_message
        else:
            job_updates["status"] = "running"
        db.update_document_job(latest_job["id"], **job_updates)
    return {"updated": True, "stage": stage}


@activity.defn
async def persist_document_content(workflow_id: str, pages: list[dict], chunks: list[dict]) -> dict:
    """Persist pages and chunks to Postgres."""
    from pipeline import db

    activity.logger.info(f"Persisting content for {workflow_id}: {len(pages)} pages, {len(chunks)} chunks")
    db.save_pages(workflow_id, pages)
    db.save_chunks(workflow_id, chunks)
    return {"persisted": True, "pages": len(pages), "chunks": len(chunks)}


@activity.defn
async def auto_tag_chunks_from_db(workflow_id: str, filename: str = "") -> dict:
    """No-op retained so existing workflow histories that scheduled this
    activity before the auto-tag-v1 patch continue to replay deterministically.
    """
    return {"tagged_chunks": 0, "skipped": True}
