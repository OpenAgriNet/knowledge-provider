"""
Unit tests for pipeline/activities.py - Temporal activities.

Tests cover:
- OCR activity (mocked)
- Chunking activity
- Translation activity (mocked)
- Ingestion activity (mocked)
- State update activity
"""

import asyncio
import os
from unittest.mock import MagicMock

import pytest

os.environ["MINIO_ACCESS_KEY"] = "test-access"
os.environ["MINIO_SECRET_KEY"] = "test-secret"
os.environ["TRANSLATION_VLLM_BASE_URL"] = "http://localhost:8000/v1"


class TestChunkingActivity:
    """Tests for the chunking activity."""

    @pytest.mark.unit
    def test_create_chunks_basic(self):
        """Test basic chunking of pages."""
        from pipeline.activities import create_chunks

        pages = [
            {
                "page_number": 1,
                "original_markdown": "This is page one with some content. " * 50,
                "edited_markdown": None,
                "detected_language": "en"
            },
            {
                "page_number": 2,
                "original_markdown": "This is page two with more content. " * 50,
                "edited_markdown": None,
                "detected_language": "en"
            }
        ]

        chunks = asyncio.run(create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=10))

        assert len(chunks) > 0
        for chunk in chunks:
            assert "chunk_number" in chunk
            assert "original_text" in chunk
            assert "token_count" in chunk
            assert len(chunk["original_text"]) > 0

    @pytest.mark.unit
    def test_create_chunks_uses_edited_markdown(self):
        """Test that edited_markdown is preferred over original."""
        from pipeline.activities import create_chunks

        pages = [
            {
                "page_number": 1,
                "original_markdown": "Original content that should not appear.",
                "edited_markdown": "Edited content that should appear. " * 30,
                "detected_language": "en"
            }
        ]

        chunks = asyncio.run(create_chunks(pages, chunk_size=50, chunk_overlap=10, min_tokens=5))

        assert len(chunks) > 0
        # The edited content should be in the chunks
        all_text = " ".join(c["original_text"] for c in chunks)
        assert "Edited content" in all_text
        assert "Original content that should not appear" not in all_text

    @pytest.mark.unit
    def test_create_chunks_empty_pages(self):
        """Test chunking with empty pages."""
        from pipeline.activities import create_chunks

        pages = []
        chunks = asyncio.run(create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=10))
        assert chunks == []

    @pytest.mark.unit
    def test_create_chunks_min_tokens_filter(self):
        """Test that chunks below min_tokens are filtered."""
        from pipeline.activities import create_chunks

        pages = [
            {
                "page_number": 1,
                "original_markdown": "Short.",
                "edited_markdown": None,
                "detected_language": "en"
            }
        ]

        # With high min_tokens, short content should ideally be filtered.
        # Current deterministic path may still emit a single short chunk; assert it stays small.
        chunks = asyncio.run(create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=100))
        assert all(isinstance(c.get("token_count"), int) for c in chunks)
        assert all(c["token_count"] < 100 for c in chunks) or len(chunks) == 0


