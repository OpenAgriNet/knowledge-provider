import importlib
import json
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient
from temporalio.exceptions import ApplicationError

from pipeline.intake import scheduled_intake
from pipeline.intake.source_fetch import SourceFile
from pipeline.network.network_publisher import PublishConfigError, validate_network_adapters
from pipeline.network.release_policy import upload_release_target
from pipeline.temporal.activities import indexing, publication
from pipeline.vector_store import qdrant_store


@pytest.fixture
def installed_adapter(tmp_path, monkeypatch):
    package = tmp_path / "kp_example_adapter.py"
    package.write_text('''
from pipeline.network.adapters.contracts import PublicationReceipt

attempts = []
evidence = "a" * 64
matched = True
status = "ACCEPTED"
rejection = None
has_exchange = True

class NoticeAdapter:
    def __init__(self, connection):
        self.connection = connection

    def validate(self):
        assert self.connection.options == {"channel": "local-notices"}

    def supports_kind(self, kind):
        return kind == "notice"

    def provider_id(self, kind, provider_code):
        return f"account:{provider_code}" if self.supports_kind(kind) else None

    def publication_evidence(self, conn, request):
        assert request.revision["metadata"]["kind"] == request.kind == "notice"
        assert request.provider_id == "account:example"
        return evidence

    def publish_revision(self, request):
        attempts.append(request)
        return PublicationReceipt(
            exchange={"protocol": "notice-test"} if has_exchange else None, message_id="notice-receipt",
            result_status=status, context_matches=matched, rejection=rejection,
        )
''')
    distribution = tmp_path / "kp_example_adapter-1.0.dist-info"
    distribution.mkdir()
    (distribution / "entry_points.txt").write_text(
        "[knowledge_provider.network_adapters]\nnotice-test = kp_example_adapter:NoticeAdapter\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for key, value in {
        "DOCUMENT_KINDS": "notice,advisory",
        "DOCUMENT_KIND_SCHEMAS": '{"notice":"example:Notice"}',
        "ENABLE_PUBLISH_TO_NETWORK": "true",
        "NETWORK_CONNECTIONS": "external",
        "NETWORK_CONNECTION_DEFAULT": "external",
        "NETWORK_CONNECTION_EXTERNAL_TYPE": "notice-test",
        "NETWORK_CONNECTION_EXTERNAL_ID": "external.test",
        "NETWORK_CONNECTION_EXTERNAL_RELEASE_COLLECTION": "published-notices",
        "NETWORK_CONNECTION_EXTERNAL_OPTION_CHANNEL": "local-notices",
        "AUTO_RELEASE_NETWORK_ID": "external.test",
        "AUTO_RELEASE_COLLECTION": "published-notices",
        "BECKN_CATALOG_PROFILE_PATH": str(tmp_path / "unavailable-beckn-profile.json"),
        "PROVIDER_CONFIG_OVERRIDE_PATH": str(tmp_path / "unavailable-beckn-names.json"),
    }.items():
        monkeypatch.setenv(key, value)
    module = importlib.import_module("kp_example_adapter")
    module.attempts.clear()
    module.matched = True
    module.evidence = "a" * 64
    module.status = "ACCEPTED"
    module.rejection = None
    module.has_exchange = True
    return module


def test_startup_rejects_an_ambiguous_installed_adapter(installed_adapter, monkeypatch):
    monkeypatch.setenv("NETWORK_CONNECTION_EXTERNAL_TYPE", "duplicate-test")
    monkeypatch.setattr("pipeline.network.network_publisher.entry_points", lambda **_: (object(), object()))
    with pytest.raises(PublishConfigError, match="Install exactly one matching network adapter"):
        validate_network_adapters()


