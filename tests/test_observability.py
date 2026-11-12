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


def test_extras_are_exported_as_attributes(exported):
    # Attributes, not body text, are what HyperDX can filter on.
    logging.info("search request", extra={"query": "cotton pest", "top_k": 5})

    [item] = exported.get_finished_logs()
    assert item.log_record.body == "search request"
    assert item.log_record.attributes["query"] == "cotton pest"
    assert item.log_record.attributes["top_k"] == 5


def test_a_dict_extra_is_flattened_into_dotted_attributes(exported):
    # Temporal sends its context as one dict; flat keys let a search filter on workflow_id.
    logging.info("ocr done", extra={"temporal_activity": {"workflow_id": "wf-1", "attempt": 2}})

    [item] = exported.get_finished_logs()
    assert item.log_record.attributes["temporal_activity.workflow_id"] == "wf-1"
    assert item.log_record.attributes["temporal_activity.attempt"] == 2
    assert "temporal_activity" not in item.log_record.attributes


def test_a_non_primitive_extra_is_exported_as_text(exported):
    logging.info("publish rejected", extra={"errors": ["bad catalog"]})

    [item] = exported.get_finished_logs()
    assert item.log_record.attributes["errors"] == "['bad catalog']"


def test_console_prints_extras_as_key_value():
    formatter = observability.KeyValueFormatter("%(message)s")
    record = logging.makeLogRecord({"msg": "worker started", "queue": "ocr-pipeline"})

    assert formatter.format(record) == "worker started queue=ocr-pipeline"


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


@pytest.fixture
def built_provider(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    yield lambda: observability._provider
    observability.reset_logs()


def test_lines_are_labelled_knowledge_provider_without_a_service_name(monkeypatch, built_provider):
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    observability.configure_logs()

    assert built_provider().resource.attributes["service.name"] == "knowledge-provider"


def test_service_name_from_env_wins(monkeypatch, built_provider):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "knowledge-provider-worker")

    observability.configure_logs()

    assert built_provider().resource.attributes["service.name"] == "knowledge-provider-worker"


def test_export_gives_up_within_five_seconds_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TIMEOUT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", raising=False)

    assert observability._export_timeout() == 5.0


def test_export_timeout_from_env_is_left_to_the_sdk(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "2")

    assert observability._export_timeout() is None


def test_reset_leaves_an_injected_provider_running(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    observability.configure_logs(logger_provider=provider)

    observability.reset_logs()
    provider.get_logger("caller").emit(body="still mine")

    assert [item.log_record.body for item in exporter.get_finished_logs()] == ["still mine"]


def test_reset_shuts_down_a_provider_it_built(built_provider):
    observability.configure_logs()
    provider = built_provider()
    shutdowns = []
    provider.shutdown = lambda: shutdowns.append(True)

    observability.reset_logs()

    assert shutdowns == [True]
