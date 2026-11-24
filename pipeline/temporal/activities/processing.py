"""Activities for chunking and translation."""

import json
import os
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from temporalio import activity

from pipeline.chunking import chunk_pages, load_chunking_config
from pipeline.network.release_policy import index_only_mode
from pipeline.temporal.activities.artifacts import _upload_artifact, _write_json_temp
from pipeline.translation import load_translation_config, translate_pages


async def _detect_and_translate_impl(
    pages: list[dict],
    target_language: str = "en",
    source_language: str | None = None,
) -> list[dict]:
    del source_language
    config = load_translation_config(target_language=target_language)
    return await translate_pages(
        pages,
        target_language=target_language,
        config=config,
        log=activity.logger.info,
    )


@activity.defn
async def create_chunks(
    pages: list[dict],
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
) -> list[dict]:
    """Create chunks from pages with page range tracking."""
    activity.logger.info(f"Creating chunks from {len(pages)} pages")
    config = load_chunking_config(chunk_size=chunk_size, chunk_overlap=chunk_overlap, min_tokens=min_tokens)
    result = await chunk_pages(pages, config)
    chunks = []
    for idx, chunk in enumerate(result.chunks, 1):
        chunks.append(
            {
                "chunk_number": idx,
                "original_text": chunk.text,
                "edited_text": None,
                "token_count": chunk.token_count,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "source_page_numbers_json": json.dumps(chunk.source_page_numbers),
                "source_spans_json": json.dumps(chunk.source_spans),
                "section_title": chunk.section_title,
                "content_type": chunk.content_type,
                "is_reference": chunk.is_reference,
                "chunking_provider": result.provider,
                "chunking_model": result.model,
                "chunking_config_json": result.config.to_json(),
                "chunking_run_id": "",
                "chunk_version": 1,
                "is_reviewed": False,
                "is_excluded": False,
                "reviewer_notes": None,
            }
        )
    activity.logger.info(f"Created {len(chunks)} chunks with page tracking")
    return chunks


