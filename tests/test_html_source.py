from unittest.mock import AsyncMock

import httpx
import pytest

from pipeline.intake import source_fetch as html_source


@pytest.mark.asyncio
async def test_fetch_pins_address_but_preserves_tls_name_and_extracts_source(monkeypatch):
    def respond(request):
        assert request.url.host == "93.184.216.34"
        assert request.extensions["sni_hostname"] == "notices.example"
        assert request.headers["Host"] == "notices.example"
        return httpx.Response(
            200, headers={"Content-Type": "text/html; charset=UTF-8"},
            stream=httpx.ByteStream(
                '<html lang="hi-IN"><head><title>फसल नोटिस</title></head>'
                '<body><nav>Navigation</nav><main><p>फसल की सलाह</p>'
                '<script>unrendered()</script><p>दूसरा पैराग्राफ</p></main></body></html>'.encode()
            ),
        )

    monkeypatch.setattr(html_source, "_resolve_public_address", lambda host: "93.184.216.34")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    result = await html_source.fetch_html_source("https://notices.example/notice?id=123", {"notices.example"})
    assert result.language == "hi-in"
    assert result.title == "फसल नोटिस"
    assert result.text == "फसल की सलाह\n\nदूसरा पैराग्राफ"
    assert result.attribution_url == "https://notices.example/notice"


@pytest.mark.parametrize("url", ["http://notices.example", "https://other.example", "https://notices.example:8443"])
@pytest.mark.asyncio
async def test_source_policy_is_checked_before_resolution(monkeypatch, url):
    def unexpected_resolution(host):
        pytest.fail("No DNS request should be sent for a URL outside the configured source policy")

    monkeypatch.setattr(html_source, "_resolve_public_address", unexpected_resolution)
    with pytest.raises(html_source.SourcePageError):
        await html_source.fetch_html_source(url, {"notices.example"})


@pytest.mark.db
def test_crawl_creates_one_idempotent_document_job(test_client, db_connection, mock_temporal_client, monkeypatch):
    from pipeline.api.routers import crawl

    db_connection.get_or_create_instance("bv")
    monkeypatch.setenv("CRAWL_SOURCE_PROFILES", '{"bv":{"notices":{"host":"notices.example"}}}')
    monkeypatch.setattr(crawl, "fetch_source", AsyncMock(return_value=html_source.SourcePage(
        "Cotton bulletin", "This advisory explains how to check the cotton crop after rain.", "en",
        "https://notices.example/bulletin",
    )))
    path = "/providers/bv/crawl/notices"
    request = {"url": "https://notices.example/bulletin", "external_id": "bulletin-1"}
    first = test_client.post(path, json=request)
    second = test_client.post(path, json=request)
    assert first.status_code == second.status_code == 202
    assert first.json()["document_id"] == second.json()["document_id"]
    assert first.json()["workflow_id"] == second.json()["workflow_id"]
    assert first.json()["source_url"] == "https://notices.example/bulletin"
    assert db_connection.get_document(first.json()["workflow_id"])["name"] == "Cotton bulletin"
    assert mock_temporal_client.start_workflow.call_count == 1


@pytest.mark.db
def test_crawl_pdf_uses_upload_and_keeps_source_attribution(
    test_client, db_connection, mock_temporal_client, sample_pdf_content, monkeypatch,
):
    from pipeline.api.routers import crawl

    db_connection.get_or_create_instance("bv")
    monkeypatch.setenv("CRAWL_SOURCE_PROFILES", '{"bv":{"notices":{"host":"notices.example"}}}')
    monkeypatch.setattr(crawl, "fetch_source", AsyncMock(return_value=html_source.SourceFile(
        "notice.pdf", "application/pdf", sample_pdf_content, "https://notices.example/notice.pdf",
    )))
    url = "/providers/bv/crawl/notices"
    body = {"url": "https://notices.example/notice.pdf?version=1", "external_id": "notice"}
    first = test_client.post(url, json=body)
    second = test_client.post(url, json=body)
    assert first.status_code == second.status_code == 202, first.text
    assert first.json()["workflow_id"] == second.json()["workflow_id"]
    job = db_connection.get_latest_document_job(first.json()["document_id"])
    assert job["config"]["source_url"] == "https://notices.example/notice.pdf"
    assert next(iter(job["config"]["private_source_responses"].values()))["size_bytes"] == len(sample_pdf_content)
    assert mock_temporal_client.start_workflow.call_count == 1