@pytest.mark.db
@pytest.mark.asyncio
async def test_scheduled_automatic_release_requires_a_supported_destination(
    db_connection, installed_adapter, monkeypatch, mock_object_store,
):
    profile = scheduled_intake.PullProfile(
        provider="example", source_key="notices", host="notices.example.test",
        url="https://notices.example.test/feed.json", auto_release=True,
    )
    db_connection.get_or_create_instance(profile.provider)
    record = {"external_id": "notice-1", "title": "Notice", "text": "Inspection notice", "kind": "advisory"}

    async def fetch(*_):
        return SourceFile("feed.json", "application/json", json.dumps(record).encode(), profile.url)

    monkeypatch.setattr(scheduled_intake, "fetch_source", fetch)
    monkeypatch.setattr(scheduled_intake, "open_object_store", lambda _: mock_object_store)
    assert not await scheduled_intake.poll_source(profile)
    with db_connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM source_intake_receipts").fetchone()["n"] == 0
        assert conn.execute("SELECT failure_count FROM scheduled_source_pulls").fetchone()["failure_count"] == 1
        conn.execute("UPDATE scheduled_source_pulls SET next_run_at = now()")

    record["kind"] = "notice"
    assert await scheduled_intake.poll_source(profile)
    with db_connection.get_connection() as conn:
        job = conn.execute(
            "SELECT w.config, w.status FROM source_intake_receipts r JOIN workflows w ON w.id = r.job_id",
        ).fetchone()
        assert conn.execute("SELECT count(*) AS n FROM network_releases").fetchone()["n"] == 0
    assert job["status"] == "start_pending"
    assert job["config"]["release_network_id"] == "external.test"
    assert job["config"]["release_collection"] == "published-notices"
    assert job["config"]["upload_start_args"][6] == "published-notices"


