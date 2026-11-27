import pytest

from pipeline.ocr import service


def test_default_and_named_parser_profiles(monkeypatch):
    monkeypatch.setenv("OCR_PROVIDER", "chandra")
    monkeypatch.setenv("DOCUMENT_PARSER_PROFILES", "text,layout")
    monkeypatch.setenv("DOCUMENT_PARSER_DEFAULT", "layout")
    monkeypatch.setenv("DOCUMENT_PARSER_TEXT_TYPE", "pypdf")
    monkeypatch.setenv("DOCUMENT_PARSER_LAYOUT_TYPE", "liteparse")
    monkeypatch.setenv("DOCUMENT_PARSER_LAYOUT_SEGMENT_PAGES", "5")
    monkeypatch.setenv("DOCUMENT_PARSER_LAYOUT_EXECUTABLE", "/usr/bin/lit")

    assert service.allowed_ocr_providers() == ("text", "layout")
    assert service.default_ocr_profile() == "layout"
    assert service.load_ocr_config().segment_pages == 5
    assert service.get_ocr_provider().executable == "/usr/bin/lit"
    assert service.load_ocr_config("text").provider == "pypdf"
    with pytest.raises(ValueError, match="not configured"):
        service.load_ocr_config("chandra")


def test_parser_rejects_other_provider_options_without_exposing_values(monkeypatch):
    monkeypatch.setenv("DOCUMENT_PARSER_TYPE", "pypdf")
    monkeypatch.setenv("DOCUMENT_PARSER_API_KEY", "fixture-secret")

    with pytest.raises(ValueError, match="extra_forbidden") as error:
        service.load_ocr_config()
    assert "fixture-secret" not in str(error.value)


def test_parser_reads_adapter_credential_from_file(monkeypatch, tmp_path):
    credential = tmp_path / "test-secret"
    credential.write_text("fixture-secret\n")
    monkeypatch.setenv("DOCUMENT_PARSER_TYPE", "mistral")
    monkeypatch.setenv("DOCUMENT_PARSER_API_KEY_FILE", str(credential))
    monkeypatch.setenv("DOCUMENT_PARSER_SKIP_TEXT_LAYER", "false")

    config = service.load_ocr_config()

    assert config.api_key == "fixture-secret"
    assert config.skip_text_layer is False
    assert "fixture-secret" not in repr(config)


def test_deployment_installed_extension_runs_as_a_named_pdf_parser(monkeypatch, tmp_path):
    from pypdf import PdfWriter

    from pipeline.ocr.service import allowed_ocr_providers, ocr_pdf

    distribution = tmp_path / "example_parser-1.0.dist-info"
    distribution.mkdir()
    (distribution / "entry_points.txt").write_text(
        "[knowledge_provider.document_parsers]\nexample-ocr = example_parser:make_parser\n"
    )
    (tmp_path / "example_parser.py").write_text(
        """from pipeline.ocr.base import OcrProvider

class ExampleParser(OcrProvider):
    name = "example-ocr"
    requires_all_pages = True

    def __init__(self, language):
        self.language = language

    def process_pdf_range(self, pdf_path, start_idx, end_idx, *, log=None):
        return [
            {"page_number": index + 1, "original_markdown": f"Parsed with {self.language}"}
            for index in range(start_idx, end_idx)
        ]

def make_parser(options):
    return ExampleParser(options["language"])
"""
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DOCUMENT_PARSER_PROFILES", "text,scan")
    monkeypatch.setenv("DOCUMENT_PARSER_DEFAULT", "text")
    monkeypatch.setenv("DOCUMENT_PARSER_TEXT_TYPE", "pypdf")
    monkeypatch.setenv("DOCUMENT_PARSER_SCAN_TYPE", "extension")
    monkeypatch.setenv("DOCUMENT_PARSER_SCAN_PLUGIN", "example-ocr")
    monkeypatch.setenv("DOCUMENT_PARSER_SCAN_OPTION_LANGUAGE", "en")
    path = tmp_path / "document.pdf"
    writer = PdfWriter()
    writer.add_blank_page(200, 100)
    writer.write(path)

    assert allowed_ocr_providers() == ("text", "scan")
    assert ocr_pdf(str(path), str.strip, provider="scan") == [
        {"page_number": 1, "original_markdown": "Parsed with en"},
    ]
