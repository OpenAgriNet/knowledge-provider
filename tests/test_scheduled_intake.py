import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline.api.routers import providers
from pipeline.auth.deps import get_current_user
from pipeline.auth.jwt import claims_to_user
from pipeline.intake import scheduled_intake
from pipeline.intake.source_fetch import SourceFile, SourcePageError


@pytest.mark.db
def test_provider_scheduled_status_filters_scope_and_current_profile(db_connection, tmp_path, monkeypatch):
    monkeypatch.setenv("KEYCLOAK_PROVIDER_GROUP_ROOT", "providers")
    monkeypatch.setenv("KEYCLOAK_VERIFY_LIVE_GROUPS", "true")
    config = tmp_path / "sources.json"
    monkeypatch.setenv("SCHEDULED_PULL_PROFILES_FILE", str(config))
    base = {
        "provider": "example", "host": "reports.example.test",
        "url": "https://reports.example.test/feed?key=fixture-only",
        "authorization_env": "CRAWL_AUTH_FIXTURE",
    }
    entries = [
        dict(
            base, source_key=key, auto_release=(key == "retry"),
            release_network_id="notices.test" if key == "retry" else None,
        )
        for key in ("awaiting", "active", "retry", "done")
    ]
    config.write_text(json.dumps([*entries, dict(base, provider="other", source_key="other-only")]))
    checkpoints = {entry["source_key"]: scheduled_intake.PullProfile.model_validate(entry).identity for entry in entries}
    with db_connection.get_connection() as conn:
        conn.execute(
            "INSERT INTO scheduled_source_pulls (provider, source_key, profile_sha256, failure_count, cursor) "
            "VALUES (%s, %s, %s, 9, 'stale-cursor')", ("example", "awaiting", "f" * 64),
        )
        conn.execute(
            "INSERT INTO scheduled_source_pulls (provider, source_key, profile_sha256, lease_expires_at) "
            "VALUES (%s, %s, %s, now() + interval '1 minute')", checkpoints["active"],
        )
        conn.execute(
            "INSERT INTO scheduled_source_pulls (provider, source_key, profile_sha256, failure_count, last_fetch_at, cursor) "
            "VALUES (%s, %s, %s, 2, now(), 'private-cursor')", checkpoints["retry"],
        )
        conn.execute(
            "INSERT INTO scheduled_source_pulls (provider, source_key, profile_sha256, last_fetch_at, last_page_policy_conflicts) "
            "VALUES (%s, %s, %s, now(), 3)", checkpoints["done"],
        )
    app = FastAPI()
    app.include_router(providers.router)
    app.dependency_overrides[get_current_user] = lambda: claims_to_user({
        "sub": "manager", "groups": ["/providers/example/admin"],
    })
    client = TestClient(app)
    assert client.get("/providers/other/scheduled-sources").status_code == 403
    response = client.get("/providers/example/scheduled-sources")
    assert response.status_code == 200, response.text
    assert response.json()["checked_at"]
    assert all(value not in response.text for value in ("fixture-only", "private-cursor", "CRAWL_AUTH", "other-only"))
    sources = {source["source_key"]: source for source in response.json()["sources"]}
    assert {key: state["status"] for key, state in sources.items()} == {
        "active": "polling", "awaiting": "awaiting_worker", "done": "scheduled", "retry": "retrying",
    }
    assert sources["active"]["next_check_at"] is None
    assert sources["awaiting"]["last_page_received_at"] is None
    assert sources["awaiting"]["last_page_policy_conflicts"] == 0
    assert sources["done"]["last_page_policy_conflicts"] == 3
    assert sources["retry"]["consecutive_failures"] == 2
    assert sources["retry"]["last_page_received_at"] and sources["retry"]["next_check_at"]
    assert sources["retry"]["auto_release"]
    assert sources["retry"]["release_network_id"] == "notices.test"

    config.write_text("{malformed")
    invalid = client.get("/providers/example/scheduled-sources")
    assert invalid.status_code == 503
    assert "malformed" not in invalid.text


def test_scheduled_profile_checkpoint_depends_on_resolved_template(tmp_path, monkeypatch):
    profile = {
        "provider": "example", "source_key": "reports", "host": "reports.example.test",
        "url": "https://reports.example.test/feed",
    }
    mapping = {"fields": {"external_id": "/id", "title": "/name", "text": "/body"}}
    template_file = tmp_path / "templates.json"
    monkeypatch.setenv("KNOWLEDGE_INTAKE_TEMPLATES_FILE", str(template_file))
    template_file.write_text(json.dumps({"reports": mapping}))

    inline = scheduled_intake.PullProfile.model_validate({**profile, "mapping": mapping})
    old_profile = inline.model_dump(mode="json", exclude={"intake_template", "release_network_id"})
    assert inline.identity[2] == scheduled_intake.canonical_json_sha256(old_profile)
    named = scheduled_intake.PullProfile.model_validate({**profile, "intake_template": "reports"})
    assert named.identity == inline.identity
    with pytest.raises(ValueError, match="Choose an intake template or supply a mapping"):
        scheduled_intake.PullProfile.model_validate({**profile, "intake_template": "reports", "mapping": mapping})

    mapping["fields"]["text"] = "/fullText"
    template_file.write_text(json.dumps({"reports": mapping}))
    changed = scheduled_intake.PullProfile.model_validate({**profile, "intake_template": "reports"})
    assert changed.identity != named.identity
    with pytest.raises(ValueError, match="requires automatic release"):
        scheduled_intake.PullProfile.model_validate({**profile, "release_network_id": "notices.test"})


