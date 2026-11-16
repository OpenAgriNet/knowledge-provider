from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pypdf import PdfWriter
from temporalio.testing import ActivityEnvironment

from pipeline.documents import document_formats
from pipeline.intake import source_fetch as html_source
from pipeline.temporal.activities import source as activities


@pytest.fixture
def extra_format(monkeypatch):
    def normalize(input_path, output_path):
        raw = Path(input_path).read_bytes()
        assert raw.startswith(b"FORMAT1\n")
        Path(output_path).write_bytes(raw.removeprefix(b"FORMAT1\n"))

    definition = document_formats.DocumentFormat(".fixture", "application/vnd.example.fixture", normalize)
    entry_point = Mock()
    entry_point.load.return_value = definition
    monkeypatch.setattr(document_formats, "entry_points", lambda **_: (entry_point,))
    monkeypatch.setenv("DOCUMENT_FORMAT_EXTENSIONS", "test-fixture")
    document_formats._load_formats.cache_clear()
    yield definition
    document_formats._load_formats.cache_clear()


@pytest.mark.db
def test_configured_format_is_accepted_by_upload_and_crawl(test_client, db_connection, extra_format, monkeypatch):
    media_type = extra_format.media_type
    assert ".fixture" in test_client.get("/upload/options").json()["file_extensions"]
    uploaded = test_client.post(
        "/upload?kind=advisory", files={"file": ("one.fixture", b"FORMAT1\n%PDF-1.7\n", media_type)},
    )
    assert uploaded.status_code == 200, uploaded.text

    db_connection.get_or_create_instance("bv")
    monkeypatch.setenv("CRAWL_SOURCE_PROFILES", '{"bv":{"notices":{"host":"notices.example"}}}')
    monkeypatch.setattr(html_source, "_resolve_public_address", lambda _: "93.184.216.34")

    def response(_):
        return httpx.Response(200, headers={"Content-Type": media_type}, stream=httpx.ByteStream(b"FORMAT1\n%PDF-1.7\n"))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(response))
    crawled = test_client.post("/providers/bv/crawl/notices", json={"url": "https://notices.example/two"})
    assert crawled.status_code == 202, crawled.text
    document = db_connection.get_document(crawled.json()["document_id"])
    assert document["filename"] == "two.fixture"
    assert document["canonical_input_type"] == "pdf"


@pytest.mark.db
@pytest.mark.asyncio
async def test_worker_normalizes_extension_before_document_parser(test_client, db_connection, extra_format, tmp_path, monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = BytesIO()
    writer.write(pdf)
    source = tmp_path / "notice.fixture"
    source.write_bytes(b"FORMAT1\n" + pdf.getvalue())
    uploaded = test_client.post(
        "/upload?kind=advisory", files={"file": (source.name, source.read_bytes(), extra_format.media_type)},
    )
    assert uploaded.status_code == 200, uploaded.text
    workflow_id = uploaded.json()["workflow_id"]
    document = db_connection.get_document(workflow_id)
    normalized_paths = []

    def save_artifact(filename, _, kind, display_name):
        if kind == "normalized_pdf":
            path = Path(filename)
            assert path.read_bytes() == pdf.getvalue()
            normalized_paths.append(path)
        return f"blob://docs/{display_name}", Path(filename).stat().st_size, "application/octet-stream"

    monkeypatch.setenv("DOCUMENT_PARSER_TYPE", "mock")
    monkeypatch.setattr(activities, "_resolve_local_path", lambda _: (str(source), False))
    monkeypatch.setattr(activities, "_upload_artifact", save_artifact)
    result = await ActivityEnvironment().run(activities.run_ocr_and_store, workflow_id, document["filepath"])
    assert result["page_count"] == 1
    assert len(db_connection.get_pages(workflow_id)) == 1
    assert len(normalized_paths) == 1 and not normalized_paths[0].exists()


def test_formats_cannot_replace_builtin_media_type(monkeypatch):
    entry_point = Mock()
    entry_point.load.return_value = document_formats.DocumentFormat(".other", "application/pdf", Mock())
    monkeypatch.setattr(document_formats, "entry_points", lambda **_: (entry_point,))
    monkeypatch.setenv("DOCUMENT_FORMAT_EXTENSIONS", "duplicate-type")
    with pytest.raises(ValueError, match="invalid or duplicate media type"):
        document_formats.allowed_file_extensions()
