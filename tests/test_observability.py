import logging

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

from pipeline import observability


@pytest.fixture
def exported(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    observability.configure_logs(logger_provider=provider)
    yield exporter
    observability.reset_logs()
    root.setLevel(previous_level)


def _bodies(exporter):
    return [item.log_record.body for item in exporter.get_finished_logs()]


def _our_handlers():
    return [h for h in logging.getLogger().handlers if observability.is_otel_handler(h)]


def test_nothing_is_attached_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    observability.configure_logs()

    assert _our_handlers() == []


def test_an_endpoint_attaches_one_handler(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    observability.configure_logs()
    try:
        assert len(_our_handlers()) == 1
    finally:
        observability.reset_logs()


def test_info_lines_from_root_and_module_loggers_are_exported(exported):
    logging.info("search request query=%r", "cotton pest")
    logging.getLogger("pipeline.activities").warning("ocr retry")

    assert _bodies(exported) == ["search request query='cotton pest'", "ocr retry"]


def test_exported_line_keeps_its_level(exported):
    logging.getLogger("pipeline.activities").error("ingest failed")

    [item] = exported.get_finished_logs()
    assert item.log_record.severity_text == "ERROR"


def test_debug_lines_are_not_exported(exported):
    logging.getLogger("pipeline.activities").debug("page text")

    assert _bodies(exported) == []


def test_opentelemetry_own_lines_are_not_exported(exported):
    logging.getLogger("opentelemetry.exporter.otlp").warning("export failed")

    assert _bodies(exported) == []


def test_configuring_twice_keeps_one_handler(exported):
    observability.configure_logs(logger_provider=LoggerProvider())

    assert len(_our_handlers()) == 1


def test_reset_removes_the_handler(exported):
    observability.reset_logs()

    assert _our_handlers() == []
