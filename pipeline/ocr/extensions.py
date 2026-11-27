"""Load deployment-installed OCR providers through a Python entry point."""

from dataclasses import dataclass, field
from importlib.metadata import entry_points

from .base import OcrConfig, OcrProvider

PARSER_ENTRY_POINT_GROUP = "knowledge_provider.document_parsers"


@dataclass
class ExtensionParserConfig(OcrConfig):
    plugin: str = ""
    options: dict[str, str] = field(default_factory=dict, repr=False)


def load_extension(config: ExtensionParserConfig) -> OcrProvider:
    matches = tuple(entry_points(group=PARSER_ENTRY_POINT_GROUP, name=config.plugin))
    if len(matches) != 1:
        raise ValueError(f"Install exactly one document parser extension named {config.plugin!r}")
    factory = matches[0].load()
    provider = factory(dict(config.options))
    if not isinstance(provider, OcrProvider):
        raise TypeError(f"Document parser extension {config.plugin!r} must return an OcrProvider")
    return provider
