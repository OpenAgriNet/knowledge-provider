"""Unit tests for translation providers and service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTranslationService:
    @pytest.mark.unit
    def test_load_translation_config_defaults(self, monkeypatch):
        from pipeline.translation.service import load_translation_config

        monkeypatch.delenv("TRANSLATION_PROVIDER", raising=False)
        monkeypatch.delenv("TRANSLATION_MODEL", raising=False)
        monkeypatch.setenv("TRANSLATION_VLLM_BASE_URL", "http://localhost:8000/v1")

        config = load_translation_config()

        assert config.provider == "gemma_vllm"
        assert config.model == "google/gemma-4-31b-it"
        assert config.endpoint == "http://localhost:8000/v1"

    @pytest.mark.unit
    def test_gemma_provider_requires_endpoint(self):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation.gemma_vllm import GemmaVllmTranslationProvider

        config = TranslationConfig(provider="gemma_vllm", model="gemma-4", endpoint="")
        with pytest.raises(ValueError, match="TRANSLATION_VLLM_BASE_URL"):
            GemmaVllmTranslationProvider(config)

    @pytest.mark.unit
    def test_gemma_provider_translate(self, monkeypatch):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation.gemma_vllm import GemmaVllmTranslationProvider

        config = TranslationConfig(
            provider="gemma_vllm",
            model="gemma-4",
            endpoint="http://localhost:8000/v1",
        )
        provider = GemmaVllmTranslationProvider(config)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Translated text"}}],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch("pipeline.translation.gemma_vllm.httpx.Client", return_value=mock_client):
            result = provider.translate("ટેસ્ટ", source_lang="gu", target_language="en")

        assert result == "Translated text"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["json"]["model"] == "gemma-4"

    @pytest.mark.unit
    def test_normalize_detected_language_gujarati_script(self):
        from pipeline.translation.service import normalize_detected_language

        assert normalize_detected_language("zl", "ગુજરાતી ટેક્સ્ટ") == "gu"
        assert normalize_detected_language("en", "English only") == "en"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_translate_pages_skips_english(self, monkeypatch):
        from pipeline.translation import service as translation_service
        from pipeline.translation.base import TranslationConfig

        config = TranslationConfig(
            provider="gemma_vllm",
            model="gemma-4",
            endpoint="http://localhost:8000/v1",
        )

        pages = [
            {
                "page_number": 1,
                "original_markdown": "English content that is long enough for detection.",
                "edited_markdown": None,
            },
            {
                "page_number": 2,
                "original_markdown": "ગુજરાતી સામગ્રી જે અનુવાદ માટે લાંબી છે.",
                "edited_markdown": None,
            },
        ]

        monkeypatch.setattr(
            translation_service,
            "detect_page_languages",
            AsyncMock(return_value={0: "en", 1: "gu"}),
        )

        mock_provider = MagicMock()
        mock_provider.translate.return_value = "Gujarati content translated."
        monkeypatch.setattr(
            translation_service,
            "get_translation_provider",
            lambda cfg=None: mock_provider,
        )

        result = await translation_service.translate_pages(pages, config=config)

        assert result[0].get("translated_markdown") is None
        assert result[0]["detected_language"] == "en"
        assert result[1]["translated_markdown"] == "Gujarati content translated."
        assert result[1]["detected_language"] == "gu"
        mock_provider.translate.assert_called_once()


class TestScriptGate:
    """Regex script gate — decides which pages reach the translation model."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "text",
        [
            "National Mission on Edible Oils - Oil Palm (NMEO-OP) operational guidelines.",
            "Table 3.1 | Area | Yield | 12,500 | 3.4 | Rs. 29,000 per hectare subsidy.",
            "1. Introduction\n2. Objectives\n3. Pattern of Assistance\n4. Implementation",
        ],
    )
    def test_english_pages_are_skipped(self, text):
        """The exact shape of page that was misdetected as sw/de/hu/fr/ro."""
        from pipeline.translation.script_detect import analyze_script

        result = analyze_script(text)

        assert result.is_non_english is False
        assert result.language == "en"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "text,expected_lang,expected_script",
        [
            ("राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।", "hi", "Devanagari"),
            ("ખેડૂતોને આ યોજના હેઠળ સહાય આપવામાં આવશે અને લાભ મળશે.", "gu", "Gujarati"),
            ("இந்த திட்டத்தின் கீழ் விவசாயிகளுக்கு உதவி வழங்கப்படும்.", "ta", "Tamil"),
            ("ఈ పథకం కింద రైతులకు సహాయం అందించబడుతుంది.", "te", "Telugu"),
            ("এই প্রকল্পের অধীনে কৃষকদের সহায়তা দেওয়া হবে।", "bn", "Bengali"),
        ],
    )
    def test_indic_pages_are_flagged(self, text, expected_lang, expected_script):
        from pipeline.translation.script_detect import analyze_script

        result = analyze_script(text)

        assert result.is_non_english is True
        assert result.language == expected_lang
        assert result.script == expected_script

    @pytest.mark.unit
    def test_stray_glyph_does_not_trigger_translation(self):
        """A danda or lone character in English text must not cost a Gemma call."""
        from pipeline.translation.script_detect import analyze_script

        text = "Pattern of Assistance under the scheme is Rs. 29,000 per hectare ₹ । क"

        result = analyze_script(text)

        assert result.is_non_english is False
        assert "min_chars" in result.reason

    @pytest.mark.unit
    def test_mostly_english_page_with_hindi_paragraph_is_translated(self):
        from pipeline.translation.script_detect import analyze_script

        text = (
            "Operational guidelines for the scheme. " * 5
            + "योजना के अंतर्गत किसानों को प्रति हेक्टेयर सहायता राशि दी जाएगी और लाभ मिलेगा।"
        )

        result = analyze_script(text)

        assert result.is_non_english is True
        assert result.language == "hi"

    @pytest.mark.unit
    def test_devanagari_is_marked_ambiguous_gujarati_is_not(self):
        from pipeline.translation.script_detect import analyze_script

        hindi = analyze_script("राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।")
        gujarati = analyze_script("ખેડૂતોને આ યોજના હેઠળ સહાય આપવામાં આવશે અને લાભ મળશે.")

        assert hindi.ambiguous is True
        assert "mr" in hindi.candidates
        assert gujarati.ambiguous is False

    @pytest.mark.unit
    def test_empty_page_is_english(self):
        from pipeline.translation.script_detect import analyze_script

        assert analyze_script("").is_non_english is False
        assert analyze_script("   \n  ").is_non_english is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_gate_skips_pyfranc_for_english_pages(self, monkeypatch):
        """No disambiguation call at all when every page is Latin script.

        pyfranc lives in script_detect now, not service — patch it there.
        """
        from pipeline.translation import script_detect, service
        from pipeline.translation.base import TranslationConfig

        pages = [
            {"page_number": 1, "original_markdown": "Operational guidelines for oil palm."},
            {"page_number": 2, "original_markdown": "Pattern of assistance and subsidy norms."},
        ]

        def explode(*args, **kwargs):
            raise AssertionError("pyfranc must not be called for Latin-script pages")

        monkeypatch.setattr(script_detect.franc, "lang_detect", explode)

        config = TranslationConfig(provider="gemma_vllm", model="gemma-4")
        detected = await service.detect_page_languages(pages, config=config)

        assert detected == {0: "en", 1: "en"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_gate_logs_decision_per_page(self):
        from pipeline.translation import service
        from pipeline.translation.base import TranslationConfig

        pages = [
            {"page_number": 1, "original_markdown": "Operational guidelines for oil palm."},
            {"page_number": 2, "original_markdown": "ખેડૂતોને આ યોજના હેઠળ સહાય આપવામાં આવશે અને લાભ મળશે."},
        ]
        messages = []

        def log(msg, *args):
            messages.append(msg % args if args else msg)

        config = TranslationConfig(provider="gemma_vllm", model="gemma-4")
        detected = await service.detect_page_languages(pages, log=log, config=config)

        assert detected == {0: "en", 1: "gu"}
        joined = "\n".join(messages)
        assert "Page 1: regex" in joined and "SKIP translation" in joined
        assert "Page 2: regex" in joined and "TRANSLATE" in joined
        assert "1/2 page(s) need translation" in joined


class TestScriptDetectDisambiguation:
    """pyfranc winner-vote logic — lives entirely in script_detect now
    (service.py never imports pyfranc, per the Tell-Don't-Ask split)."""

    @pytest.mark.unit
    def test_family_whitelist_maps_to_iso3(self):
        from pipeline.translation.script_detect import _family_whitelist

        whitelist = _family_whitelist(("hi", "mr", "ne", "sa", "kok"))
        assert whitelist == ["hin", "mar", "nep", "san", "kok"]

    @pytest.mark.unit
    def test_family_whitelist_drops_codes_with_no_iso3_entry(self):
        """A family member missing from script_families.json's iso3 map must be
        dropped silently, not raise — mirrors the exact gap found and fixed
        earlier (a language added to 'family' but not to 'iso3')."""
        from pipeline.translation.script_detect import _family_whitelist

        whitelist = _family_whitelist(("hi", "mr", "not-a-configured-code"))
        assert whitelist == ["hin", "mar"]

    @pytest.mark.unit
    def test_iso3_map_matches_script_families_json(self):
        from pipeline.translation.script_detect import iso3_map

        m = iso3_map()
        assert m["hi"] == "hin"
        assert m["mr"] == "mar"

    @pytest.mark.unit
    def test_disambiguate_flips_hindi_default_to_marathi(self):
        """Real pyfranc call (no mocking): genuine Marathi text on a page the
        regex gate defaulted to Hindi must get corrected to 'mr'."""
        from pipeline.translation.script_detect import disambiguate

        marathi_text = "राज्यातील शेतकऱ्यांना या योजनेअंतर्गत आर्थिक मदत दिली जाईल आणि लाभ मिळेल."
        result = disambiguate("hi", [marathi_text])

        assert result.winner == "mr"
        assert result.attempted is True

    @pytest.mark.unit
    def test_disambiguate_keeps_hindi_default_for_hindi_text(self):
        """Real pyfranc call: genuine Hindi text must NOT get flipped away
        from the regex gate's own correct default — winner stays None."""
        from pipeline.translation.script_detect import disambiguate

        hindi_text = "राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।"
        result = disambiguate("hi", [hindi_text])

        assert result.winner is None

    @pytest.mark.unit
    def test_disambiguate_keeps_default_when_pyfranc_errors(self, monkeypatch):
        """A pyfranc failure on a line must not raise or block the pipeline —
        it keeps the script gate's default language (winner=None)."""
        from pipeline.translation import script_detect

        def explode(*args, **kwargs):
            raise RuntimeError("pyfranc boom")

        monkeypatch.setattr(script_detect.franc, "lang_detect", explode)

        result = script_detect.disambiguate(
            "hi", ["राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।"]
        )

        assert result.winner is None
        assert result.votes == {}

    @pytest.mark.unit
    def test_disambiguate_keeps_default_when_votes_outside_family(self, monkeypatch):
        """If pyfranc's top pick isn't a member of the script's family, the
        default must be kept rather than overwritten with a nonsense value."""
        from pipeline.translation import script_detect

        monkeypatch.setattr(script_detect.franc, "lang_detect", lambda *a, **kw: [("fra", 1.0)])

        result = script_detect.disambiguate(
            "hi", ["राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।"]
        )

        assert result.winner is None
        assert result.votes == {}

    @pytest.mark.unit
    def test_disambiguate_not_attempted_for_unambiguous_script(self):
        """A language with no shared-script family (e.g. Gujarati) has
        nothing to disambiguate — must be a safe no-op, attempted=False."""
        from pipeline.translation.script_detect import disambiguate

        result = disambiguate("gu", ["ખેડૂતોને આ યોજના હેઠળ સહાય આપવામાં આવશે."])

        assert result.attempted is False
        assert result.winner is None

    @pytest.mark.unit
    def test_detect_any_returns_short_code(self):
        from pipeline.translation.script_detect import detect_any

        assert detect_any("This is a normal English sentence used for testing detection.") in {
            "en",
            None,
        }

    @pytest.mark.unit
    def test_detect_any_returns_none_on_pyfranc_error(self, monkeypatch):
        from pipeline.translation import script_detect

        monkeypatch.setattr(
            script_detect.franc,
            "lang_detect",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        assert script_detect.detect_any("some text here") is None


class TestServiceDisambiguationWiring:
    """service.py's _disambiguate_languages() only orchestrates pages/logging
    now — the actual pyfranc decision comes from script_detect.disambiguate().
    These confirm that wiring, not the winner-vote logic itself."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_applies_winner_and_logs_it(self):
        from pipeline.translation import service

        marathi_text = "राज्यातील शेतकऱ्यांना या योजनेअंतर्गत आर्थिक मदत दिली जाईल आणि लाभ मिळेल."
        pages = [{"page_number": 1, "original_markdown": marathi_text}]
        detected_languages = {0: "hi"}
        messages = []

        def log(msg, *args):
            messages.append(msg % args if args else msg)

        await service._disambiguate_languages(pages, [0], detected_languages, log=log)

        assert detected_languages[0] == "mr"
        assert any("refined hi → mr" in m for m in messages)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_keeps_default_and_does_not_log_refinement(self):
        from pipeline.translation import service

        hindi_text = "राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।"
        pages = [{"page_number": 1, "original_markdown": hindi_text}]
        detected_languages = {0: "hi"}
        messages = []

        def log(msg, *args):
            messages.append(msg % args if args else msg)

        await service._disambiguate_languages(pages, [0], detected_languages, log=log)

        assert detected_languages[0] == "hi"
        assert not any("refined" in m for m in messages)
