"""Run one processing stage independently."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from pipeline.documents.models import DocumentStage
    from pipeline.temporal.activities.processing import (
        create_chunks_from_db,
        detect_and_translate_pages_from_db,
    )
    from pipeline.temporal.activities.source import run_ocr_and_store
    from pipeline.temporal.activities.state import auto_tag_chunks_from_db

from .policies import (
    CHUNK_RETRY,
    OCR_RETRY,
    TRANSLATION_RETRY,
    _mirror_state,
    _now_iso,
)


@dataclass
class TranslationOnlyWorkflowState:
    workflow_id: str
    document_id: str
    filename: str
    stage: DocumentStage = DocumentStage.OCR_REVIEW
    error_message: Optional[str] = None
    page_count: int = 0
    translated_count: int = 0
    translation_completed_at: Optional[str] = None


@workflow.defn
class TranslationOnlyWorkflow:
    """Resume from OCR review, run translation only, and stop at translation review."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        original_workflow_id: str,
        document_id: str,
        filename: str,
    ) -> dict:
        self.state = TranslationOnlyWorkflowState(
            workflow_id=original_workflow_id,
            document_id=document_id,
            filename=filename,
        )

        try:
            await _mirror_state(original_workflow_id, "translation_processing", 0, 0, None)
            self.state.stage = DocumentStage.TRANSLATION_PROCESSING

            translation_result = await workflow.execute_activity(
                detect_and_translate_pages_from_db,
                args=[original_workflow_id],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=TRANSLATION_RETRY,
            )
            self.state.page_count = translation_result.get("page_count", 0)
            self.state.translated_count = translation_result.get("translated_count", 0)
            self.state.translation_completed_at = _now_iso()
            self.state.stage = DocumentStage.TRANSLATION_REVIEW

            await _mirror_state(original_workflow_id, "translation_review", self.state.page_count, 0, None)

            return {
                "workflow_id": original_workflow_id,
                "document_id": document_id,
                "filename": filename,
                "stage": "translation_review",
                "page_count": self.state.page_count,
                "translated_count": self.state.translated_count,
            }
        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", self.state.page_count, 0, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "workflow_id": self.state.workflow_id,
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": self.state.page_count,
            "translated_count": self.state.translated_count,
            "translation_completed_at": self.state.translation_completed_at,
        }


@dataclass
class OcrOnlyWorkflowState:
    workflow_id: str
    document_id: str
    filename: str
    stage: DocumentStage = DocumentStage.REGISTERED
    error_message: Optional[str] = None
    page_count: int = 0
    ocr_completed_at: Optional[str] = None


@workflow.defn
class OcrOnlyWorkflow:
    """Resume or retry OCR for an existing document and stop at OCR review."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        original_workflow_id: str,
        document_id: str,
        filename: str,
        filepath: str,
    ) -> dict:
        self.state = OcrOnlyWorkflowState(
            workflow_id=original_workflow_id,
            document_id=document_id,
            filename=filename,
        )

        try:
            await _mirror_state(original_workflow_id, "ocr_processing", 0, 0, None)
            self.state.stage = DocumentStage.OCR_PROCESSING

            ocr_result = await workflow.execute_activity(
                run_ocr_and_store,
                args=[original_workflow_id, filepath],
                start_to_close_timeout=timedelta(minutes=90),
                heartbeat_timeout=timedelta(minutes=10),
                retry_policy=OCR_RETRY,
            )
            self.state.page_count = ocr_result.get("page_count", 0)
            self.state.ocr_completed_at = _now_iso()
            self.state.stage = DocumentStage.OCR_REVIEW

            await _mirror_state(original_workflow_id, "ocr_review", self.state.page_count, 0, None)

            return {
                "workflow_id": original_workflow_id,
                "document_id": document_id,
                "filename": filename,
                "stage": "ocr_review",
                "page_count": self.state.page_count,
            }
        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", self.state.page_count, 0, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "workflow_id": self.state.workflow_id,
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": self.state.page_count,
            "ocr_completed_at": self.state.ocr_completed_at,
        }


@dataclass
class ChunkingOnlyWorkflowState:
    workflow_id: str
    document_id: str
    filename: str
    stage: DocumentStage = DocumentStage.TRANSLATION_REVIEW
    error_message: Optional[str] = None
    page_count: int = 0
    chunk_count: int = 0
    chunks_completed_at: Optional[str] = None


@workflow.defn
class ChunkingOnlyWorkflow:
    """Resume or retry chunking for an existing document and stop at chunk review."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        original_workflow_id: str,
        document_id: str,
        filename: str,
        page_count: int = 0,
        chunk_size: int = 450,
        chunk_overlap: int = 128,
        min_tokens: int = 100,
    ) -> dict:
        self.state = ChunkingOnlyWorkflowState(
            workflow_id=original_workflow_id,
            document_id=document_id,
            filename=filename,
            page_count=page_count,
        )

        try:
            await _mirror_state(original_workflow_id, "chunking", page_count, 0, None)
            self.state.stage = DocumentStage.CHUNKING

            chunk_result = await workflow.execute_activity(
                create_chunks_from_db,
                args=[original_workflow_id, chunk_size, chunk_overlap, min_tokens],
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=CHUNK_RETRY,
            )
            self.state.chunk_count = chunk_result.get("chunk_count", 0)
            self.state.chunks_completed_at = _now_iso()

            if workflow.patched("auto-tag-v1"):
                await workflow.execute_activity(
                    auto_tag_chunks_from_db,
                    args=[original_workflow_id, filename],
                    start_to_close_timeout=timedelta(minutes=45),
                    retry_policy=CHUNK_RETRY,
                )

            self.state.stage = DocumentStage.CHUNK_REVIEW

            await _mirror_state(original_workflow_id, "chunk_review", page_count, self.state.chunk_count, None)

            return {
                "workflow_id": original_workflow_id,
                "document_id": document_id,
                "filename": filename,
                "stage": "chunk_review",
                "chunk_count": self.state.chunk_count,
            }
        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", page_count, self.state.chunk_count, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "workflow_id": self.state.workflow_id,
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": self.state.page_count,
            "chunk_count": self.state.chunk_count,
            "chunks_completed_at": self.state.chunks_completed_at,
        }
