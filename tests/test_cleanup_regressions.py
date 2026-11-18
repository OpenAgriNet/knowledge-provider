"""Integration regressions found during the metadata and retrieval review."""

import json
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from pipeline.intake.source_intake import (
    NormalizedSourceDocument,
    canonical_json_sha256,
    crawl_record_key,
    register_source_document,
)
from pipeline.search.index_identity import projection_doc_id


@pytest.mark.parametrize("period,form,expected", [
    ({"validity": {"startsAt": "2026-01-01T12:30:00Z", "endsAt": "2027-01-01T04:00:00Z"}}, {},
     {"startsAt": "2026-01-01T12:30:00Z", "endsAt": "2027-01-01T04:00:00Z"}),
    ({"valid_from": "2026-01-01", "valid_to": "2027-01-01"}, {},
     {"startsAt": "2026-01-01T00:00:00Z", "endsAt": "2027-01-01T23:59:59.999999Z"}),
    ({"validity": {"startsAt": "2026-01-01T12:30:00Z", "endsAt": "2027-01-01T04:00:00Z"}}, {"valid_to": "2027-02-01"},
     {"startsAt": "2026-01-01T12:30:00Z", "endsAt": "2027-02-01T23:59:59.999999Z"}),
    ({"validity": None}, {}, None),
])
def test_canonical_upload_preserves_period(test_client, db_connection, period, form, expected):
    body = {"external_id": "period", "title": "Period", "kind": "advisory", "text": "Full notice", **period}
    response = test_client.post("/upload?kind=advisory", data=form,
                                files={"file": ("period.json", json.dumps(body).encode(), "application/json")})
    assert response.status_code == 200, response.text
    assert db_connection.get_document(response.json()["workflow_id"]).get("validity") == expected


@pytest.mark.parametrize("normalized_raw", [False, True])
def test_crawl_replay_survives_historical_defaults_dates_and_enum_changes(db_connection, monkeypatch, tmp_path, normalized_raw):
    db = db_connection
    db.get_or_create_instance("bv")
    legacy = {"external_id": "crop", "title": "Wheat", "text": "Wheat field", "kind": "advisory",
              "valid_from": "2026-01-01", "knowledge_metadata": {"subject_categories": ["Crop"]}}
    document = NormalizedSourceDocument.model_validate(legacy)
    raw = legacy if normalized_raw else {"id": "crop", "name": "Wheat", "body": "Wheat field"}
    with db.get_connection() as conn:
        original = register_source_document(conn, instance="bv", source_kind="crawl", source_id="feed",
                   idempotency_key="historical-key", raw_payload=raw, document=document, release_target=None)
        conn.execute("UPDATE workflows SET status = 'completed' WHERE id = %s", (original["job_id"],))
        conn.execute("UPDATE source_intake_receipts SET normalized_document = %s, raw_payload = %s, raw_sha256 = %s WHERE id = %s",
                     (Jsonb(legacy), Jsonb(raw), canonical_json_sha256(raw), original["id"]))
    if normalized_raw:
        with db.get_connection() as conn:
            for number in range(65):
                changed = document.model_copy(update={"title": f"Later {number}"})
                receipt = register_source_document(conn, instance="bv", source_kind="crawl", source_id="feed",
                          idempotency_key=f"later-{number}", raw_payload=changed.model_dump(mode="json"), document=changed, release_target=None)
                conn.execute("UPDATE workflows SET status='completed' WHERE id=%s", (receipt["job_id"],))
    schema = tmp_path / "categories.json"
    schema.write_text('{"properties":{"subjectCategories":{"items":{"enum":["New category"]}}}}')
    monkeypatch.setenv("INTAKE_SUBJECT_SCHEMA", str(schema))
    new_raw = document.model_dump(mode="json", exclude_none=True) if normalized_raw else raw
    with db.get_connection() as conn:
        replay = register_source_document(conn, instance="bv", source_kind="crawl", source_id="feed",
                  idempotency_key=crawl_record_key(new_raw, document), raw_payload=new_raw, document=document, release_target=None)
        assert replay["id"] == original["id"]
        assert conn.execute("SELECT count(*) AS n FROM source_intake_receipts").fetchone()["n"] == (66 if normalized_raw else 1)
        assert conn.execute("SELECT normalized_document FROM source_intake_receipts WHERE id=%s",(original["id"],)).fetchone()["normalized_document"] == legacy