@pytest.mark.db
@pytest.mark.asyncio
async def test_scheduled_source_selects_and_pins_a_nondefault_network(
    db_connection, installed_adapter, monkeypatch, mock_object_store,
):
    for key, value in {
        "NETWORK_CONNECTIONS": "external,secondary",
        "NETWORK_CONNECTION_SECONDARY_TYPE": "notice-test",
        "NETWORK_CONNECTION_SECONDARY_ID": "secondary.test",
        "NETWORK_CONNECTION_SECONDARY_RELEASE_COLLECTION": "second-notices",
        "NETWORK_CONNECTION_SECONDARY_OPTION_CHANNEL": "local-notices",
    }.items():
        monkeypatch.setenv(key, value)
    profile = scheduled_intake.PullProfile(
        provider="example", source_key="notices", host="notices.example.test",
        url="https://notices.example.test/feed.json", auto_release=True, release_network_id="unknown.test",
    )
    db_connection.get_or_create_instance(profile.provider)
    record = {"external_id": "notice-1", "title": "Notice", "text": "Inspection notice", "kind": "notice"}
    records = [record]

    async def fetch(*_):
        return SourceFile("feed.json", "application/json", json.dumps(records).encode(), profile.url)

    monkeypatch.setattr(scheduled_intake, "fetch_source", fetch)
    monkeypatch.setattr(scheduled_intake, "open_object_store", lambda _: mock_object_store)
    assert not await scheduled_intake.poll_source(profile)
    with db_connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM source_intake_receipts").fetchone()["n"] == 0

    profile = profile.model_copy(update={"release_network_id": "secondary.test"})
    assert await scheduled_intake.poll_source(profile)
    with db_connection.get_connection() as conn:
        receipt = conn.execute("SELECT document_id::text, job_id::text FROM source_intake_receipts").fetchone()
    job = db_connection.get_document_job(receipt["job_id"])
    document = db_connection.get_document(receipt["document_id"])
    workflow_id = job["temporal_workflow_id"]
    assert job["config"]["explicit_release_network"]
    assert job["config"]["upload_start_args"][6] == "second-notices"
    assert upload_release_target("notice") == ("external.test", "published-notices")
    assert indexing._release_target_for_job(document, job, workflow_id, "secondary.test") == (
        "secondary.test", "second-notices",
    )
    assert indexing._release_target_for_job(document, job, workflow_id, "external.test") is None
    assert indexing._release_target_for_job(document, job, "stale-workflow", "secondary.test") is None
    with monkeypatch.context() as changed:
        changed.setenv("NETWORK_CONNECTION_DEFAULT", "secondary")
        changed.setenv("AUTO_RELEASE_NETWORK_ID", "secondary.test")
        changed.setenv("AUTO_RELEASE_COLLECTION", "second-notices")
        default_profile = profile.model_copy(update={"release_network_id": None})
        assert await scheduled_intake.poll_source(default_profile)
        with db_connection.get_connection() as conn:
            assert conn.execute("SELECT count(*) AS n FROM source_intake_receipts").fetchone()["n"] == 1
    records.append({**record, "external_id": "notice-2", "text": "New inspection notice"})
    retargeted = profile.model_copy(update={"release_network_id": "external.test"})
    assert await scheduled_intake.poll_source(retargeted)
    with db_connection.get_connection() as conn:
        receipts = conn.execute(
            "SELECT external_id, document_id::text, job_id::text FROM source_intake_receipts ORDER BY external_id"
        ).fetchall()
        assert receipts[0] == {"external_id": "notice-1", **receipt}
        assert len(receipts) == 2
        assert conn.execute(
            "SELECT failure_count, last_page_policy_conflicts FROM scheduled_source_pulls "
            "WHERE provider = %s AND source_key = %s AND profile_sha256 = %s", retargeted.identity,
        ).fetchone() == {"failure_count": 0, "last_page_policy_conflicts": 1}
        conn.execute("UPDATE scheduled_source_pulls SET next_run_at = now()")
    assert db_connection.get_document_job(receipts[1]["job_id"])["config"]["release_network_id"] == "external.test"
    assert db_connection.get_document_job(receipt["job_id"])["config"]["release_network_id"] == "secondary.test"
    db_connection.update_document_job(receipt["job_id"], status="completed", current_stage="completed")
    records[0] = {**record, "text": "Revised inspection notice"}
    assert await scheduled_intake.poll_source(retargeted)
    with db_connection.get_connection() as conn:
        revisions = conn.execute(
            "SELECT document_id::text, job_id::text FROM source_intake_receipts "
            "WHERE external_id = 'notice-1' AND job_id != %s", (receipt["job_id"],),
        ).fetchall()
    assert len(revisions) == 1 and revisions[0]["document_id"] == receipt["document_id"]
    assert db_connection.get_document_job(revisions[0]["job_id"])["config"]["release_network_id"] == "external.test"
    monkeypatch.setenv("NETWORK_CONNECTION_SECONDARY_RELEASE_COLLECTION", "changed-notices")
    assert indexing._release_target_for_job(document, job, workflow_id, "secondary.test") is None