@pytest.mark.db
def test_crawl_json_uses_the_named_intake_template(test_client, db_connection, monkeypatch, tmp_path):
    import json

    from pipeline.api.routers import crawl
    from pipeline.intake.source_intake import materialize_intake_pages

    db_connection.get_or_create_instance("bv")
    templates = tmp_path / "templates.json"
    templates.write_text(json.dumps({
        "notices": {"fields": {"external_id": "/id", "title": "/name", "text": "/body"}},
    }))
    monkeypatch.setenv("KNOWLEDGE_INTAKE_TEMPLATES_FILE", str(templates))
    monkeypatch.setenv("CRAWL_SOURCE_PROFILES", json.dumps({
        "bv": {"notices": {"host": "notices.example", "intake_template": "notices"}},
    }))
    raw = {"id": "1", "name": "Notice", "body": "The full notice text.", "upstreamVersion": 1}
    url = "https://notices.example/notice.json"

    async def fetch(*_):
        return html_source.SourceFile("notice.json", "application/json", json.dumps(raw).encode(), url)

    monkeypatch.setattr(crawl, "fetch_source", fetch)
    endpoint = "/providers/bv/crawl/notices"
    response = test_client.post(endpoint, json={"url": url})
    assert response.status_code == 202, response.text
    item = response.json()
    job = db_connection.get_latest_document_job(item["document_id"])
    materialize_intake_pages(item["workflow_id"], job["config"]["upload_start_args"][2])
    assert db_connection.get_pages(item["document_id"])[0]["original_markdown"] == "The full notice text."
    with db_connection.get_connection() as conn:
        saved = conn.execute(
            "SELECT raw_payload, normalized_document FROM source_intake_receipts WHERE id = %s", (item["receipt_id"],),
        ).fetchone()
    assert saved["raw_payload"] == raw
    assert saved["normalized_document"]["text"] == raw["body"]
    assert "upstreamVersion" not in saved["normalized_document"]

    replayed = test_client.post(endpoint, json={"url": url})
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["receipt_id"] == item["receipt_id"]
    with monkeypatch.context() as release_policy:
        release_policy.setattr(crawl, "upload_release_target", lambda _, **kwargs: ("dev", "published"))
        conflict = test_client.post(endpoint, json={"url": url})
    assert conflict.status_code == 409, conflict.text
    assert "different release policy" in conflict.json()["detail"]

    db_connection.update_document_job(job["id"], status="completed")
    raw["upstreamVersion"] = 2
    updated = test_client.post(endpoint, json={"url": url})
    assert updated.status_code == 202, updated.text
    assert updated.json()["document_id"] == item["document_id"]
    assert updated.json()["receipt_id"] != item["receipt_id"]