def test_local_liveness_requires_serving_state_owner_and_current_chunk(db_connection):
    db = db_connection
    owner = db.upsert_document("local-visible", "a" * 32, "notice.txt", "blob://notice.txt", instance="bv", kind="advisory", code="local")
    db.save_chunks(owner, [{"chunk_number": 1, "original_text": "Current"}, {"chunk_number": 2, "original_text": "Excluded", "is_excluded": True}])
    key = projection_doc_id(db.get_document(owner))
    hit = {"doc_id": key, "instance": "bv", "chunk_num": 1, "text": "Current"}
    assert db.visible_document_hits([hit], index_name="serving") == []
    db.upsert_document_index_status(owner, "serving", doc_id=key, status="indexed")
    assert db.visible_document_hits([hit, {**hit, "chunk_num": 2}, {**hit, "chunk_num": 99}, {**hit, "instance": "mh"}], index_name="serving") == [hit]
    assert db.visible_document_hits([hit], index_name="unregistered") == []
    db.upsert_document_index_status(owner, "serving", doc_id="old-fingerprint", status="indexed")
    assert db.visible_document_hits([hit], index_name="serving") == []
    legacy = {**hit, "doc_id": "old-fingerprint"}
    assert db.visible_document_hits([legacy], index_name="serving") == [legacy]
    db.set_document_disabled(owner, True)
    assert db.visible_document_hits([legacy], index_name="serving") == []


def test_local_liveness_uses_frozen_passages_after_new_draft(db_connection):
    db = db_connection
    owner = db.upsert_document("frozen-local", "a" * 32, "notice.txt", "blob://notice.txt", instance="bv", kind="advisory", code="local")
    key = projection_doc_id(db.get_document(owner))
    db.save_chunks(owner, [{"chunk_number": 1, "original_text": "Approved"}])
    db.save_draft_revision_snapshot(owner, "frozen-local", str(uuid4()), {"vector_doc_id": key, "passages": [{"chunk_num": 1, "text": "Approved"}]})
    db.upsert_document_index_status(owner, "serving", doc_id=key, status="indexed")
    hit = {"doc_id": key, "instance": "bv", "chunk_num": 1, "text": "Approved"}
    assert db.visible_document_hits([hit], index_name="serving") == [hit]
    db.create_document_job(owner, "pipeline", temporal_workflow_id="unfinished-local")
    db.save_chunks(owner, [{"chunk_number": 9, "original_text": "Unapproved"}])
    assert db.visible_document_hits([hit, {**hit, "chunk_num": 9}, {**hit, "text": "Unapproved"}], index_name="serving") == [hit]


def test_saved_reranking_aliases_normalize_and_settings_limits_are_bounded(db_connection):
    assert db_connection.update_search_settings({"rerankMode": "cross-encoder"})["rerankMode"] == "heuristic"
    for field, value in (("candidateCap", 201), ("limit", 51), ("maxChunksPerDoc", 51), ("efSearch", 10001)):
        with pytest.raises(ValueError):
            db_connection.update_search_settings({field: value})


def test_old_settings_are_readable_and_repairable(db_connection):
    db = db_connection
    with db.get_connection() as conn:
        conn.execute("UPDATE search_settings SET candidates = %s", (Jsonb({"candidateCap": 250}),))
    assert db.get_search_settings()["candidateCap"] == 200
    assert db.update_search_settings({"candidateCap": 100})["candidateCap"] == 100


def test_unnamed_local_search_uses_configured_collection(test_client, capturing_store, monkeypatch):
    monkeypatch.delenv("KP_INDEX_ONLY", raising=False)
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "configured-serving")
    assert test_client.post("/search", json={"query": "PM Kisan overview"}).status_code == 200
    assert capturing_store.captured["name"] == "configured-serving"
    assert test_client.post("/search", json={"query": "PM Kisan overview", "index_name": "other"}).status_code == 422


@pytest.mark.parametrize("extension", ["pdf", "xlsx"])
def test_binary_crawl_receipt_replay_has_no_text_requirement(db_connection, extension):
    from pipeline.intake.source_intake import BinarySource, SourceDocumentMetadata

    db = db_connection
    db.get_or_create_instance("bv")
    doc = SourceDocumentMetadata(external_id="report", title="Report", source_url="https://example.test/report")
    raw = {"sha256": "b" * 64, "filename": f"report.{extension}", "media_type": "application/octet-stream"}
    binary = BinarySource(f"report.{extension}", "minio://docs/report", "a" * 32, "application/octet-stream", 10)
    with db.get_connection() as conn:
        first = register_source_document(conn, instance="bv", source_kind="crawl", source_id="binary", idempotency_key="old-shape",
                  raw_payload=raw, document=doc, binary=binary, release_target=None)
        conn.execute("UPDATE workflows SET status = 'completed' WHERE id = %s", (first["job_id"],))
        second = register_source_document(conn, instance="bv", source_kind="crawl", source_id="binary", idempotency_key=crawl_record_key(raw, doc),
                  raw_payload=raw, document=doc, binary=binary, release_target=None)
        assert second["id"] == first["id"]


