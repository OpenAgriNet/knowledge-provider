import pytest


@pytest.mark.api
@pytest.mark.db
def test_upload_pins_only_an_enabled_parser(
    test_client, db_connection, mock_temporal_client, sample_pdf_content, monkeypatch,
):
    monkeypatch.setenv("OCR_ALLOWED_PROVIDERS", "mock,liteparse")
    for parser, expected_status in (("mistral", 422), ("liteparse", 200)):
        response = test_client.post(
            "/upload", params={"kind": "advisory"}, data={"parser_provider": parser},
            files={"file": ("parser-choice.pdf", sample_pdf_content, "application/pdf")},
        )
        assert response.status_code == expected_status

    config = db_connection.get_latest_document_job(response.json()["workflow_id"])["config"]
    assert config["parser_provider"] == "liteparse"
    assert config["upload_start_args"][-2:] == [None, "liteparse"]
    assert mock_temporal_client.start_workflow.call_count == 1


@pytest.mark.api
@pytest.mark.db
def test_batch_starts_independent_workflows_and_reports_bad_pdf(
    test_client, db_connection, mock_temporal_client, sample_pdf_content,
):
    observed = []

    async def start_workflow(*args, **kwargs):
        workflow_id = kwargs["id"]
        assert db_connection.get_document(workflow_id) is not None
        assert db_connection.get_latest_document_job(workflow_id) is not None
        observed.append(workflow_id)

    mock_temporal_client.start_workflow.side_effect = start_workflow
    response = test_client.post(
        "/upload/batch",
        params={"kind": "advisory", "instance": "bv"},
        files=[
            ("files", ("rice.pdf", sample_pdf_content, "application/pdf")),
            ("files", ("broken.pdf", b"not a PDF", "application/pdf")),
            ("files", ("cotton.pdf", sample_pdf_content, "application/pdf")),
        ],
    )
    assert response.status_code == 207
    items = response.json()["items"]
    assert [item["status"] for item in items] == [202, 400, 202]
    ids = [item["document"]["workflow_id"] for item in items if item["document"]]
    assert ids == observed
    assert len(set(ids)) == 2
    assert all(db_connection.get_document(id_)["instance"] == "bv" for id_ in ids)


