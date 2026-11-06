"""OCR provider interfaces and shared structures.

Each OCR backend is a class that subclasses ``OcrProvider`` (one file per model).
The pipeline never calls a model directly — it uses ``get_ocr_provider()`` in
``service.py``, which picks the class from the ``PROVIDERS`` registry via
``OCR_PROVIDER``.

To swap or add a model:
  1. Create ``pipeline/ocr/<name>.py`` with ``class <Name>OcrProvider(OcrProvider)``
  2. Register it in ``pipeline/ocr/service.py`` → ``PROVIDERS``
  3. Set ``OCR_PROVIDER=<name>`` in docker-compose / env
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class OcrConfig:
    provider: str
    model: str = ""
    api_key: str = ""
    endpoint: str = ""
    api_url: str = ""
    inference_mode: str = "hf"
    max_split_pages: int = 40
    segment_pages: int = 20
    max_output_tokens: int = 12288
    max_workers: int = 4
    image_dpi: int = 192
    request_timeout_seconds: float = 300.0

    # Baidu PaddleOCR (local, CPU) lane. These are separate fields rather than
    # reusing ``model``/``image_dpi`` because the PaddleOCR knobs are spelled
    # differently from the Chandra/Mistral ones, and one deployment may want both
    # lanes configured at once (switch with OCR_PROVIDER).
    paddleocr_version: str = "PP-OCRv6"
    paddleocr_lang: str = ""
    paddleocr_device: str = "cpu"
    paddleocr_det_model: str = ""
    paddleocr_rec_model: str = ""
    paddleocr_min_score: float = 0.0
    paddleocr_image_dpi: int = 192
    paddleocr_enable_mkldnn: bool = False


PageDict = dict


class OcrProvider(ABC):
    name: str

    @abstractmethod
    def process_pdf_range(
        self,
        pdf_path: str,
        start_idx: int,
        end_idx: int,
        *,
        log: Optional[Callable[..., None]] = None,
    ) -> list[PageDict]:
        """OCR pages [start_idx, end_idx) and return pipeline page dicts (1-based page numbers)."""
        raise NotImplementedError
