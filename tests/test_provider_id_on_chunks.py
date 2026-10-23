from unittest.mock import MagicMock, patch

import pytest
from temporalio.exceptions import ApplicationError

NAMING_ENV = (
    "PORTAL_INSTANCE",
    "DEFAULT_INSTANCE",
    "NETWORK_PROVIDER_ID_TEMPLATE",
    "NETWORK_GROUP_PROVIDER_NAME_TEMPLATE",
    "NETWORK_ADVISORY_PROVIDER_NAME",
    "NETWORK_SCHEMES_PROVIDER_NAME",
    "PROVIDER_CONFIG_OVERRIDE_PATH",
)

CHUNK = {"chunk_number": 1, "original_text": "Cotton pest advice", "token_count": 3, "is_excluded": False}


@pytest.fixture(autouse=True)
def _default_naming(monkeypatch):
    for name in NAMING_ENV:
        monkeypatch.delenv(name, raising=False)


def _use_overrides(monkeypatch, tmp_path, content):
    path = tmp_path / "overrides.json"
    path.write_text(content)
    monkeypatch.setenv("PROVIDER_CONFIG_OVERRIDE_PATH", str(path))


class TestProviderIdFromDoc:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "kind,instance,expected",
        [
            ("advisory", "mh", "advisory.mh.provider.oan.dev"),
            ("Scheme", "MH", "scheme.mh.provider.oan.dev"),
            ("advisory", "bv", "advisory.bv.provider.oan.dev"),
            ("scheme", "default", "scheme.bv.provider.oan.dev"),
            ("advisory", None, "advisory.bv.provider.oan.dev"),
        ],
    )
    def test_published_kinds_get_the_provider_id(self, kind, instance, expected):
        from pipeline.activities import _provider_id_from_doc

        assert _provider_id_from_doc({"document_kind": kind, "instance": instance}) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize("kind", ["document", "video", "how_to_faq", None])
    def test_kinds_that_never_publish_get_none(self, kind):
        from pipeline.activities import _provider_id_from_doc

        assert _provider_id_from_doc({"document_kind": kind, "instance": "mh"}) is None

    @pytest.mark.unit
    def test_follows_the_overrides_file(self, monkeypatch, tmp_path):
        from pipeline.activities import _provider_id_from_doc

        _use_overrides(monkeypatch, tmp_path, '{"ka": {"advisory": "advisory.ka.com"}}')

        assert _provider_id_from_doc({"document_kind": "advisory", "instance": "ka"}) == "advisory.ka.com"
        assert _provider_id_from_doc({"document_kind": "scheme", "instance": "ka"}) == "scheme.ka.provider.oan.dev"

    @pytest.mark.unit
    def test_an_invalid_provider_id_fails_without_retry(self):
        from pipeline.activities import _provider_id_from_doc

        with pytest.raises(ApplicationError, match="ladakh_ut") as excinfo:
            _provider_id_from_doc({"document_kind": "advisory", "instance": "ladakh_ut"})

        assert excinfo.value.non_retryable is True

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "kind,instance", [("advisory", "mh"), ("scheme", "bv"), ("advisory", "default"), ("scheme", "ka")]
    )
    def test_matches_the_provider_id_sent_in_publish(self, monkeypatch, tmp_path, kind, instance):
        from pipeline.activities import _provider_id_from_doc
        from pipeline.auth.tenancy import group_for_instance
        from pipeline.discovery_publish_service import DiscoveryPublishService

        _use_overrides(monkeypatch, tmp_path, '{"ka": {"scheme": "scheme.ka.com"}}')
        monkeypatch.setenv("DISCOVERY_SERVICE_ENDPOINT", "https://discovery.example.com")
        monkeypatch.setenv("NETWORK_SENDER_ID", "docs-pipeline-bv")
        response = MagicMock(status_code=200, text="{}")
        response.json.return_value = {}
        client = MagicMock()
        client.__enter__.return_value = client
        client.post.return_value = response

        with patch("pipeline.discovery_publish_service.httpx.Client", return_value=client):
            DiscoveryPublishService().publish("txn", kind, "wf", group_for_instance(instance))

        published = client.post.call_args.kwargs["json"]["message"]["catalogs"][0]["provider"]["id"]
        assert _provider_id_from_doc({"document_kind": kind, "instance": instance}) == published


class TestProviderIdOnRecords:
    @pytest.mark.unit
    def test_every_record_carries_the_provider_id(self):
        from pipeline.activities import _prepare_records

        records = _prepare_records("doc-1", "doc.pdf", [CHUNK, {**CHUNK, "chunk_number": 2}], provider_id="advisory.mh.provider.oan.dev")

        assert [r["provider_id"] for r in records] == ["advisory.mh.provider.oan.dev"] * 2

    @pytest.mark.unit
    def test_no_provider_id_is_left_off_the_point(self):
        from pipeline.activities import _prepare_records
        from pipeline.vector_store.qdrant_store import _record_payload

        (record,) = _prepare_records("doc-1", "doc.pdf", [CHUNK])

        assert "provider_id" not in _record_payload(record)

    @pytest.mark.unit
    def test_provider_id_is_in_the_passage_schema(self):
        from pipeline.activities import _passage_schema_field_names

        assert "provider_id" in _passage_schema_field_names()


class _RecordsBuilt(Exception):
    pass


class TestIngestPathsStampTheProviderId:
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
        monkeypatch.setattr(db, "get_document", lambda workflow_id: {"document_kind": "scheme", "instance": "mh"})
        return seen

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_dev_ingest(self, captured):
        from pipeline.activities import ingest_document_from_db

        with pytest.raises(_RecordsBuilt):
            await ingest_document_from_db("wf-1", "doc-1", "doc.pdf")

        assert captured["provider_id"] == "scheme.mh.provider.oan.dev"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_prod_promotion(self, captured, monkeypatch):
        import pipeline.activities as activities

        monkeypatch.setenv("PROD_VECTOR_DB_URL", "http://localhost:6333")
        monkeypatch.setenv("PROD_SCHEME_VECTOR_DB_COLLECTION_NAME", "schemes-index")
        monkeypatch.setattr(activities, "get_qdrant_client", lambda *a, **k: MagicMock(), raising=False)

        with pytest.raises(_RecordsBuilt):
            await activities.promote_document_to_prod_qdrant("wf-1", "doc-1", "doc.pdf")

        assert captured["provider_id"] == "scheme.mh.provider.oan.dev"