@pytest.mark.db
@pytest.mark.asyncio
async def test_scheduled_pull_resumes_cursor_after_failure_without_duplicate_receipts(db_connection, monkeypatch, mock_object_store):
    db = db_connection
    monkeypatch.setattr(scheduled_intake, "open_object_store", lambda _: mock_object_store)
    db.get_or_create_instance("bv")
    profile = scheduled_intake.PullProfile.model_validate({
        "provider": "bv", "source_key": "reports", "host": "reports.example.test",
        "url": "https://reports.example.test/feed", "next_cursor_pointer": "/next",
        "mapping": {"records": "/items", "fields": {"external_id": "/id", "title": "/name", "text": "/body"}},
    })
    calls = []

    async def fetch(url, hosts, authorization):
        assert hosts == {"reports.example.test"}
        calls.append(url)
        if len(calls) == 2:
            raise SourcePageError("The source timed out")
        second_page = "cursor=page-2" in url
        payload = {
            "next": None if second_page else "page-2",
            "items": [{"id": "2" if second_page else "1", "name": "Report", "body": "Full report text"}],
        }
        return SourceFile("feed.json", "application/json", json.dumps(payload).encode(), profile.url)

    monkeypatch.setattr(scheduled_intake, "fetch_source", fetch)

    def state():
        with db.get_connection() as conn:
            return conn.execute("SELECT cursor, failure_count FROM scheduled_source_pulls").fetchone()

    assert await scheduled_intake.poll_source(profile)
    assert state() == {"cursor": "page-2", "failure_count": 0}
    assert not await scheduled_intake.poll_source(profile)
    assert state() == {"cursor": "page-2", "failure_count": 1}
    assert not await scheduled_intake.poll_source(profile)
    with db.get_connection() as conn:
        conn.execute("UPDATE scheduled_source_pulls SET next_run_at = now()")
    assert await scheduled_intake.poll_source(profile)
    assert state() == {"cursor": None, "failure_count": 0}
    assert calls == [profile.url, profile.url + "?cursor=page-2", profile.url + "?cursor=page-2"]
    with db.get_connection() as conn:
        receipts = conn.execute(
            "SELECT external_id FROM source_intake_receipts WHERE source_id = 'reports' ORDER BY external_id"
        ).fetchall()
        conn.execute("UPDATE scheduled_source_pulls SET next_run_at = now()")
    assert receipts == [{"external_id": "1"}, {"external_id": "2"}]

    assert await scheduled_intake.poll_source(profile)
    with db.get_connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM source_intake_receipts").fetchone()["n"] == 2


@pytest.mark.db
@pytest.mark.asyncio
async def test_scheduled_pdf_registers_real_file_and_deduplicates_unchanged_content(
    db_connection, monkeypatch, mock_object_store, sample_pdf_content,
):
    db = db_connection
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "scheduled-review")
    db.get_or_create_instance("bv")
    profile = scheduled_intake.PullProfile(
        provider="bv", source_key="bulletin", host="reports.example.test",
        url="https://reports.example.test/bulletin.pdf",
    )

    async def fetch(*_):
        return SourceFile("bulletin.pdf", "application/pdf", sample_pdf_content, profile.url)

    monkeypatch.setattr(scheduled_intake, "fetch_source", fetch)
    monkeypatch.setattr(scheduled_intake, "open_object_store", lambda _: mock_object_store)
    assert await scheduled_intake.poll_source(profile)
    with db.get_connection() as conn:
        receipt = conn.execute("SELECT document_id FROM source_intake_receipts").fetchone()
        conn.execute("UPDATE scheduled_source_pulls SET next_run_at = now()")
    document = db.get_document(str(receipt["document_id"]))
    job = db.get_latest_document_job(document["id"])
    assert document["filename"] == "bulletin.pdf"
    assert document["filepath"].startswith("blob://documents/scheduled/bv/bulletin/")
    assert document["canonical_input_type"] == "pdf"
    assert job["config"]["upload_start_args"][2] == document["filepath"]
    assert job["config"]["upload_start_args"][6] == "scheduled-review"
    assert job["config"]["source_url"] == profile.url
    assert db.list_document_artifacts(document["id"])[0]["artifact_type"] == "original_upload"
    assert await scheduled_intake.poll_source(profile)
    with db.get_connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM source_intake_receipts").fetchone()["n"] == 1