@activity.defn
async def create_chunks_from_db(
    workflow_id: str,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    source_first: bool = False,
) -> dict:
    """Create chunks from persisted pages and persist chunks in Postgres."""
    from pipeline import db

    pages = db.get_pages(workflow_id)
    activity.logger.info(f"Creating chunks from DB pages for {workflow_id}: {len(pages)} pages")
    config = load_chunking_config(chunk_size=chunk_size, chunk_overlap=chunk_overlap, min_tokens=min_tokens)
    latest_job = db.get_latest_document_job(workflow_id)
    base_job_config = {}
    if latest_job and latest_job.get("config_json"):
        try:
            base_job_config = json.loads(latest_job["config_json"]) or {}
        except Exception:
            base_job_config = {}

    async def _persist_chunking_progress(event: dict) -> None:
        if not latest_job:
            return
        pages_total = int(event.get("pages_total") or len(pages) or 0)
        pages_processed = int(event.get("pages_processed") or 0)
        chunks_emitted = int(event.get("chunks_emitted") or 0)
        raw_percent = float(event.get("percent") or 0.0)
        percent = max(0.0, min(100.0, raw_percent))
        progress = {
            "status": "running" if percent < 100.0 else "completed",
            "provider": event.get("provider") or config.provider,
            "pages_processed": pages_processed,
            "pages_total": pages_total,
            "chunks_emitted": chunks_emitted,
            "percent": round(percent, 2),
            "updated_at": datetime.utcnow().isoformat(),
        }
        next_config = {**base_job_config, "chunking_progress": progress}
        db.update_document_job(latest_job["id"], config_json=next_config)

    await _persist_chunking_progress(
        {
            "provider": config.provider,
            "pages_processed": 0,
            "pages_total": len(pages),
            "chunks_emitted": 0,
            "percent": 0.0,
        }
    )

    chunking_pages = pages
    if source_first:
        chunking_pages = [
            {**page, "edited_translation": None, "translated_markdown": None, "translations": {}}
            for page in pages
        ]
    result = await chunk_pages(chunking_pages, config, progress_callback=_persist_chunking_progress)
    if (source_first or index_only_mode()) and not result.chunks and config.min_chunk_tokens > 1:
        if any((page.get("edited_markdown") or page.get("original_markdown") or "").strip() for page in chunking_pages):
            activity.logger.info("Retrying chunking without the size floor for a short source")
            result = await chunk_pages(
                chunking_pages, replace(config, min_chunk_tokens=1), progress_callback=_persist_chunking_progress,
            )
    chunking_run_id = f"chunk-{workflow_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    chunks = []
    for idx, chunk in enumerate(result.chunks, 1):
        chunks.append(
            {
                "chunk_number": idx,
                "original_text": chunk.text,
                "edited_text": None,
                "token_count": chunk.token_count,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "source_page_numbers_json": json.dumps(chunk.source_page_numbers),
                "source_spans_json": json.dumps(chunk.source_spans),
                "section_title": chunk.section_title,
                "content_type": chunk.content_type,
                "is_reference": chunk.is_reference,
                "chunking_provider": result.provider,
                "chunking_model": result.model,
                "chunking_config_json": result.config.to_json(),
                "chunking_run_id": chunking_run_id,
                "is_reviewed": False,
                "is_excluded": False,
                "reviewer_notes": None,
            }
        )
    db.save_chunks(workflow_id, chunks)
    chunks_json_path = _write_json_temp(chunks)
    try:
        chunks_uri, chunks_size, chunks_mime = _upload_artifact(
            chunks_json_path, workflow_id, "chunk_json_export", "chunks.json"
        )
    finally:
        if os.path.exists(chunks_json_path):
            os.remove(chunks_json_path)
    db.add_document_artifact(
        workflow_id=workflow_id,
        job_id=latest_job["id"] if latest_job else None,
        artifact_type="chunk_json_export",
        stage="chunk_review",
        storage_uri=chunks_uri,
        mime_type=chunks_mime,
        filename="chunks.json",
        size_bytes=chunks_size,
        metadata={
            "chunk_count": len(chunks),
            "chunking_provider": result.provider,
            "chunking_model": result.model,
            "chunking_run_id": chunking_run_id,
            "chunking_config": json.loads(result.config.to_json()),
            "warnings": result.warnings,
            "stats": result.stats,
        },
    )
    # Reconcile the document row immediately after chunk persistence so Postgres
    # remains truthful even if the workflow fails before its final state update.
    db.reconcile_materialized_state(workflow_id)
    await _persist_chunking_progress(
        {
            "provider": result.provider,
            "pages_processed": len(pages),
            "pages_total": len(pages),
            "chunks_emitted": len(chunks),
            "percent": 100.0,
        }
    )
    from pipeline.metadata.extraction import automatic_extraction_enabled

    if automatic_extraction_enabled():
        from pipeline.db.metadata_review import document_id_for_workflow, generate_review

        try:
            await generate_review(document_id_for_workflow(workflow_id), automatic=True, workflow_id=workflow_id)
        except Exception as exc:
            activity.logger.warning("Document metadata generation failed (%s)", type(exc).__name__)
    return {"chunk_count": len(chunks)}


@activity.defn
async def detect_and_translate_pages(
    pages: list[dict],
    target_language: str = "en",
    source_language: str = None,
) -> list[dict]:
    """Detect language and translate non-English pages."""
    return await _detect_and_translate_impl(pages, target_language=target_language, source_language=source_language)


@activity.defn
async def detect_and_translate_pages_from_db(
    workflow_id: str,
    target_language: str = "en",
    source_language: str = None,
) -> dict:
    """Detect and translate pages loaded from Postgres; persist updated pages back to Postgres."""
    from pipeline import db

    pages = db.get_pages(workflow_id)
    translated = await _detect_and_translate_impl(pages, target_language=target_language, source_language=source_language)
    db.save_pages(workflow_id, translated)
    translated_count = sum(1 for p in translated if p.get("translated_markdown"))
    latest_job = db.get_latest_document_job(workflow_id)
    translation_config = load_translation_config(target_language=target_language)
    translated_json_path = _write_json_temp(translated)
    try:
        translated_uri, translated_size, translated_mime = _upload_artifact(
            translated_json_path, workflow_id, "translation_pages_json", "translated_pages.json"
        )
    finally:
        if os.path.exists(translated_json_path):
            os.remove(translated_json_path)
    db.add_document_artifact(
        workflow_id=workflow_id,
        job_id=latest_job["id"] if latest_job else None,
        artifact_type="translation_pages_json",
        stage="translation_review",
        storage_uri=translated_uri,
        mime_type=translated_mime,
        filename="translated_pages.json",
        size_bytes=translated_size,
        metadata={
            "page_count": len(translated),
            "translated_count": translated_count,
            "translation_provider": translation_config.provider,
            "translation_model": translation_config.model,
            "translation_target_language": target_language,
            "translation_run_id": str(uuid4()),
        },
    )
    return {"page_count": len(translated), "translated_count": translated_count}
