import pytest

CHUNK = {"chunk_number": 1, "original_text": "Cotton pest advice", "token_count": 3, "is_excluded": False}


class TestNormalize:
    @pytest.mark.unit
    def test_trims_surrounding_whitespace(self):
        from pipeline.display_name import normalize

        assert normalize("  PM Kisan Guidelines  ") == "PM Kisan Guidelines"

    @pytest.mark.unit
    def test_none_means_not_given(self):
        from pipeline.display_name import normalize

        assert normalize(None) is None

    @pytest.mark.unit
    @pytest.mark.parametrize("value", ["", "   "])
    def test_rejects_blank(self, value):
        from pipeline.display_name import normalize

        with pytest.raises(ValueError, match="cannot be blank"):
            normalize(value)

    @pytest.mark.unit
    def test_accepts_the_maximum_length(self):
        from pipeline.display_name import MAX_LENGTH, normalize

        assert normalize("a" * MAX_LENGTH) == "a" * MAX_LENGTH

    @pytest.mark.unit
    def test_rejects_over_the_maximum_length(self):
        from pipeline.display_name import MAX_LENGTH, normalize

        with pytest.raises(ValueError, match="150"):
            normalize("a" * (MAX_LENGTH + 1))


class TestEditable:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stage",
        ["registered", "ocr_review", "translation_review", "chunk_review", "ready_for_ingestion", "failed"],
    )
    def test_editable_before_dev_ingest(self, stage):
        from pipeline.display_name import ensure_editable

        ensure_editable({"stage": stage, "ingested_at": None})

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stage",
        ["ingesting", "approval_for_prod", "ingesting_prod", "publishing_to_network", "completed"],
    )
    def test_locked_once_dev_ingest_starts(self, stage):
        from pipeline.display_name import DisplayNameLocked, ensure_editable

        with pytest.raises(DisplayNameLocked):
            ensure_editable({"stage": stage, "ingested_at": None})

    @pytest.mark.unit
    def test_locked_when_ever_ingested(self):
        from pipeline.display_name import DisplayNameLocked, ensure_editable

        with pytest.raises(DisplayNameLocked):
            ensure_editable({"stage": "failed", "ingested_at": "2026-11-01T10:00:00"})


class TestNameOnRecords:
    @pytest.mark.unit
    def test_display_name_becomes_name_en(self):
        from pipeline.activities import _prepare_records

        records = _prepare_records("doc-1", "scan_0042.pdf", [CHUNK], display_name="PM Kisan Guidelines")

        assert records[0]["name_en"] == "PM Kisan Guidelines"

    @pytest.mark.unit
    def test_display_name_beats_the_manifest_title(self, monkeypatch):
        import pipeline.activities as activities

        monkeypatch.setattr(activities, "_get_doc_metadata", lambda filename: {"title_en": "Manifest Title"})

        records = activities._prepare_records("doc-1", "scan.pdf", [CHUNK], display_name="Reviewer Name")

        assert records[0]["name_en"] == "Reviewer Name"

    @pytest.mark.unit
    def test_manifest_title_used_when_no_display_name(self, monkeypatch):
        import pipeline.activities as activities

        monkeypatch.setattr(activities, "_get_doc_metadata", lambda filename: {"title_en": "Manifest Title"})

        records = activities._prepare_records("doc-1", "scan.pdf", [CHUNK], display_name=None)

        assert records[0]["name_en"] == "Manifest Title"

    @pytest.mark.unit
    def test_filename_stem_used_when_nothing_else(self, monkeypatch):
        import pipeline.activities as activities

        monkeypatch.setattr(activities, "_get_doc_metadata", lambda filename: {})

        records = activities._prepare_records("doc-1", "scan_0042.pdf", [CHUNK])

        assert records[0]["name_en"] == "scan_0042"


class _RecordsBuilt(Exception):
    pass


class TestIngestPathsCarryTheDisplayName:
    @pytest.fixture
    def captured(self, monkeypatch):
        import pipeline.activities as activities
        import pipeline.db as db

        seen = {}

        def capture(*args, **kwargs):
            seen.update(kwargs)
            raise _RecordsBuilt

        monkeypatch.setattr(activities, "_prepare_records", capture)
        monkeypatch.setattr(db, "get_chunks", lambda *a, **k: [CHUNK])
        monkeypatch.setattr(
            db, "get_document", lambda workflow_id: {"name": "PM Kisan Guidelines", "group": "mh"}
        )
        return seen

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_dev_ingest(self, captured):
        from pipeline.activities import ingest_document_from_db

        with pytest.raises(_RecordsBuilt):
            await ingest_document_from_db("wf-1", "doc-1", "doc.pdf")

        assert captured["display_name"] == "PM Kisan Guidelines"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_prod_promotion(self, captured, monkeypatch):
        from unittest.mock import MagicMock

        import pipeline.activities as activities

        monkeypatch.setenv("PROD_VECTOR_DB_URL", "http://localhost:6333")
        monkeypatch.setattr(activities, "get_qdrant_client", lambda *a, **k: MagicMock(), raising=False)

        with pytest.raises(_RecordsBuilt):
            await activities.promote_document_to_prod_qdrant("wf-1", "doc-1", "doc.pdf")

        assert captured["display_name"] == "PM Kisan Guidelines"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_prepare_for_ingestion(self, captured):
        from pipeline.activities import prepare_for_ingestion

        with pytest.raises(_RecordsBuilt):
            await prepare_for_ingestion("doc-1", "doc.pdf", [CHUNK], workflow_id="wf-1")

        assert captured["display_name"] == "PM Kisan Guidelines"


