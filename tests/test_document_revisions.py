from uuid import uuid4

import pytest


@pytest.mark.db
def test_revision_snapshot_retry_and_network_visibility(db_connection):
    db = db_connection
    network = "oan-dev.local"
    workflow = "doc-revision-first"
    doc_id = db.upsert_document(
        workflow_id=workflow, document_id="a" * 32, filename="advisory.pdf", filepath="minio://documents/advisory.pdf",
        instance="bv", kind="advisory", code="revision-test",
    )
    job = db.get_latest_document_job(workflow)
    db.update_document_job(job["id"], config_json={"release_network_id": network})
    first_id = str(uuid4())
    first = db.save_revision_snapshot(doc_id, workflow, network, first_id, {"passages": [{"text": "first"}]})
    retry = db.save_revision_snapshot(doc_id, workflow, network, str(uuid4()), {"passages": [{"text": "changed"}]})
    assert first == retry

    legacy = {"text": "legacy"}
    first_hit = {"text": "first", "revision_id": first_id, "release_network_id": network}
    wrong_network = {**first_hit, "release_network_id": "oan-prod.local"}
    assert db.visible_release_hits([legacy, first_hit, wrong_network]) == [legacy]
    with pytest.raises(ValueError):
        db.activate_network_release(doc_id, workflow, "oan-prod.local", str(uuid4()))
    db.activate_network_release(doc_id, workflow, network, str(uuid4()))
    assert db.visible_release_hits([legacy, first_hit, wrong_network]) == [legacy, first_hit]

    db.update_document_job(job["id"], status="completed")
    replacement = "doc-revision-second"
    db.create_document_job(
        workflow_id=doc_id, job_type="pipeline", temporal_workflow_id=replacement,
        config={"release_network_id": network},
    )
    second_id = str(uuid4())
    second_hit = {"text": "second", "revision_id": second_id, "release_network_id": network}
    db.save_revision_snapshot(doc_id, replacement, network, second_id, {"passages": [{"text": "second"}]})
    assert db.visible_release_hits([first_hit, second_hit]) == [first_hit]
    db.activate_network_release(doc_id, replacement, network, str(uuid4()))
    with pytest.raises(ValueError):
        db.activate_network_release(doc_id, workflow, network, str(uuid4()))
    assert db.visible_release_hits([first_hit, second_hit]) == [second_hit]
    assert {revision["id"] for revision in db.list_document_revisions(doc_id)["revisions"]} == {first_id, second_id}
    generation_before_withdrawal = db.list_document_revisions(doc_id)["releases"][0]["generation"]
    assert not db.withdraw_network_release(doc_id, network, first_id)
    assert db.withdraw_network_release(doc_id, network, second_id)
    assert db.visible_release_hits([first_hit, second_hit]) == []
    db.activate_network_release(doc_id, replacement, network, str(uuid4()))
    assert db.visible_release_hits([second_hit]) == []
    with pytest.raises(ValueError):
        db.select_network_revision(doc_id, network, first_id, str(uuid4()), expected_generation=generation_before_withdrawal)
    generation = db.list_document_revisions(doc_id)["releases"][0]["generation"]
    db.select_network_revision(doc_id, network, first_id, str(uuid4()), expected_generation=generation)
    assert db.visible_release_hits([first_hit, second_hit]) == [first_hit]


@pytest.mark.db
@pytest.mark.parametrize("operation", ["withdraw", "rollback"])
def test_staged_revision_cannot_override_a_later_operator_selection(db_connection, operation):
    db = db_connection
    network = "network.example"
    workflow = "doc-original"
    doc_id = db.upsert_document(
        workflow_id=workflow, document_id="b" * 32, filename="notice.pdf", filepath="minio://docs/notice.pdf",
        instance="provider", kind="advisory", code="release-race",
    )
    job = db.get_latest_document_job(workflow)
    db.update_document_job(job["id"], config_json={"release_network_id": network})
    revisions = []
    for sequence in range(3):
        if sequence:
            db.update_document_job(job["id"], status="completed")
            workflow = f"doc-replacement-{sequence}"
            db.create_document_job(
                workflow_id=doc_id, job_type="pipeline", temporal_workflow_id=workflow,
                config={"release_network_id": network},
            )
            job = db.get_latest_document_job(doc_id)
        revision_id = str(uuid4())
        db.save_revision_snapshot(doc_id, workflow, network, revision_id, {"passages": [{"text": workflow}]})
        revisions.append(revision_id)
        if sequence < 2:
            db.activate_network_release(doc_id, workflow, network, str(uuid4()))

    active = db.list_document_revisions(doc_id)["releases"][0]
    if operation == "withdraw":
        assert db.withdraw_network_release(doc_id, network, revisions[1])
    else:
        db.select_network_revision(doc_id, network, revisions[0], str(uuid4()), active["generation"])
    selected = db.list_document_revisions(doc_id)["releases"]
    with pytest.raises(ValueError, match="changed after this revision was staged"):
        db.activate_network_release(doc_id, workflow, network, str(uuid4()))
    assert db.list_document_revisions(doc_id)["releases"] == selected