@pytest.mark.db
@pytest.mark.asyncio
async def test_installed_adapter_releases_and_searches_a_frozen_notice(
    test_client, db_connection, installed_adapter, mock_temporal_client, monkeypatch,
):
    validate_network_adapters()
    assert upload_release_target("notice") == ("external.test", "published-notices")
    assert upload_release_target("advisory") is None
    db = db_connection
    workflow_id = "external-notice"
    document_id = db.upsert_document(
        workflow_id=workflow_id, document_id="b" * 32, filename="notice.txt", filepath="blob://docs/notice.txt",
        instance="example", kind="notice", code="notice-test",
    )
    revision_id, transaction_id, point_id = (str(uuid4()) for _ in range(3))
    db.save_draft_revision_snapshot(document_id, workflow_id, revision_id, {
        "metadata": {"kind": "notice"},
        "passages": [{"_id": point_id, "doc_id": revision_id, "text": "Inspection notice", "is_reference": False, "type": "notice"}],
    })
    selected = test_client.put(f"/documents/{workflow_id}/releases/external.test", json={"revision_id": revision_id})
    assert selected.status_code == 202, selected.text
    assert mock_temporal_client.start_workflow.call_args.kwargs["args"] == [
        document_id, revision_id, "external.test", None,
    ]

    monkeypatch.setenv("EMBEDDING_VECTOR_SIZE", "2")
    monkeypatch.setattr(qdrant_store, "embed_passages", lambda rows, **_: [[1.0, 0.0] for _ in rows])
    monkeypatch.setattr(qdrant_store, "embed_query", lambda *_, **__: [1.0, 0.0])
    store = qdrant_store.QdrantVectorStore(client=QdrantClient(":memory:"))
    monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: store)
    artifacts = iter([OSError("receipt artifact unavailable"), None, None, None])

    def save_artifact(*_):
        error = next(artifacts)
        if error:
            raise error
        return "blob://docs/receipt.json", 10, "application/json"

    monkeypatch.setattr(publication, "_upload_artifact", save_artifact)
    with pytest.raises(OSError, match="receipt artifact unavailable"):
        await publication.release_saved_revision(document_id, revision_id, "external.test", transaction_id, None)
    point, = store.list_by_doc_id("published-notices", revision_id)
    assert point["provider_id"] == "account:example"
    assert db.visible_release_hits([point]) == []

    await publication.release_saved_revision(document_id, revision_id, "external.test", transaction_id, None)
    request, = installed_adapter.attempts
    assert request.revision_id == revision_id
    assert request.transaction_id == transaction_id
    assert db.visible_release_hits([point]) == [point]

    from pipeline.api import api
    from pipeline.auth import deps
    from pipeline.auth.models import AuthUser
    from pipeline.auth.permissions import Permission

    monkeypatch.setenv("SEARCH_NETWORK_PRINCIPALS", '{"adapter-reader":["external.test"]}')
    monkeypatch.setitem(api.app.dependency_overrides, deps.get_current_user,
                        lambda: AuthUser("adapter-reader", roles=["superadmin"], permissions={Permission.SEARCH}))
    search = test_client.post("/search", json={
        "query": "Inspection", "search_mode": "TENSOR", "index_name": "published-notices",
        "network_id": "external.test", "document_kind": "notice",
        "provider_id": "account:example",
    })
    assert search.status_code == 200, search.text
    hit, = search.json()["hits"]
    assert (hit["text"], hit["revision_id"], hit["release_network_id"]) == (
        "Inspection notice", revision_id, "external.test",
    )
    collections = test_client.get("/search/collections")
    assert collections.json() == [{"label": "Published documents", "name": "published-notices", "network_id": "external.test"}]
    assert test_client.get(f"/documents/{workflow_id}").status_code == 403
    api.app.dependency_overrides.pop(deps.get_current_user)

    installed_adapter.matched = False
    generation = db.list_document_revisions(document_id)["releases"][0]["generation"]
    with pytest.raises(ApplicationError, match="did not accept"):
        await publication.release_saved_revision(document_id, revision_id, "external.test", str(uuid4()), generation)
    assert db.list_document_revisions(document_id)["releases"][0]["generation"] == generation
    detail = test_client.get(f"/documents/{workflow_id}").json()
    assert detail["network_publish"]["status"] == "rejected"

    installed_adapter.matched = True
    installed_adapter.status = "REJECTED"
    installed_adapter.rejection = "The notice network refused publication"
    with pytest.raises(ApplicationError, match="refused publication"):
        await publication.release_saved_revision(document_id, revision_id, "external.test", str(uuid4()), generation)
    detail = test_client.get(f"/documents/{workflow_id}").json()
    assert detail["network_publish"]["reason"] == installed_adapter.rejection

    installed_adapter.status = "ACCEPTED"
    installed_adapter.rejection = None
    installed_adapter.has_exchange = False
    invalid_transaction = str(uuid4())
    with pytest.raises(ApplicationError, match="without its publication exchange"):
        await publication.release_saved_revision(document_id, revision_id, "external.test", invalid_transaction, generation)
    with db.get_connection() as conn:
        invalid = conn.execute("SELECT receipt FROM publication_deliveries WHERE transaction_id = %s", (invalid_transaction,)).fetchone()
    assert invalid["receipt"] is None
    assert db.list_document_revisions(document_id)["releases"][0]["generation"] == generation
