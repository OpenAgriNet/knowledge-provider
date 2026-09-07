"""Unit tests for DocumentRepository.

Two styles on purpose: the `db_connection` fixture exercises the repository
against real SQLite (so a drifted db.py signature fails here), and the fake-db
tests pin the behaviour that is the repository's own - the None guards and the
job-id lookup it does on the caller's behalf.
"""

import pytest

from pipeline.document_repository import DocumentRepository


class FakeDb:
    """Records calls and returns canned rows, standing in for pipeline.db."""

    def __init__(self, document=None, latest_job=None):
        self._document = document
        self._latest_job = latest_job
        self.artifact_calls = []

    def get_document(self, workflow_id):
        self.get_document_called_with = workflow_id
        return self._document

    def get_latest_document_job(self, workflow_id):
        return self._latest_job

    def add_document_artifact(self, **kwargs):
        self.artifact_calls.append(kwargs)
        return 7


class TestGetDocumentKind:
    @pytest.mark.unit
    def test_returns_the_stored_kind(self):
        repo = DocumentRepository(FakeDb(document={"document_kind": "advisory"}))

        assert repo.get_document_kind("wf-1") == "advisory"

    @pytest.mark.unit
    def test_returns_none_for_a_missing_document(self):
        repo = DocumentRepository(FakeDb(document=None))

        assert repo.get_document_kind("wf-nope") is None

    @pytest.mark.unit
    def test_returns_none_when_the_column_is_null(self):
        # Old rows predate the column's default, so it can be NULL.
        repo = DocumentRepository(FakeDb(document={"document_kind": None}))

        assert repo.get_document_kind("wf-1") is None

    @pytest.mark.unit
    def test_does_not_normalise_the_value(self):
        # Normalisation belongs to the caller that knows what the kind means.
        repo = DocumentRepository(FakeDb(document={"document_kind": "  Advisory "}))

        assert repo.get_document_kind("wf-1") == "  Advisory "

    @pytest.mark.unit
    def test_passes_the_workflow_id_through(self):
        fake = FakeDb(document={"document_kind": "scheme"})

        DocumentRepository(fake).get_document_kind("wf-42")

        assert fake.get_document_called_with == "wf-42"


class TestGetLatestJobId:
    @pytest.mark.unit
    def test_returns_the_job_id(self):
        repo = DocumentRepository(FakeDb(latest_job={"id": 42}))

        assert repo.get_latest_job_id("wf-1") == 42

    @pytest.mark.unit
    def test_returns_none_when_the_document_has_no_jobs(self):
        repo = DocumentRepository(FakeDb(latest_job=None))

        assert repo.get_latest_job_id("wf-1") is None


class TestRecordArtifact:
    @pytest.mark.unit
    def test_attaches_the_artifact_to_the_latest_job(self):
        fake = FakeDb(latest_job={"id": 99})

        artifact_id = DocumentRepository(fake).record_artifact(
            workflow_id="wf-1",
            artifact_type="network_publish_payload",
            stage="publishing_to_network",
            storage_uri="minio://documents/x.json",
            mime_type="application/json",
            filename="x.json",
            size_bytes=12,
            metadata={"request": {}},
        )

        assert artifact_id == 7
        call = fake.artifact_calls[0]
        assert call["job_id"] == 99
        assert call["workflow_id"] == "wf-1"
        assert call["artifact_type"] == "network_publish_payload"
        assert call["stage"] == "publishing_to_network"
        assert call["storage_uri"] == "minio://documents/x.json"
        assert call["metadata"] == {"request": {}}

    @pytest.mark.unit
    def test_records_with_a_null_job_when_there_is_none(self):
        fake = FakeDb(latest_job=None)

        DocumentRepository(fake).record_artifact(
            workflow_id="wf-1",
            artifact_type="network_publish_skipped",
            stage="publishing_to_network",
            storage_uri="minio://documents/y.json",
        )

        assert fake.artifact_calls[0]["job_id"] is None

    @pytest.mark.unit
    def test_optional_fields_default_to_none(self):
        fake = FakeDb(latest_job={"id": 1})

        DocumentRepository(fake).record_artifact(
            workflow_id="wf-1",
            artifact_type="t",
            stage="s",
            storage_uri="minio://documents/z.json",
        )

        call = fake.artifact_calls[0]
        assert call["mime_type"] is None
        assert call["filename"] is None
        assert call["size_bytes"] is None
        assert call["metadata"] is None


class TestAgainstRealSqlite:
    """Guards against db.py drifting out from under the repository."""

    @pytest.mark.unit
    @pytest.mark.db
    def test_reads_a_kind_written_through_the_db_layer(self, db_connection, sample_document):
        workflow_id = sample_document["workflow_id"]
        db_connection.update_document_fields(workflow_id, document_kind="advisory")

        assert DocumentRepository().get_document_kind(workflow_id) == "advisory"

    @pytest.mark.unit
    @pytest.mark.db
    def test_unclassified_document_reads_as_the_column_default(self, db_connection, sample_document):
        # A fresh upload is 'document' - which is why the publish path has to
        # treat "no kind chosen yet" as a real case.
        assert DocumentRepository().get_document_kind(sample_document["workflow_id"]) == "document"

    @pytest.mark.unit
    @pytest.mark.db
    def test_missing_document_reads_as_none(self, db_connection):
        assert DocumentRepository().get_document_kind("no-such-workflow") is None

    @pytest.mark.unit
    @pytest.mark.db
    def test_records_an_artifact_that_the_db_layer_can_read_back(self, db_connection, sample_document):
        workflow_id = sample_document["workflow_id"]

        artifact_id = DocumentRepository().record_artifact(
            workflow_id=workflow_id,
            artifact_type="network_publish_payload",
            stage="publishing_to_network",
            storage_uri="minio://documents/network_publish.json",
            mime_type="application/json",
            filename="network_publish.json",
            size_bytes=34,
            metadata={"verdict": {"status": "ACCEPTED"}},
        )

        stored = db_connection.get_document_artifact(workflow_id, artifact_id)
        assert stored["artifact_type"] == "network_publish_payload"
        assert stored["stage"] == "publishing_to_network"
        assert stored["filename"] == "network_publish.json"

    @pytest.mark.unit
    @pytest.mark.db
    def test_document_with_no_jobs_has_no_latest_job_id(self, db_connection, sample_document):
        assert DocumentRepository().get_latest_job_id(sample_document["workflow_id"]) is None
