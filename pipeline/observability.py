from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider

_MARKER = "_knowledge_provider_otel_handler"

_provider: LoggerProvider | None = None
_provider_is_ours = False


class _SkipOpenTelemetry(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry")


def is_otel_handler(handler: logging.Handler) -> bool:
    return getattr(handler, _MARKER, False)


def configure_logs(*, logger_provider: LoggerProvider | None = None) -> None:
    global _provider, _provider_is_ours

    _detach()

    ours = logger_provider is None
    if ours:
        if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
            return

        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider as SdkLoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        logger_provider = SdkLoggerProvider()
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))

    from opentelemetry.instrumentation.logging.handler import LoggingHandler

    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    handler.addFilter(_SkipOpenTelemetry())
    setattr(handler, _MARKER, True)
    logging.getLogger().addHandler(handler)

    _provider = logger_provider
    _provider_is_ours = ours


def reset_logs() -> None:
    global _provider, _provider_is_ours

    _detach()
    _provider = None
    _provider_is_ours = False


def _detach() -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if is_otel_handler(h)]:
        root.removeHandler(handler)

    if _provider is not None and _provider_is_ours:
        _provider.shutdown()
