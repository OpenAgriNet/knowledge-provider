"""File format admission shared by upload, URL fetch, and document processing."""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import entry_points

TEXT_UPLOAD_TYPES = {
    ".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown",
    ".html": "text/html", ".htm": "text/html", ".json": "application/json",
}
BINARY_MEDIA_TYPES = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/csv": ".csv",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/tiff": ".tiff",
}
BUILTIN_BINARY_SUFFIXES = frozenset((*BINARY_MEDIA_TYPES.values(), ".jpeg", ".tif"))
FORMAT_ENTRY_POINT_GROUP = "knowledge_provider.document_formats"


@dataclass(frozen=True)
class DocumentFormat:
    suffix: str
    media_type: str
    normalize_to_pdf: Callable[[str, str], None]


def installed_formats() -> tuple[DocumentFormat, ...]:
    configured = os.environ.get("DOCUMENT_FORMAT_EXTENSIONS", "")
    names = tuple(name.strip() for name in configured.split(",") if name.strip())
    return _load_formats(names)


@lru_cache(maxsize=8)
def _load_formats(names: tuple[str, ...]) -> tuple[DocumentFormat, ...]:
    suffixes = set(BUILTIN_BINARY_SUFFIXES) | TEXT_UPLOAD_TYPES.keys()
    media_types = BINARY_MEDIA_TYPES.keys() | set(TEXT_UPLOAD_TYPES.values()) | {"application/xhtml+xml"}
    formats = []
    for name in names:
        matches = tuple(entry_points(group=FORMAT_ENTRY_POINT_GROUP, name=name))
        if len(matches) != 1:
            raise ValueError(f"Install exactly one document format extension named {name!r}")
        definition = matches[0].load()
        if not isinstance(definition, DocumentFormat) or not callable(definition.normalize_to_pdf):
            raise ValueError(f"Document format extension {name!r} must export a DocumentFormat")
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", definition.suffix) or definition.suffix in suffixes:
            raise ValueError(f"Document format extension {name!r} has an invalid or duplicate suffix")
        if (not re.fullmatch(r"[a-z0-9-]+/[a-z0-9][a-z0-9.+-]{0,127}", definition.media_type)
                or definition.media_type in media_types or definition.media_type == "application/octet-stream"):
            raise ValueError(f"Document format extension {name!r} has an invalid or duplicate media type")
        suffixes.add(definition.suffix)
        media_types.add(definition.media_type)
        formats.append(definition)
    return tuple(formats)


def allowed_file_extensions() -> set[str]:
    return BUILTIN_BINARY_SUFFIXES | TEXT_UPLOAD_TYPES.keys() | {item.suffix for item in installed_formats()}


def binary_media_types() -> dict[str, str]:
    return BINARY_MEDIA_TYPES | {item.media_type: item.suffix for item in installed_formats()}
