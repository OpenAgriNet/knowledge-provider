"""Baidu PaddleOCR (PP-OCR) provider — local CPU inference, weights fetched on first use.

WHY THIS EXISTS NEXT TO CHANDRA
--------------------------------
``chandra`` (pipeline/ocr/chandra_vllm.py) is the high-accuracy lane: a ~5B-parameter
vision-language model, but it needs a remote vLLM server on a GPU host. PaddleOCR is
the opposite trade — PP-OCRv6 is a 34.5M-parameter detection + recognition pair that
runs on CPU inside this very image and reads 50 languages from a single checkpoint, so
it is the practical lane for a self-hosted deployment with no GPU.

Weights are NOT baked into the image. PaddleX downloads the selected checkpoint into
``PADDLE_PDX_CACHE_HOME`` the first time it is needed, so one image serves every tier
and the download happens once, onto the mounted cache volume.

MODEL SELECTION (deployment-time settings, no code change)
-----------------------------------------------------------
``OCR_PROVIDER=paddleocr``  — pick this provider (aliases: ``paddle``, ``baidu``).
``PADDLEOCR_MODEL``         — ``medium`` | ``small`` | ``tiny`` (PP-OCRv6 tiers), or an
                              explicit ``"<det>,<rec>"`` pair of model names.
``PADDLEOCR_OCR_VERSION``   — ``PP-OCRv6`` (default) | ``PP-OCRv5`` | ``PP-OCRv4``.
``PADDLEOCR_LANG``          — checkpoint language hint, e.g. ``en``, ``ch``, ``dev``.
                              Empty = the checkpoint's own default.
``PADDLEOCR_DET_MODEL`` / ``PADDLEOCR_REC_MODEL`` — explicit model names; they win over
                              ``PADDLEOCR_MODEL`` and let you mix tiers.
``PADDLEOCR_DEVICE``        — ``cpu`` (default) or ``gpu`` / ``gpu:0``.
``PADDLEOCR_MIN_SCORE``     — drop recognised lines below this confidence (default 0.0).
``PADDLEOCR_IMAGE_DPI``     — rasterisation DPI for PDF pages (default 192).

``PADDLE_PDX_MODEL_SOURCE`` is honoured by PaddleX itself (``huggingface`` by default,
``modelscope`` is the alternative) — set it if the default model host is unreachable.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .base import OcrConfig, OcrProvider, PageDict

logger = logging.getLogger("pipeline.ocr.paddleocr")

DEFAULT_OCR_VERSION = "PP-OCRv6"
DEFAULT_MODEL = "medium"
DEFAULT_IMAGE_DPI = 192

# PP-OCRv6 ships three tiers of the same detection+recognition architecture. The tier
# name is what an operator picks; the pair of model names is what PaddleOCR is given.
TIER_MODELS: dict[str, tuple[str, str]] = {
    "medium": ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
    "small": ("PP-OCRv6_small_det", "PP-OCRv6_small_rec"),
    "tiny": ("PP-OCRv6_tiny_det", "PP-OCRv6_tiny_rec"),
}


def _default_log(level: str, message: str, *args) -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(message, *args)


class PaddleOcrUnavailable(RuntimeError):
    """Raised when PaddleOCR / PaddlePaddle cannot be imported."""


@dataclass(frozen=True)
class ModelSpec:
    """The resolved (detector, recogniser) pair plus how to run it."""

    det: str
    rec: str
    version: str
    lang: str
    device: str
    enable_mkldnn: bool = False


def resolve_model_spec(config: OcrConfig) -> ModelSpec:
    """Resolve the configured model into a (detector, recogniser) pair.

    Explicit ``PADDLEOCR_DET_MODEL`` / ``PADDLEOCR_REC_MODEL`` win, then an explicit
    ``"<det>,<rec>"`` pair in the model setting, then a tier name. Raises
    ``ValueError`` naming the valid options rather than failing later inside PaddleX
    with an opaque model-not-found error.
    """
    version = config.paddleocr_version or DEFAULT_OCR_VERSION
    lang = config.paddleocr_lang or ""
    device = config.paddleocr_device or "cpu"
    enable_mkldnn = bool(config.paddleocr_enable_mkldnn)

    det = config.paddleocr_det_model or ""
    rec = config.paddleocr_rec_model or ""
    if det and rec:
        return ModelSpec(
            det=det,
            rec=rec,
            version=version,
            lang=lang,
            device=device,
            enable_mkldnn=enable_mkldnn,
        )

    model = (config.model or "").strip() or DEFAULT_MODEL
    if "," in model:
        det, rec = (part.strip() for part in model.split(",", 1))
        if not det or not rec:
            raise ValueError(
                f"PaddleOCR model {model!r} must be '<det_model>,<rec_model>' "
                "with both names non-empty."
            )
        return ModelSpec(
            det=det,
            rec=rec,
            version=version,
            lang=lang,
            device=device,
            enable_mkldnn=enable_mkldnn,
        )

    tier = model.lower()
    if tier in TIER_MODELS:
        det_model, rec_model = TIER_MODELS[tier]
        return ModelSpec(
            det=det_model,
            rec=rec_model,
            version=version,
            lang=lang,
            device=device,
            enable_mkldnn=enable_mkldnn,
        )

    valid = ", ".join(sorted(TIER_MODELS))
    raise ValueError(
        f"Unknown PaddleOCR model {model!r}. Use one of the PP-OCRv6 tiers ({valid}), "
        "an explicit '<det>,<rec>' pair, or set PADDLEOCR_DET_MODEL and PADDLEOCR_REC_MODEL."
    )


# One engine per resolved model set. Constructing PaddleOCR loads the weights, and the
# very first construction is what triggers the download — so construction happens under
# the lock, and the engine is reused for every later page.
_engines: dict[tuple[str, str, str, str, str, bool], object] = {}
_engine_lock = threading.Lock()


def _engine_key(spec: ModelSpec) -> tuple[str, str, str, str, str, bool]:
    return (spec.det, spec.rec, spec.version, spec.lang, spec.device, spec.enable_mkldnn)


def get_engine(spec: ModelSpec):
    """Build (once) and return the PaddleOCR engine for ``spec``.

    The first call downloads the checkpoint into ``PADDLE_PDX_CACHE_HOME``; later calls
    reuse the loaded engine.
    """
    key = _engine_key(spec)
    engine = _engines.get(key)
    if engine is not None:
        return engine

    with _engine_lock:
        engine = _engines.get(key)
        if engine is not None:
            return engine

        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise PaddleOcrUnavailable(
                "PaddleOCR is not installed in this image. The Baidu PaddleOCR lane is "
                "built for linux/amd64 (paddlepaddle publishes no aarch64 wheels); on "
                "arm64 use OCR_PROVIDER=chandra or OCR_PROVIDER=mistral instead."
            ) from exc

        kwargs: dict[str, object] = {
            "text_detection_model_name": spec.det,
            "text_recognition_model_name": spec.rec,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "device": spec.device,
            # PaddlePaddle 3.3 + oneDNN crash on CPU with
            # "ConvertPirAttribute2RuntimeAttribute not support
            # [pir::ArrayAttribute<pir::DoubleAttribute>]" during the PIR -> oneDNN
            # conversion. It is not fixable by the FLAGS_* env vars (PaddleX picks
            # run_mode=mkldnn independently); the pipeline has to be told to skip
            # oneDNN. See https://github.com/PaddlePaddle/Paddle/issues/77340
            "enable_mkldnn": spec.enable_mkldnn,
        }
        logger.info(
            "Loading PaddleOCR engine: det=%s rec=%s version=%s lang=%s device=%s "
            "(weights are downloaded on first use into PADDLE_PDX_CACHE_HOME)",
            spec.det,
            spec.rec,
            spec.version,
            spec.lang or "<default>",
            spec.device,
        )
        engine = PaddleOCR(**kwargs)
        _engines[key] = engine
        logger.info("PaddleOCR engine ready (det=%s rec=%s)", spec.det, spec.rec)
        return engine


def _page_to_array(doc, index: int, dpi: int):
    """Render one PDF page to an RGB numpy array (PaddleOCR's native input)."""
    import numpy as np

    page = doc.load_page(index)
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:  # RGBA -> RGB
        arr = arr[:, :, :3]
    return arr


def _result_value(result, key: str, default=None):
    """Read ``key`` from a PaddleOCR result, tolerating dict-like and attribute-style."""
    if result is None:
        return default
    try:
        value = result[key]
    except Exception:  # noqa: BLE001 — not a mapping, or key absent
        value = getattr(result, key, default)
    return default if value is None else value


def _boxes_and_texts(result, min_score: float) -> list[tuple[list[float], str]]:
    """Extract [(bbox, text)] from one PaddleOCR result, dropping low-confidence lines."""
    texts = list(_result_value(result, "rec_texts", []) or [])
    scores = list(_result_value(result, "rec_scores", []) or [])
    polys = list(_result_value(result, "rec_polys", []) or [])

    out: list[tuple[list[float], str]] = []
    for i, text in enumerate(texts):
        text = ("" if text is None else str(text)).strip()
        if not text:
            continue
        if i < len(scores):
            try:
                if float(scores[i]) < min_score:
                    continue
            except (TypeError, ValueError):
                pass
        bbox = [0.0, 0.0, 0.0, 0.0]
        if i < len(polys):
            try:
                points = [[float(x), float(y)] for x, y in polys[i]]
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                bbox = [min(xs), min(ys), max(xs), max(ys)]
            except Exception:  # noqa: BLE001 — a malformed polygon must not kill the page
                pass
        out.append((bbox, text))
    return out


def group_into_lines(items: list[tuple[list[float], str]]) -> list[str]:
    """Group detected boxes into reading-order lines.

    Boxes are clustered by vertical overlap, then sorted left-to-right within a line.
    A page's words therefore come out as the lines a human would read, not as a flat
    bag of fragments.
    """
    if not items:
        return []

    remaining = sorted(items, key=lambda it: (it[0][1], it[0][0]))
    lines: list[str] = []
    current: list[tuple[list[float], str]] = []

    def flush() -> None:
        if not current:
            return
        current.sort(key=lambda it: it[0][0])
        text = " ".join(t for _, t in current).strip()
        if text:
            lines.append(text)

    for bbox, text in remaining:
        if not current:
            current = [(bbox, text)]
            continue
        line_top = min(b[1] for b, _ in current)
        line_bottom = max(b[3] for b, _ in current)
        height = max(1.0, bbox[3] - bbox[1])
        overlap = min(line_bottom, bbox[3]) - max(line_top, bbox[1])
        if overlap > 0.5 * height:
            current.append((bbox, text))
        else:
            flush()
            current = [(bbox, text)]
    flush()
    return lines


def result_to_markdown(result, min_score: float = 0.0) -> str:
    """One PaddleOCR result -> Markdown text (one line per detected text line)."""
    items = _boxes_and_texts(result, min_score)
    return "\n".join(group_into_lines(items))


class PaddleOcrProvider(OcrProvider):
    """PP-OCR (Baidu PaddleOCR) running locally on CPU inside this image."""

    name = "paddleocr"

    def __init__(self, config: OcrConfig):
        self.config = config
        self.spec = resolve_model_spec(config)
        self.min_score = float(config.paddleocr_min_score or 0.0)
        self.dpi = int(config.paddleocr_image_dpi or DEFAULT_IMAGE_DPI)

    def process_pdf_range(
        self,
        pdf_path: str,
        start_idx: int,
        end_idx: int,
        *,
        log: Optional[Callable[..., None]] = None,
    ) -> list[PageDict]:
        emit = log or _default_log
        if start_idx >= end_idx:
            return []

        emit(
            "info",
            "Running PaddleOCR (%s / %s) for pages %s-%s of %s",
            self.spec.det,
            self.spec.rec,
            start_idx + 1,
            end_idx,
            Path(pdf_path).name,
        )

        try:
            engine = get_engine(self.spec)
        except PaddleOcrUnavailable as exc:
            raise RuntimeError(str(exc)) from exc

        try:
            import fitz
        except ImportError as exc:  # pragma: no cover — PyMuPDF is a hard dependency
            raise RuntimeError(
                "PyMuPDF (fitz) is required to rasterise PDF pages for PaddleOCR."
            ) from exc

        pages: list[PageDict] = []
        doc = fitz.open(pdf_path)
        try:
            end_idx = min(end_idx, doc.page_count)
            for idx in range(start_idx, end_idx):
                page_number = idx + 1
                try:
                    array = _page_to_array(doc, idx, self.dpi)
                    results = engine.predict(array)
                    result = results[0] if isinstance(results, list) and results else results
                    markdown = result_to_markdown(result, self.min_score)
                except Exception as exc:  # noqa: BLE001 — one bad page must not kill the doc
                    emit("warning", "PaddleOCR failed on page %s: %s", page_number, exc)
                    markdown = ""
                pages.append(
                    {
                        "page_number": page_number,
                        "original_markdown": markdown,
                        "edited_markdown": None,
                        "is_reviewed": False,
                        "reviewer_notes": None,
                    }
                )
        finally:
            doc.close()
        return pages