@pytest.mark.asyncio
async def test_editing_suggestion_retains_proof_and_accepting_subset_retains_other_fields(db_connection, sample_document, monkeypatch):
    from pipeline.db import metadata_review
    from pipeline.metadata.extraction import source_digest
    from pipeline.metadata.metadata_edits import MetadataEdit

    db = db_connection
    wid = sample_document["workflow_id"]
    owner = db.get_document(wid)["id"]
    db.save_chunks(owner, [{"chunk_number": 1, "original_text": "Wheat during Rabi"}])
    digest = source_digest([(1, "Wheat during Rabi")])
    proposal = {"source_digest": digest, "model": "test", "origin": "generated", "facts": [
        {"field": "crop", "value": "Wheat", "evidence": {"block": 1, "start": 0, "end": 5, "quote": "Wheat"}},
        {"field": "season", "value": "Rabi", "evidence": {"block": 1, "start": 13, "end": 17, "quote": "Rabi"}},
    ]}
    async def extract(_):
        return proposal.copy()
    monkeypatch.setattr(metadata_review, "extract_metadata", extract)
    monkeypatch.setattr(metadata_review, "extraction_config", lambda: ("unused", "test"))
    generated = await metadata_review.generate_review(owner, 1)
    edit = MetadataEdit(version=generated["version"], overrides=["crop"], facts=[proposal["facts"][0]])
    saved = metadata_review.save_review(owner, wid, 1, edit, actor_user_id="reviewer")
    assert saved["accepted"]["facts"][0]["evidence"]["quote"] == "Wheat"
    generated = await metadata_review.generate_review(owner, 1)
    saved = metadata_review.accept_review(owner, wid, 1, generated["pending"]["id"], [1], actor_user_id="reviewer")
    assert {fact["field"] for fact in saved["accepted"]["facts"]} == {"crop", "season"}


def test_baseline_upgrade_preserves_catalog_ids_and_projection_ownership(db_connection):
    from pathlib import Path

    db = db_connection
    owner = db.upsert_document('upgrade-owner', 'a'*32, 'test.pdf', 'blob://test.pdf', kind='scheme', code='upgrade')
    catalog_id = db.get_document(owner)['catalog_id']
    with db.get_connection() as conn:
        with conn.transaction(force_rollback=True):
            conn.execute('DROP VIEW catalog_status')
            conn.execute('ALTER TABLE catalogs DROP CONSTRAINT catalogs_instance_id_kind_key')
            conn.execute('ALTER TABLE catalogs DROP COLUMN kind')
            conn.execute("UPDATE catalogs SET type='openagrinet:AgricultureResource' WHERE id=%s", (catalog_id,))
            conn.execute('ALTER TABLE catalogs ADD CONSTRAINT catalogs_instance_id_type_key UNIQUE(instance_id,type)')
            conn.execute("CREATE VIEW legacy_document_catalog_types AS SELECT * FROM (VALUES ('scheme','openagrinet:AgricultureResource'),('advisory','openagrinet:KnowledgeAdvisory'),('video','openagrinet:VideoResource')) m(kind,catalog_type)")
            conn.execute('CREATE VIEW catalog_status AS SELECT id FROM documents')
            conn.execute("DELETE FROM schema_migrations WHERE version='0025'")
            conn.execute("INSERT INTO schema_migrations(version,name) VALUES ('0024','previous_baseline')")
            conn.execute("UPDATE documents SET metadata=metadata || %s WHERE id=%s", (Jsonb({
                'index_status': {'dev': {'index_name':'serving','doc_id':'a'*32,'status':'indexed'}, 'prod': {'index_name':'published','doc_id':'b'*32,'status':'indexed'}},
                'superseded_doc_ids': {'prod':['old-key']}}),owner))
            conn.execute((Path(__file__).resolve().parents[1] / 'docs/upgrades/0024_to_0025.sql').read_text())
            row = conn.execute('SELECT kind,type,id::text FROM catalogs WHERE id=%s',(catalog_id,)).fetchone()
            assert row == {'kind':'scheme','type':'openagrinet:KnowledgeAdvisory','id':catalog_id}
            metadata = conn.execute('SELECT metadata FROM documents WHERE id=%s',(owner,)).fetchone()['metadata']
            assert set(metadata['index_status']) == {'serving','published'}
            assert metadata['index_status']['published']['publication'] is True
            assert metadata['superseded_doc_ids'] == {'published':['old-key']}