@pytest.mark.db
def test_inactive_release_filter_refuses_to_silently_drop_ids(db_connection, sample_document):
    db = db_connection
    document_id = db.get_document(sample_document["workflow_id"])["id"]
    with db.get_connection() as conn:
        conn.execute(
            """
            WITH new_workflows AS (
                INSERT INTO workflows (document_id, workflow_type, temporal_workflow_id, config)
                SELECT %s::uuid, 'pipeline', 'inactive-release-' || i, '{"release_network_id":"test.net"}'::jsonb
                FROM generate_series(1, 1000) AS i
                RETURNING id, document_id
            ), revisions AS (
                INSERT INTO document_revisions (id, document_id, workflow_id, content_sha256, snapshot)
                SELECT gen_random_uuid(), document_id, id, repeat('a', 64), '{}'::jsonb FROM new_workflows
                RETURNING id
            )
            INSERT INTO network_release_staging (revision_id, network_id, collection)
            SELECT id, 'test.net', 'test-release' FROM revisions
            """,
            (document_id,),
        )
    assert len(db.inactive_network_revision_ids("test.net", "test-release")) == 1000
    with db.get_connection() as conn:
        conn.execute(
            """
            WITH new_workflow AS (
                INSERT INTO workflows (document_id, workflow_type, temporal_workflow_id, config)
                VALUES (%s::uuid, 'pipeline', 'overflow-release', '{"release_network_id":"test.net"}'::jsonb)
                RETURNING id, document_id
            ), revisions AS (
                INSERT INTO document_revisions (id, document_id, workflow_id, content_sha256, snapshot)
                SELECT gen_random_uuid(), document_id, id, repeat('a', 64), '{}'::jsonb FROM new_workflow
                RETURNING id
            )
            INSERT INTO network_release_staging (revision_id, network_id, collection)
            SELECT id, 'test.net', 'test-release' FROM revisions
            """,
            (document_id,),
        )
    with pytest.raises(ValueError, match="exceeds the complete pre-ranking filter limit"):
        db.inactive_network_revision_ids("test.net", "test-release")


@pytest.mark.db
def test_one_revision_can_be_staged_and_released_independently_on_two_networks(db_connection, sample_document):
    db = db_connection
    workflow = sample_document["workflow_id"]
    document_id = db.get_document(workflow)["id"]
    job = db.get_latest_document_job(workflow)
    db.update_document_job(job["id"], config_json={"release_network_id": "test.dev"})
    revision_id = str(uuid4())
    snapshot = {"passages": [{"text": "Shared content"}]}
    db.save_revision_snapshot(
        document_id, workflow, "test.dev", revision_id, snapshot, collection="dev-published",
    )
    db.activate_network_release(document_id, workflow, "test.dev", str(uuid4()))

    transaction = str(uuid4())
    assert db.stage_saved_network_revision(
        document_id, "test.prod", revision_id, transaction, None, "prod-published", "provider.test.prod",
    )
    assert db.inactive_network_revision_ids("test.dev", "dev-published") == []
    assert db.inactive_network_revision_ids("test.prod", "prod-published") == [revision_id]
    db.select_network_revision(document_id, "test.prod", revision_id, transaction, None)
    assert db.inactive_network_revision_ids("test.prod", "prod-published") == []
    assert not db.stage_saved_network_revision(
        document_id, "test.prod", revision_id, transaction, None, "prod-published", "provider.test.prod",
    )
    assert db.withdraw_network_release(document_id, "test.dev", revision_id)
    assert db.inactive_network_revision_ids("test.dev", "dev-published") == [revision_id]
    assert db.inactive_network_revision_ids("test.prod", "prod-published") == []
    assert db.get_revision_snapshot(document_id, revision_id)["snapshot"] == snapshot


@pytest.mark.db
@pytest.mark.asyncio
async def test_ingest_freezes_a_revision_without_releasing_it(db_connection, sample_document, monkeypatch):
    from pipeline.temporal.activities import indexing as activities

    db = db_connection
    workflow = sample_document["workflow_id"]
    doc_id = db.get_document(workflow)["id"]
    db.save_chunks(workflow, [{"chunk_number": 1, "original_text": "A draft can be released later."}])
    monkeypatch.setattr(activities, "_upload_artifact", lambda *_: ("blob://docs/export.json", 1, "application/json"))

    async def ingest(records, **_):
        return {"records_ingested": len(records)}

    monkeypatch.setattr(activities, "ingest_to_vector_db", ingest)
    await activities.ingest_document_from_db(workflow, "source", "draft.pdf", freeze_draft_revision=True)

    history = db.list_document_revisions(doc_id)
    assert len(history["revisions"]) == 1
    assert history["releases"] == []
    snapshot = db.get_revision_snapshot(doc_id, history["revisions"][0]["id"])["snapshot"]
    assert snapshot["passages"][0]["text"] == "A draft can be released later."
    assert snapshot["passages"][0]["doc_id"] != db.get_document_index_status(workflow, "documents-index")["doc_id"]
