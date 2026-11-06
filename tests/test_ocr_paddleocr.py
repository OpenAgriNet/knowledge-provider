"""Unit tests for the Baidu PaddleOCR provider wiring.

These do NOT run PaddleOCR: the real engine needs the weights, which are downloaded
on first use. They cover the parts that are pure logic — model resolution and the
provider registry — so a broken env-var mapping fails fast in CI.
"""

import pytest


class TestPaddleOcrRegistration:
    def test_registered_in_providers(self):
        from pipeline.ocr.service import PROVIDERS

        assert PROVIDERS["paddleocr"].__name__ == "PaddleOcrProvider"
        assert PROVIDERS["paddle"] is PROVIDERS["paddleocr"]
        assert PROVIDERS["baidu"] is PROVIDERS["paddleocr"]

    def test_selectable_by_env(self, monkeypatch):
        from pipeline.ocr import service as ocr_service

        monkeypatch.setenv("OCR_PROVIDER", "paddleocr")
        config = ocr_service.load_ocr_config()
        assert config.provider == "paddleocr"

        provider = ocr_service.get_ocr_provider(config)
        assert provider.name == "paddleocr"


class TestPaddleOcrModelResolution:
    def _config(self, **kwargs):
        from pipeline.ocr.base import OcrConfig

        defaults = dict(provider="paddleocr", model="medium")
        defaults.update(kwargs)
        return OcrConfig(**defaults)

    def test_default_tier_is_ppocrv6_medium(self):
        from pipeline.ocr.paddleocr_local import resolve_model_spec

        spec = resolve_model_spec(self._config())
        assert spec.det == "PP-OCRv6_medium_det"
        assert spec.rec == "PP-OCRv6_medium_rec"
        assert spec.version == "PP-OCRv6"
        assert spec.device == "cpu"

    @pytest.mark.parametrize(
        "tier,det,rec",
        [
            ("medium", "PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
            ("small", "PP-OCRv6_small_det", "PP-OCRv6_small_rec"),
            ("tiny", "PP-OCRv6_tiny_det", "PP-OCRv6_tiny_rec"),
        ],
    )
    def test_each_tier_resolves(self, tier, det, rec):
        from pipeline.ocr.paddleocr_local import resolve_model_spec

        spec = resolve_model_spec(self._config(model=tier))
        assert (spec.det, spec.rec) == (det, rec)

    def test_explicit_pair(self):
        from pipeline.ocr.paddleocr_local import resolve_model_spec

        spec = resolve_model_spec(self._config(model="A_det,B_rec"))
        assert (spec.det, spec.rec) == ("A_det", "B_rec")

    def test_explicit_model_fields_win(self):
        from pipeline.ocr.paddleocr_local import resolve_model_spec

        spec = resolve_model_spec(
            self._config(model="medium", paddleocr_det_model="X_det", paddleocr_rec_model="Y_rec")
        )
        assert (spec.det, spec.rec) == ("X_det", "Y_rec")

    def test_unknown_model_raises_with_options(self):
        from pipeline.ocr.paddleocr_local import resolve_model_spec

        with pytest.raises(ValueError) as exc:
            resolve_model_spec(self._config(model="enormous"))
        assert "medium" in str(exc.value)

    def test_device_and_version_are_configurable(self):
        from pipeline.ocr.paddleocr_local import resolve_model_spec

        spec = resolve_model_spec(
            self._config(paddleocr_device="gpu:0", paddleocr_version="PP-OCRv5")
        )
        assert spec.device == "gpu:0"
        assert spec.version == "PP-OCRv5"


class TestPaddleOcrTextAssembly:
    def test_lines_are_ordered_and_grouped(self):
        from pipeline.ocr.paddleocr_local import group_into_lines

        items = [
            ([0.0, 0.0, 50.0, 20.0], "Hello"),
            ([60.0, 2.0, 120.0, 22.0], "world"),
            ([0.0, 40.0, 60.0, 60.0], "Second line"),
        ]
        assert group_into_lines(items) == ["Hello world", "Second line"]

    def test_empty_input(self):
        from pipeline.ocr.paddleocr_local import group_into_lines

        assert group_into_lines([]) == []

    def test_markdown_from_rec_texts(self):
        from pipeline.ocr.paddleocr_local import result_to_markdown

        class Result(dict):
            pass

        result = Result(
            rec_texts=["Invoice 4471", "Total 250.00"],
            rec_scores=[0.98, 0.91],
            rec_polys=[[[0, 0], [10, 0], [10, 5], [0, 5]], [[0, 9], [10, 9], [10, 14], [0, 14]]],
        )
        md = result_to_markdown(result)
        assert "Invoice 4471" in md
        assert "Total 250.00" in md

    def test_low_confidence_lines_are_dropped(self):
        from pipeline.ocr.paddleocr_local import result_to_markdown

        class Result(dict):
            pass

        result = Result(
            rec_texts=["keep", "drop"],
            rec_scores=[0.95, 0.10],
            rec_polys=[[[0, 0], [10, 0], [10, 5], [0, 5]], [[0, 9], [10, 9], [10, 14], [0, 14]]],
        )
        md = result_to_markdown(result, min_score=0.5)
        assert "keep" in md
        assert "drop" not in md