@pytest.mark.db
def test_json_crawl_reuses_a_legacy_normalized_receipt(test_client, db_connection, monkeypatch, tmp_path):
    import json

    from pipeline.api.routers import crawl
    from pipeline.intake.source_intake import (
        NormalizedSourceDocument,
        canonical_json_sha256,
        register_source_document,
    )

    db_connection.get_or_create_instance("bv")
    templates = tmp_path / "templates.json"
    templates.write_text(json.dumps({
        "notices": {"fields": {"external_id": "/external_id", "title": "/title", "text": "/text"}},
    }))
    monkeypatch.setenv("KNOWLEDGE_INTAKE_TEMPLATES_FILE", str(templates))
    monkeypatch.setenv("CRAWL_SOURCE_PROFILES", json.dumps({
        "bv": {"notices": {"host": "notices.example", "intake_template": "notices"}},
    }))
    url = "https://notices.example/notice.json"
    payload = {"external_id": "1", "title": "Notice", "text": "Full notice text"}
    document = NormalizedSourceDocument(**payload, source_url=url)
    normalized = document.model_dump(mode="json", exclude_none=True)
    with db_connection.get_connection() as conn:
        old = register_source_document(
            conn, instance="bv", source_kind="crawl", source_id="notices", document=document,
            idempotency_key=canonical_json_sha256(normalized), raw_payload=normalized, release_target=None,
        )
    raw = {**payload, "upstreamVersion": 1}

    async def fetch(*_):
        return html_source.SourceFile("notice.json", "application/json", json.dumps(raw).encode(), url)

    monkeypatch.setattr(crawl, "fetch_source", fetch)
    endpoint = "/providers/bv/crawl/notices"
    for _ in range(2):
        replayed = test_client.post(endpoint, json={"url": url})
        assert replayed.status_code == 202, replayed.text
        assert replayed.json()["receipt_id"] == old["id"]
    with db_connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM source_intake_receipts").fetchone()["n"] == 1

    db_connection.update_document_job(old["job_id"], status="completed")
    raw["upstreamVersion"] = 2
    updated = test_client.post(endpoint, json={"url": url})
    assert updated.status_code == 202, updated.text
    assert updated.json()["document_id"] == old["document_id"]
    assert updated.json()["receipt_id"] != old["id"]
    with db_connection.get_connection() as conn:
        saved = conn.execute(
            "SELECT raw_payload AS legacy_payload, replay_evidence_payload AS observed_payload "
            "FROM source_intake_receipts WHERE id = %s",
            (old["id"],),
        ).fetchone()
    assert saved["legacy_payload"] == normalized
    assert saved["observed_payload"] == {**payload, "upstreamVersion": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type,body", [
    ("text/html", b"<html><body><p>Notice</p></body></html>\r\n"),
    ("text/plain", b"  Notice\r\n"),
    ("application/json", b'{ "external_id": "1", "title":"Notice", "text":"Advice" }\n'),
    ("application/pdf", b"%PDF-1.4\nfixture"),
])
async def test_public_url_retains_exact_bytes_without_configured_host(monkeypatch, media_type, body):
    def respond(request):
        assert "authorization" not in request.headers
        assert request.url.host == "93.184.216.34"
        return httpx.Response(200, headers={"content-type": media_type}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(html_source, "_resolve_public_address", lambda host: "93.184.216.34")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    result = await html_source.fetch_source("https://notices.example/notice")
    assert result.content == body
    assert result.media_type == media_type


@pytest.mark.asyncio
async def test_authenticated_fetch_requires_saved_host_policy(monkeypatch):
    monkeypatch.setattr(html_source, "_read_source", AsyncMock())
    with pytest.raises(html_source.SourcePageError, match="saved host policy"):
        await html_source.fetch_source("https://notices.example/notice", authorization="fixture-only")
    html_source._read_source.assert_not_called()


@pytest.mark.asyncio
async def test_public_source_redirect_is_not_followed(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://another.example/notice"})

    monkeypatch.setattr(html_source, "_resolve_public_address", lambda host: "93.184.216.34")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(respond))
    with pytest.raises(html_source.SourcePageError):
        await html_source.fetch_source("https://notices.example/notice")
    assert len(calls) == 1


def test_raw_response_is_private_and_job_output_omits_capture_metadata():
    import hashlib
    import json
    from unittest.mock import Mock

    from pipeline.intake.source_intake import public_source_job, store_source_response

    raw = b"<html><body>Notice</body></html>\r\n"
    source = html_source.extract_text_source(raw, "text/html", "Notice")
    store = Mock()
    capture = store_source_response(store, "documents", "example", source)
    ref, data, size, media_type = store.put.call_args.args
    assert data.getvalue() == raw
    assert size == len(raw)
    assert media_type == "application/octet-stream"
    assert ref.key.startswith("private/source-responses/")
    assert capture["sha256"] == hashlib.sha256(raw).hexdigest()
    config = {"private_source_responses": {capture["sha256"]: capture}, "source_url": "https://notices.example/notice"}
    public = public_source_job({"config": config, "config_json": json.dumps(config)})
    assert "private_source_responses" not in public["config"]
    assert "private_source_responses" not in public["config_json"]
    assert "private_source_responses" in config


@pytest.mark.db
def test_public_crawl_without_profiles_keeps_private_original(
    test_client, db_connection, mock_object_store, mock_temporal_client, monkeypatch,
):
    from pipeline.api.routers import crawl

    mock_temporal_client.get_workflow_handle.return_value.describe.side_effect = RuntimeError("No live Temporal in this test")

    db_connection.get_or_create_instance("bv")
    monkeypatch.delenv("CRAWL_SOURCE_PROFILES", raising=False)
    raw = b"<html><body><p>Cotton advisory after rain.</p></body></html>\r\n"
    source = html_source.extract_text_source(raw, "text/html", "Advisory", "https://notices.example/notice")
    fetch = AsyncMock(return_value=source)
    monkeypatch.setattr(crawl, "fetch_source", fetch)
    result = test_client.post("/providers/bv/crawl", json={"url": "https://notices.example/notice"})
    assert result.status_code == 202, result.text
    fetch.assert_awaited_once_with("https://notices.example/notice", None, None)
    assert mock_object_store.put.call_args.args[1].getvalue() == raw
    item = result.json()
    job = db_connection.get_latest_document_job(item["document_id"])
    captures = job["config"]["private_source_responses"]
    assert next(iter(captures.values()))["size_bytes"] == len(raw)
    assert not db_connection.list_document_artifacts(item["workflow_id"])
    jobs = test_client.get(f'/documents/{item["workflow_id"]}/jobs')
    assert jobs.status_code == 200
    assert "private_source_responses" not in jobs.text
    assert "private/source-responses" not in jobs.text
    document_path = f'/documents/{item["workflow_id"]}'
    for endpoint in ("/runs", f'/runs/{job["id"]}', document_path, f"{document_path}/graph", f"{document_path}/runtime"):
        response = test_client.get(endpoint)
        assert response.status_code == 200, response.text
        assert "private_source_responses" not in response.text
        assert "private/source-responses" not in response.text