@pytest.mark.api
@pytest.mark.db
def test_index_only_upload_uses_deployment_collection_without_review_or_release(
    test_client, db_connection, sample_pdf_content, monkeypatch,
):
    monkeypatch.setenv("KP_INDEX_ONLY", "true")
    monkeypatch.setenv("VECTOR_DB_COLLECTION_NAME", "all-documents")
    monkeypatch.setenv("ENABLE_PUBLISH_TO_NETWORK", "true")
    monkeypatch.setenv("AUTO_RELEASE_NETWORK_ID", "unconfigured-network")

    response = test_client.post(
        "/upload", params={"kind": "advisory", "index_name": "ignored"},
        files={"file": ("index-only.pdf", sample_pdf_content, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    config = db_connection.get_latest_document_job(response.json()["workflow_id"])["config"]
    assert config["upload_start_args"][6:] == ["all-documents", True, False, True, None, None, True]
    assert not config.get("release_network_id")


@pytest.mark.api
@pytest.mark.db
def test_automatic_release_uses_server_destination(
    test_client, db_connection, mock_temporal_client, sample_pdf_content, monkeypatch,
):
    monkeypatch.setenv("ENABLE_PUBLISH_TO_NETWORK", "true")
    monkeypatch.setenv("DISCOVERY_NETWORK_ID", "oan-dev.local")
    monkeypatch.setenv("AUTO_RELEASE_NETWORK_ID", "oan-dev.local")
    monkeypatch.setenv("AUTO_RELEASE_COLLECTION", "published-advisories")

    response = test_client.post(
        "/upload", params={"kind": "advisory", "index_name": "caller-selected", "auto_approve": "false"},
        files={"file": ("policy.pdf", sample_pdf_content, "application/pdf")},
    )

    assert response.status_code == 200
    workflow_id = response.json()["workflow_id"]
    config = db_connection.get_latest_document_job(workflow_id)["config"]
    args = mock_temporal_client.start_workflow.call_args.kwargs["args"]
    assert args == config["upload_start_args"]
    assert args[6:8] == ["published-advisories", True]
    assert args[-1] == config["release_network_id"] == "oan-dev.local"
    db_connection.update_document_stage(workflow_id, "completed")
    assert test_client.post(f"/documents/{workflow_id}/approve-prod").status_code == 409
    assert mock_temporal_client.start_workflow.call_count == 1


@pytest.mark.api
@pytest.mark.db
def test_oversized_file_count_does_not_start_partial_batch(
    test_client, mock_temporal_client, sample_pdf_content,
):
    response = test_client.post(
        "/upload/batch",
        params={"kind": "advisory"},
        files=[
            ("files", (f"part-{i}.pdf", sample_pdf_content, "application/pdf"))
            for i in range(11)
        ],
    )
    assert response.status_code == 413
    mock_temporal_client.start_workflow.assert_not_called()


@pytest.mark.api
@pytest.mark.db
def test_retry_uncertain_start_uses_the_original_document_and_workflow(
    test_client, db_connection, mock_temporal_client, sample_pdf_content,
):
    from temporalio.exceptions import WorkflowAlreadyStartedError

    mock_temporal_client.start_workflow.side_effect = TimeoutError("start reply lost")
    upload = test_client.post(
        "/upload/batch",
        params={"kind": "advisory", "instance": "bv"},
        files=[("files", ("test.pdf", sample_pdf_content, "application/pdf"))],
    )
    item, = upload.json()["items"]
    assert item["status"] == 503
    workflow_id = item["document"]["workflow_id"]
    assert db_connection.get_latest_document_job(workflow_id)["status"] == "start_pending"

    mock_temporal_client.start_workflow.side_effect = WorkflowAlreadyStartedError(
        workflow_id, "DocumentPipelineWorkflow",
    )
    assert test_client.post(f"/upload/{workflow_id}/start").status_code == 202
    assert db_connection.get_latest_document_job(workflow_id)["status"] == "running"
    calls = mock_temporal_client.start_workflow.call_args_list
    assert [call.kwargs["id"] for call in calls] == [workflow_id, workflow_id]
    assert calls[0].kwargs["args"] == calls[1].kwargs["args"]
    assert test_client.post(f"/upload/{workflow_id}/start").status_code == 202
    assert mock_temporal_client.start_workflow.call_count == 2


@pytest.mark.api
@pytest.mark.db
def test_failed_registration_rolls_back_database_rows(
    test_client, db_connection, mock_temporal_client, sample_pdf_content, monkeypatch,
):
    def failed_artifact(*args, **kwargs):
        raise RuntimeError("artifact registration failed")

    monkeypatch.setattr(db_connection, "add_document_artifact", failed_artifact)
    response = test_client.post(
        "/upload/batch",
        params={"kind": "advisory", "instance": "bv"},
        files=[("files", ("rolled-back.pdf", sample_pdf_content, "application/pdf"))],
    )
    assert response.json()["items"][0]["status"] == 500
    with db_connection.get_connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM workflows").fetchone()["n"] == 0
    mock_temporal_client.start_workflow.assert_not_called()


@pytest.mark.db
def test_batch_accepts_supported_mixed_file_formats(test_client, sample_pdf_content):
    response = test_client.post(
        "/upload/batch?kind=advisory",
        files=[
            ("files", ("notice.pdf", sample_pdf_content, "application/pdf")),
            ("files", ("measurements.csv", b"label,value\nreading,20\n", "text/csv")),
        ],
    )
    assert response.status_code == 207, response.text
    assert [item["status"] for item in response.json()["items"]] == [202, 202]


@pytest.mark.db
@pytest.mark.asyncio
async def test_canonical_json_upload_preserves_submitted_text_and_translation(
    test_client, db_connection, tmp_path, monkeypatch,
):
    import json

    from pipeline.temporal.activities import source as activities

    body = {
        "external_id": "notice-8", "title": "Field notice", "kind": "advisory",
        "text": "Keep the entire notice.", "language": "en",
        "translations": {
            "hi": {"text": "पूरी सूचना रखें।", "title": "सूचना", "description": "पूरा विवरण"},
            "fr": "Conservez l’avis en entier.",
        },
        "knowledge_metadata": {"subject_categories": ["Crop"]},
    }
    path = tmp_path / "notice.json"
    path.write_text(json.dumps(body))
    response = test_client.post(
        "/upload?kind=advisory", files={"file": (path.name, path.read_bytes(), "application/json")},
    )
    assert response.status_code == 200, response.text
    workflow_id = response.json()["workflow_id"]
    monkeypatch.setattr(activities, "_resolve_local_path", lambda _: (str(path), False))
    monkeypatch.setattr(activities, "_upload_artifact", lambda *args: (
        "blob://docs/normalized.txt", 1, "text/plain",
    ))
    await activities.run_ocr_and_store(workflow_id, db_connection.get_document(workflow_id)["filepath"])
    page, = db_connection.get_pages(workflow_id)
    assert page["original_markdown"] == body["text"]
    assert page["translations"]["hi"]["translated_markdown"] == body["translations"]["hi"]["text"]
    assert page["translations"]["fr"]["translated_markdown"] == body["translations"]["fr"]
    doc = db_connection.get_document(workflow_id)
    assert doc["name"] == body["title"]
    assert doc["knowledge_metadata"] == {
        **body["knowledge_metadata"], "localized": {"hi": {"title": "सूचना", "description": "पूरा विवरण"}},
    }
    assert doc["canonical_input_type"] == "structured_text"


@pytest.mark.db
def test_uploaded_html_download_is_not_rendered_inline(test_client, db_connection):
    response = test_client.post(
        "/upload?kind=advisory",
        files={"file": ("notice.html", b"<html><body><p>Notice</p></body></html>", "text/html")},
    )
    assert response.status_code == 200, response.text
    workflow_id = response.json()["workflow_id"]
    artifact = next(a for a in db_connection.list_document_artifacts(workflow_id) if a["artifact_type"] == "original_upload")
    download = test_client.get(f"/documents/{workflow_id}/artifacts/{artifact['id']}/content")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["x-content-type-options"] == "nosniff"


@pytest.mark.api
@pytest.mark.db
def test_scheme_matches_accepts_existing_code_and_alias(test_client, db_connection):
    db_connection.upsert_document(
        workflow_id="scheme-match", document_id="c" * 32, filename="guide.pdf", filepath="minio://documents/guide.pdf",
        instance="bv", kind="scheme", code="example-code", name="Assistance guide",
    )
    db_connection.update_document_fields("scheme-match", aliases=["Different label"])
    for name in ("example-code", "Different label"):
        response = test_client.get("/upload/scheme-matches", params={"instance": "bv", "name": name})
        assert response.status_code == 200, response.text
        assert [row["code"] for row in response.json()["matches"]] == ["example-code"]