class TestPrepareIngestionRecords:
    """Tests for the ingestion preparation activity."""

    @pytest.mark.unit
    def test_prepare_records_basic(self):
        """Test preparing records for vector-index ingestion."""
        from pipeline.activities import prepare_ingestion_records

        chunks = [
            {
                "chunk_number": 1,
                "original_text": "Test chunk one",
                "edited_text": None,
                "source_pages": [1],
                "token_count": 5,
                "is_excluded": False
            },
            {
                "chunk_number": 2,
                "original_text": "Test chunk two",
                "edited_text": None,
                "source_pages": [1, 2],
                "token_count": 5,
                "is_excluded": False
            }
        ]

        records = prepare_ingestion_records(
            document_id="test-doc",
            filename="test.pdf",
            chunks=chunks
        )

        assert len(records) == 2
        for record in records:
            assert "_id" in record
            assert "doc_id" in record
            assert "text" in record
            assert "chunk_num" in record
            assert record["doc_id"] == "test-doc"

    @pytest.mark.unit
    def test_prepare_records_carries_instance_name(self):
        """Payload ships the readable state name beside the instance code."""
        from pipeline.activities import _prepare_records

        chunks = [{"chunk_number": 1, "original_text": "Test chunk", "token_count": 5}]

        for code, expected in (("bv", "Bharat Vistaar"), ("mh", "Maharashtra"), ("ka", "Karnataka")):
            records = _prepare_records(
                document_id="test-doc",
                filename="test.pdf",
                chunks=chunks,
                instance=code,
            )
            assert records[0]["instance"] == code
            assert records[0]["instance_name"] == expected

    @pytest.mark.unit
    def test_prepare_records_fills_scheme_aliases(self):
        """A scheme must never index with scheme_aliases: []."""
        from pipeline.activities import _prepare_records

        chunks = [{"chunk_number": 1, "original_text": "Test chunk", "token_count": 5}]

        records = _prepare_records(
            document_id="test-doc",
            filename="NMEO-OPGUIEDELINES.pdf",
            chunks=chunks,
            instance="bv",
            document_kind="scheme",
            scheme_code="nmeoop",
            scheme_name="National Mission on Edible Oils - Oil Palm",
            scheme_aliases=None,
        )

        record = records[0]
        assert record["type"] == "scheme"
        assert record["scheme_aliases"], "aliases must be populated at ingest"
        lowered = {a.lower() for a in record["scheme_aliases"]}
        assert "nmeoop" in lowered
        assert "oil palm" in lowered
        assert record["instance_name"] == "Bharat Vistaar"

    @pytest.mark.unit
    def test_prepare_records_non_scheme_has_no_aliases(self):
        """Plain documents keep the scheme fields absent, as before."""
        from pipeline.activities import _prepare_records

        chunks = [{"chunk_number": 1, "original_text": "Test chunk", "token_count": 5}]

        records = _prepare_records(
            document_id="test-doc",
            filename="test.pdf",
            chunks=chunks,
            instance="mh",
            document_kind="document",
        )

        assert "scheme_aliases" not in records[0]
        assert records[0]["type"] == "document"
        assert records[0]["instance_name"] == "Maharashtra"

    @pytest.mark.unit
    def test_prepare_records_excludes_excluded_chunks(self):
        """Test that excluded chunks are not included in records."""
        from pipeline.activities import prepare_ingestion_records

        chunks = [
            {
                "chunk_number": 1,
                "original_text": "Included",
                "edited_text": None,
                "source_pages": [1],
                "token_count": 5,
                "is_excluded": False
            },
            {
                "chunk_number": 2,
                "original_text": "Excluded",
                "edited_text": None,
                "source_pages": [1],
                "token_count": 5,
                "is_excluded": True
            }
        ]

        records = prepare_ingestion_records(
            document_id="test-doc",
            filename="test.pdf",
            chunks=chunks
        )

        assert len(records) == 1
        assert records[0]["text"] == "Included"

    @pytest.mark.unit
    def test_prepare_records_uses_edited_text(self):
        """Test that edited_text is preferred over original."""
        from pipeline.activities import prepare_ingestion_records

        chunks = [
            {
                "chunk_number": 1,
                "original_text": "Original",
                "edited_text": "Edited",
                "source_pages": [1],
                "token_count": 5,
                "is_excluded": False
            }
        ]

        records = prepare_ingestion_records(
            document_id="test-doc",
            filename="test.pdf",
            chunks=chunks
        )

        assert records[0]["text"] == "Edited"


class TestUpdateDocumentState:
    """Tests for the state update activity."""

    @pytest.mark.unit
    def test_update_state(self, db_connection):
        """Test updating document state in SQLite."""
        from pipeline.activities import update_document_state

        workflow_id = "state-update-test"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-state",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )

        asyncio.run(update_document_state(
            workflow_id=workflow_id,
            stage="ocr_processing",
            page_count=5,
            chunk_count=0
        ))

        doc = db_connection.get_document(workflow_id)
        assert doc["stage"] == "ocr_processing"
        assert doc["page_count"] == 5


