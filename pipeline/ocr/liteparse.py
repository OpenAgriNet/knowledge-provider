"""Local extraction of PDF text and source positions with LiteParse."""

from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from pipeline.documents.models import (
    PAGE_LAYOUT_ITEM_MAX_CHARS,
    PAGE_LAYOUT_MAX_ITEMS,
    PageSourceLayout,
)
from .base import OcrConfig, OcrProvider, PageDict

_MAX_OUTPUT_BYTES = 32 * 1024 * 1024


class LiteParseProvider(OcrProvider):
    name = "liteparse"
    requires_all_pages = True

    def __init__(self, config: OcrConfig) -> None:
        self.executable = config.executable or "lit"
        self.timeout = config.request_timeout_seconds
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("OCR_REQUEST_TIMEOUT_SECONDS must be finite and positive")

    def process_pdf_range(
        self,
        pdf_path: str,
        start_idx: int,
        end_idx: int,
        *,
        log: Callable[..., None] | None = None,
    ) -> list[PageDict]:
        if start_idx >= end_idx:
            return []
        page_range = f"{start_idx + 1}-{end_idx}"
        input_path = str(Path(pdf_path).resolve())
        with TemporaryDirectory(prefix="kp-liteparse-") as directory:
            output_path = Path(directory) / "pages.json"
            command = [
                self.executable, "parse", "--format", "json", "--no-ocr",
                "--complexity", "--target-pages", page_range, "--quiet",
                "--output", str(output_path), "--", input_path,
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    timeout=self.timeout,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={"PATH": os.pathsep.join(os.get_exec_path())},
                    cwd=directory,
                )
            except FileNotFoundError:
                raise RuntimeError("LiteParse executable is unavailable on this worker") from None
            except subprocess.TimeoutExpired:
                raise RuntimeError("LiteParse exceeded the configured parser timeout") from None
            except (OSError, subprocess.CalledProcessError):
                raise RuntimeError("LiteParse could not parse the requested PDF pages") from None

            try:
                with output_path.open("rb") as output:
                    data = output.read(_MAX_OUTPUT_BYTES + 1)
                if len(data) > _MAX_OUTPUT_BYTES:
                    raise RuntimeError("LiteParse page range output exceeded the size limit")
                pages = json.loads(data)["pages"]
                if not isinstance(pages, list) or len(pages) != end_idx - start_idx:
                    raise ValueError("Unexpected PDF page numbers")
                for expected, page in enumerate(pages, start=start_idx + 1):
                    if type(page["page"]) is not int or page["page"] != expected:
                        raise ValueError("Unexpected PDF page numbers")
                return [_pipeline_page(page) for page in pages]
            except (OSError, KeyError, TypeError, ValueError):
                raise RuntimeError("LiteParse returned invalid page data") from None


def _pipeline_page(page: dict) -> PageDict:
    text = page["text"]
    items = page["text_items"]
    if not isinstance(text, str) or not isinstance(items, list):
        raise ValueError("Unexpected PDF text data")

    complexity = page.get("complexity") or {}
    if not isinstance(complexity, dict):
        raise ValueError("Unexpected PDF complexity data")
    needs_ocr = complexity.get("needs_ocr")
    if needs_ocr and not text.strip() and (
        complexity.get("full_page_image") or complexity.get("has_substantial_images")
    ):
        raise RuntimeError("LiteParse found a scanned page without text. Select an OCR provider.")

    positioned_text = []
    truncated = len(items) > PAGE_LAYOUT_MAX_ITEMS
    for item in items[:PAGE_LAYOUT_MAX_ITEMS]:
        excerpt = item["text"][:PAGE_LAYOUT_ITEM_MAX_CHARS]
        truncated = truncated or len(excerpt) < len(item["text"])
        positioned_text.append({
            "text": excerpt,
            "x": item["x"],
            "y": item["y"],
            "width": item["width"],
            "height": item["height"],
        })
    source_layout = PageSourceLayout(
        parser=LiteParseProvider.name,
        width=page["width"],
        height=page["height"],
        text_items=positioned_text,
        truncated=truncated,
        ocr_recommended=needs_ocr,
    )
    return {
        "page_number": page["page"],
        "original_markdown": text,
        "edited_markdown": None,
        "metadata": {"source_layout": source_layout.model_dump(exclude_none=True)},
    }