class TestUploadEndpoint:
    @pytest.mark.api
    @pytest.mark.unit
    def test_stores_the_trimmed_display_name(self, test_client, sample_pdf_content):
        response = test_client.post(
            "/upload",
            files={"file": ("scan_0042.pdf", sample_pdf_content, "application/pdf")},
            data={"name": "  PM Kisan Guidelines "},
        )

        assert response.status_code == 200
        doc = test_client.get(f"/documents/{response.json()['workflow_id']}").json()
        assert doc["display_name"] == "PM Kisan Guidelines"

    @pytest.mark.api
    @pytest.mark.unit
    def test_omitting_it_stores_nothing(self, test_client, sample_pdf_content):
        response = test_client.post(
            "/upload",
            files={"file": ("no-name.pdf", sample_pdf_content + b" ", "application/pdf")},
        )

        assert response.status_code == 200
        doc = test_client.get(f"/documents/{response.json()['workflow_id']}").json()
        assert doc["display_name"] is None

    @pytest.mark.api
    @pytest.mark.unit
    @pytest.mark.parametrize("value", ["   ", "a" * 151])
    def test_rejects_an_invalid_display_name(self, test_client, sample_pdf_content, value):
        response = test_client.post(
            "/upload",
            files={"file": ("bad-name.pdf", sample_pdf_content, "application/pdf")},
            data={"name": value},
        )

        assert response.status_code == 400


class TestMetadataEndpoint:
    def _document(self, db_connection, workflow_id, stage="ready_for_ingestion"):
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=f"doc-{workflow_id}",
            filename="scan_0042.pdf",
            filepath="/app/books/scan_0042.pdf",
            stage=stage,
        )

    @pytest.mark.api
    @pytest.mark.unit
    def test_sets_the_display_name_before_ingest(self, test_client, db_connection):
        self._document(db_connection, "display-001")

        response = test_client.patch(
            "/documents/display-001/scheme-metadata",
            json={"name": " Cotton Pest Advisory "},
        )

        assert response.status_code == 200
        assert response.json()["display_name"] == "Cotton Pest Advisory"
        assert db_connection.get_document("display-001")["display_name"] == "Cotton Pest Advisory"

    @pytest.mark.api
    @pytest.mark.unit
    def test_rejects_a_blank_display_name(self, test_client, db_connection):
        self._document(db_connection, "display-002")

        response = test_client.patch("/documents/display-002/scheme-metadata", json={"name": " "})

        assert response.status_code == 400

    @pytest.mark.api
    @pytest.mark.unit
    def test_refuses_once_ingest_has_started(self, test_client, db_connection):
        self._document(db_connection, "display-003", stage="approval_for_prod")

        response = test_client.patch(
            "/documents/display-003/scheme-metadata",
            json={"name": "Too Late"},
        )

        assert response.status_code == 409
        assert db_connection.get_document("display-003")["display_name"] is None

    @pytest.mark.api
    @pytest.mark.unit
    def test_other_metadata_still_editable_after_ingest(self, test_client, db_connection):
        self._document(db_connection, "display-004", stage="completed")

        response = test_client.patch(
            "/documents/display-004/scheme-metadata",
            json={"kind": "advisory"},
        )

        assert response.status_code == 200

    @pytest.mark.api
    @pytest.mark.unit
    def test_resending_the_unchanged_name_on_a_locked_document_is_accepted(self, test_client, db_connection):
        self._document(db_connection, "display-005", stage="ready_for_ingestion")
        db_connection.update_document_fields("display-005", display_name="Cotton Pest Advisory", ingested_at="2026-11-01T10:00:00")

        response = test_client.patch(
            "/documents/display-005/scheme-metadata",
            json={"kind": "advisory", "name": " Cotton Pest Advisory "},
        )

        assert response.status_code == 200
        assert db_connection.get_document("display-005")["document_kind"] == "advisory"

    @pytest.mark.api
    @pytest.mark.unit
    def test_a_locked_document_reports_the_lock_before_validation(self, test_client, db_connection):
        self._document(db_connection, "display-006", stage="completed")

        response = test_client.patch("/documents/display-006/scheme-metadata", json={"name": " "})

        assert response.status_code == 409