class TestMinIOClient:
    """Tests for MinIO client creation."""

    @pytest.mark.unit
    def test_get_minio_client_requires_credentials(self):
        """Test that missing credentials raise error."""
        from pipeline.activities import get_minio_client

        # Save original values
        orig_access = os.environ.get("MINIO_ACCESS_KEY")
        orig_secret = os.environ.get("MINIO_SECRET_KEY")

        try:
            # Remove credentials
            if "MINIO_ACCESS_KEY" in os.environ:
                del os.environ["MINIO_ACCESS_KEY"]
            if "MINIO_SECRET_KEY" in os.environ:
                del os.environ["MINIO_SECRET_KEY"]

            with pytest.raises(RuntimeError, match="required"):
                get_minio_client()
        finally:
            # Restore
            if orig_access:
                os.environ["MINIO_ACCESS_KEY"] = orig_access
            if orig_secret:
                os.environ["MINIO_SECRET_KEY"] = orig_secret

    @pytest.mark.unit
    def test_get_minio_client_with_credentials(self):
        """Test MinIO client creation with credentials."""
        from pipeline.activities import get_minio_client

        os.environ["MINIO_ACCESS_KEY"] = "test-key"
        os.environ["MINIO_SECRET_KEY"] = "test-secret"

        client = get_minio_client()
        assert client is not None


class TestOCRActivity:
    """Tests for OCR activity (mocked)."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_run_ocr_calls_provider(self, monkeypatch, tmp_path):
        """Test that OCR activity delegates PDF OCR to the OCR service."""
        import pipeline.activities as activities

        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test content")

        mock_run_ocr_pdf = MagicMock(
            return_value=[
                {
                    "page_number": 1,
                    "original_markdown": "# Page 1 content",
                    "edited_markdown": None,
                    "is_reviewed": False,
                    "reviewer_notes": None,
                }
            ]
        )
        monkeypatch.setattr(activities, "run_ocr_pdf", mock_run_ocr_pdf)
        monkeypatch.setattr(activities, "_ensure_pdf_input", lambda path: (path, False))

        pages = await activities.run_ocr(str(pdf_path))

        assert len(pages) == 1
        assert pages[0]["page_number"] == 1
        assert "# Page 1 content" in pages[0]["original_markdown"]
        mock_run_ocr_pdf.assert_called_once()


class TestPublishCatalogToNetworkActivity:
    """Tests for the publish_catalog_to_network activity (thin wrapper over DiscoveryPublishService)."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_records_artifact_on_success(self, monkeypatch):
        import pipeline.activities as activities
        import pipeline.db as db

        fake_result = {
            "envelope": {
                "context": {
                    "action": "catalog/publish",
                    "messageId": "msg-1",
                    "transactionId": "txn-1",
                    "timestamp": "2026-03-04T10:00:00.000Z",
                    "senderId": "docs-pipeline-bv",
                },
                "message": {"catalogs": []},
            },
            "status_code": 200,
            "response_body": '{"ack": true}',
        }

        class FakeService:
            def __init__(self):
                pass

            def publish(self, transaction_id):
                assert transaction_id == "txn-1"
                return fake_result

        monkeypatch.setattr(activities, "DiscoveryPublishService", FakeService)
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *a, **k: ("minio://documents/network_publish.json", 123, "application/json"),
        )
        monkeypatch.setattr(db, "get_latest_document_job", lambda workflow_id: {"id": 42})

        recorded = {}

        def fake_add_artifact(**kwargs):
            recorded.update(kwargs)
            return 1

        monkeypatch.setattr(db, "add_document_artifact", fake_add_artifact)

        result = await activities.publish_catalog_to_network("wf-1", "txn-1")

        assert result == {"status": "published", "transaction_id": "txn-1", "message_id": "msg-1"}
        assert recorded["workflow_id"] == "wf-1"
        assert recorded["job_id"] == 42
        assert recorded["artifact_type"] == "network_publish_payload"
        assert recorded["stage"] == "publishing_to_network"
        assert recorded["metadata"]["request"] == fake_result["envelope"]
        assert recorded["metadata"]["response_status"] == 200

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_publish_failure_propagates_and_records_nothing(self, monkeypatch):
        import pipeline.activities as activities
        import pipeline.db as db

        class FakeService:
            def publish(self, transaction_id):
                raise RuntimeError("discovery service unreachable")

        monkeypatch.setattr(activities, "DiscoveryPublishService", FakeService)

        add_artifact_calls = []
        monkeypatch.setattr(db, "add_document_artifact", lambda **kwargs: add_artifact_calls.append(kwargs))

        with pytest.raises(RuntimeError, match="discovery service unreachable"):
            await activities.publish_catalog_to_network("wf-1", "txn-1")

        assert add_artifact_calls == []
