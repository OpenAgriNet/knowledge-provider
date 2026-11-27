"""Exercise the LiteParse CLI boundary without installing the optional binary."""

import json
import subprocess
from pathlib import Path

import pytest

from pipeline.documents.models import (
    PAGE_LAYOUT_ITEM_MAX_CHARS,
    PAGE_LAYOUT_MAX_ITEMS,
    PageMetadata,
)
from pipeline.ocr import liteparse, service
from pipeline.ocr.liteparse import LiteParseProvider


def _page(number, text):
    return {
        "page": number,
        "width": 595,
        "height": 842,
        "text": text,
        "text_items": [{"text": text, "x": 65, "y": 110, "width": 155, "height": 24}],
        "complexity": {"needs_ocr": True, "full_page_image": True},
    }


def _mock_parse(monkeypatch, page):
    def run(command, **kwargs):
        assert command[:4] == ["lit", "parse", "--format", "json"]
        assert "--no-ocr" in command
        assert command[-2] == "--"
        assert Path(command[-1]).is_absolute()
        assert set(kwargs["env"]) == {"PATH"}
        assert kwargs["check"] and kwargs["timeout"] > 0
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text(json.dumps({"pages": [page]}), encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.unit
def test_explicit_selection_extracts_embedded_text_and_validates_layout(monkeypatch, temp_pdf_file):
    text = "કપાસના ખેતરની સંભાળ"
    _mock_parse(monkeypatch, _page(1, text))
    monkeypatch.setenv("OCR_PROVIDER", "pypdf")
    monkeypatch.setenv("OCR_SKIP_TEXT_LAYER", "true")
    monkeypatch.setattr(service, "split_pdf_range", lambda *a, **k: pytest.fail("text shortcut ran"))

    page, = service.ocr_pdf(str(temp_pdf_file), str.strip, provider="liteparse")

    assert page["original_markdown"] == text
    layout = PageMetadata.model_validate(page["metadata"]).source_layout
    assert layout.parser == "liteparse"
    assert layout.text_items[0].text == text
    assert layout.text_items[0].x == 65
    assert layout.ocr_recommended is True


@pytest.mark.unit
def test_source_layout_limit_preserves_untruncated_page_text(monkeypatch, temp_pdf_file):
    text = "x" * (PAGE_LAYOUT_ITEM_MAX_CHARS + 1)
    raw = _page(2, text)
    raw["text_items"] *= PAGE_LAYOUT_MAX_ITEMS + 1
    _mock_parse(monkeypatch, raw)
    parser = LiteParseProvider(service.load_ocr_config("liteparse"))

    page, = parser.process_pdf_range(str(temp_pdf_file), 1, 2)

    assert page["page_number"] == 2
    assert page["original_markdown"] == text
    layout = page["metadata"]["source_layout"]
    assert layout["truncated"] is True
    assert len(layout["text_items"]) == PAGE_LAYOUT_MAX_ITEMS
    assert len(layout["text_items"][0]["text"]) == PAGE_LAYOUT_ITEM_MAX_CHARS


@pytest.mark.unit
@pytest.mark.parametrize(("raw", "error"), [
    (_page(2, "unexpected page"), "invalid page data"),
    (_page(1, ""), "Select an OCR provider"),
])
def test_liteparse_rejects_missing_or_unusable_page(monkeypatch, temp_pdf_file, raw, error):
    _mock_parse(monkeypatch, raw)
    parser = LiteParseProvider(service.load_ocr_config("liteparse"))

    with pytest.raises(RuntimeError, match=error):
        parser.process_pdf_range(str(temp_pdf_file), 0, 1)


@pytest.mark.unit
def test_liteparse_timeout_returns_safe_error(monkeypatch, temp_pdf_file):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    parser = LiteParseProvider(service.load_ocr_config("liteparse"))

    with pytest.raises(RuntimeError, match="configured parser timeout"):
        parser.process_pdf_range(str(temp_pdf_file), 0, 1)


@pytest.mark.unit
def test_liteparse_bounds_decoded_output(monkeypatch, temp_pdf_file):
    _mock_parse(monkeypatch, _page(1, "Source text"))
    monkeypatch.setattr(liteparse, "_MAX_OUTPUT_BYTES", 10)
    parser = LiteParseProvider(service.load_ocr_config("liteparse"))

    with pytest.raises(RuntimeError, match="output exceeded the size limit"):
        parser.process_pdf_range(str(temp_pdf_file), 0, 1)


@pytest.mark.db
def test_liteparse_layout_survives_page_round_trip(monkeypatch, temp_pdf_file, db_connection, sample_document):
    _mock_parse(monkeypatch, _page(1, "Source text"))
    page, = service.ocr_pdf(str(temp_pdf_file), str.strip, provider="liteparse")
    workflow_id = sample_document["workflow_id"]

    db_connection.save_pages(workflow_id, [page])
    persisted = db_connection.get_page(workflow_id, 1)
    persisted["translations"]["en"] = {"translated_markdown": "Translated text"}
    db_connection.save_pages(workflow_id, [persisted])

    updated = db_connection.get_page(workflow_id, 1)
    assert updated["metadata"]["source_layout"] == page["metadata"]["source_layout"]
    assert updated["translations"]["en"]["translated_markdown"] == "Translated text"
