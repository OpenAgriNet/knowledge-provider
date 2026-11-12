from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider

_MARKER = "_knowledge_provider_otel_handler"
_TIMEOUT_ENV = ("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "OTEL_EXPORTER_OTLP_TIMEOUT")

DEFAULT_SERVICE_NAME = "knowledge-provider"
DEFAULT_EXPORT_TIMEOUT_SECONDS = 5.0

_provider: LoggerProvider | None = None
_provider_is_ours = False


_NOT_EXPORTED = ("opentelemetry",)

# What every record has before `extra=` adds to it.
_STANDARD_KEYS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
CONSOLE_DATE_FORMAT = "%H:%M:%S"


class _SkipUnexported(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_NOT_EXPORTED)


class _FlattenForExport(logging.Filter):
    """Turns each extra into a flat, primitive attribute HyperDX can filter on.

    - A dict, like Temporal's `temporal_activity`, becomes dotted keys.
    - Anything else that is not a primitive becomes text, or it is dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key in _extra_keys(record):
            value = vars(record).pop(key)
            vars(record).update(_flatten(key, value))
        return True


class KeyValueFormatter(logging.Formatter):
    """The base line, then each extra as `key=value`, so the console stays greppable."""

    def format(self, record: logging.LogRecord) -> str:
        fields = [f"{key}={vars(record)[key]}" for key in _extra_keys(record)]
        return " ".join([super().format(record), *fields])


def configure_console(level: int) -> None:
    """Like `logging.basicConfig`, and a no-op too when root already has handlers."""

    handler = logging.StreamHandler()
    handler.setFormatter(KeyValueFormatter(CONSOLE_FORMAT, datefmt=CONSOLE_DATE_FORMAT))
    logging.basicConfig(level=level, handlers=[handler])


def _extra_keys(record: logging.LogRecord) -> list[str]:
    return [key for key in vars(record) if key not in _STANDARD_KEYS]


def _flatten(key: str, value: Any) -> dict[str, str | int | float | bool]:
    if isinstance(value, dict):
        return {
            flat_key: flat_value
            for child_key, child_value in value.items()
            for flat_key, flat_value in _flatten(f"{key}.{child_key}", child_value).items()
        }
    if isinstance(value, (str, int, float, bool)):
        return {key: value}
    return {key: str(value)}


def is_otel_handler(handler: logging.Handler) -> bool:
    return getattr(handler, _MARKER, False)


def _resource():
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    name = os.environ.get("OTEL_SERVICE_NAME", "").strip() or DEFAULT_SERVICE_NAME
    return Resource.create({SERVICE_NAME: name})


def _export_timeout() -> float | None:
    if any(os.environ.get(name, "").strip() for name in _TIMEOUT_ENV):
        return None
    return DEFAULT_EXPORT_TIMEOUT_SECONDS


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

        logger_provider = SdkLoggerProvider(resource=_resource())
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(timeout=_export_timeout()))
        )

    from opentelemetry.instrumentation.logging.handler import LoggingHandler

    handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    handler.addFilter(_SkipUnexported())
    handler.addFilter(_FlattenForExport())
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
