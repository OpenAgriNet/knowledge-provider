"""Translation service layer, language detection, and provider selection."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Callable, Optional

from pipeline.metadata.localization import normalize_language_tag
from .base import TranslationConfig, TranslationProvider
from .gemma_vllm import GemmaVllmTranslationProvider
from .script_detect import analyze_script, detect_any, disambiguate

PROVIDERS: dict[str, type[TranslationProvider]] = {
    "openai_compatible": GemmaVllmTranslationProvider,
    "gemma_vllm": GemmaVllmTranslationProvider,
    "gemma4": GemmaVllmTranslationProvider,
    "gemma": GemmaVllmTranslationProvider,
}

LANG_MAP = {
    "english": "en",
    "hindi": "hi",
    "gujarati": "gu",
    "marathi": "mr",
    "tamil": "ta",
    "telugu": "te",
    "kannada": "kn",
    "malayalam": "ml",
    "punjabi": "pa",
    "bengali": "bn",
    "oriya": "or",
    "odia": "or",
    # Observed noisy code from the old lang-detect service for Gujarati pages.
    "zl": "gu",
}

def configured_translation_endpoint() -> str:
    return (
        os.environ.get("TRANSLATION_BASE_URL")
        or os.environ.get("AGRINET_GEMMA_BASE_URL")
        or os.environ.get("TRANSLATION_VLLM_BASE_URL")
        or ""
    ).strip()


def configured_translation_type() -> str:
    return os.environ.get("TRANSLATION_TYPE", os.environ.get("TRANSLATION_PROVIDER", "gemma_vllm")).strip().lower()


def _gemma_model() -> str:
    return (
        os.environ.get("TRANSLATION_MODEL")
        or os.environ.get("AGRINET_GEMMA_MODEL_NAME")
        or "google/gemma-4-31b-it"
    ).strip() or "google/gemma-4-31b-it"


def load_translation_config(target_language: str = "en") -> TranslationConfig:
    provider = configured_translation_type()
    model = _gemma_model()
    endpoint = configured_translation_endpoint() or "http://localhost:8020/v1"
    # Ensure OpenAI-compatible path: base may be .../gemma4 without trailing /v1
    if endpoint and not endpoint.rstrip("/").endswith("/v1"):
        endpoint = endpoint.rstrip("/") + "/v1"
    api_key = (
        os.environ.get("TRANSLATION_API_KEY")
        or os.environ.get("AGRINET_GEMMA_API_KEY")
        or ""
    ).strip()
    return TranslationConfig(
        provider=provider,
        model=model,
        endpoint=endpoint,
        api_key=api_key,
        target_language=target_language,
        page_concurrency=max(1, int(os.environ.get("TRANSLATION_PAGE_CONCURRENCY", "1"))),
        max_retries=max(1, int(os.environ.get("TRANSLATION_MAX_RETRIES", "6"))),
        retry_base_seconds=max(0.5, float(os.environ.get("TRANSLATION_RETRY_BASE_SECONDS", "2.0"))),
        max_output_tokens=int(os.environ.get("TRANSLATION_MAX_OUTPUT_TOKENS", "8000")),
        request_timeout_seconds=float(os.environ.get("TRANSLATION_REQUEST_TIMEOUT_SECONDS", "300")),
        use_max_completion_tokens=(
            os.environ.get("TRANSLATION_USE_MAX_COMPLETION_TOKENS", "false").strip().lower()
            in {"true", "1", "yes"}
        ),
        # Empty TRANSLATION_TEMPERATURE omits the field entirely.
        temperature=(
            float(raw_temp) if (raw_temp := os.environ.get("TRANSLATION_TEMPERATURE", "0.0").strip()) else None
        ),
        script_gate_enabled=(
            os.environ.get("TRANSLATION_SCRIPT_GATE_ENABLED", "true").strip().lower()
            not in {"false", "0", "no"}
        ),
        script_min_chars=max(1, int(os.environ.get("TRANSLATION_SCRIPT_MIN_CHARS", "15"))),
        script_min_ratio=max(0.0, float(os.environ.get("TRANSLATION_SCRIPT_MIN_RATIO", "0.05"))),
    )


def get_translation_provider(config: Optional[TranslationConfig] = None) -> TranslationProvider:
    config = config or load_translation_config()
    provider_cls = PROVIDERS.get(config.provider)
    if provider_cls is None:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unsupported translation provider '{config.provider}'. Supported: {supported}")
    return provider_cls(config)


def clean_translation(text: str) -> str:
    result = text
    result = re.sub(
        r"^Here is the translated text from \*\*[^*]+\*\* to English[^:]*:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the translated text from [^:]+?:\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the translated text[^:]*:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the (?:English )?translation[^:]*:?\s*\n*",
        "",
        result,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    prefixes = [
        r"^(?:the\s+)?english\s+translation:?\s*\n*",
        r"^(?:the\s+)?translation:?\s*\n*",
        r"^translated\s+(?:text|content):?\s*\n*",
        r"^##?\s*(?:english\s+)?translation\s*\n+",
        r"^---+\s*\n+",
        r"^\*\*Translation:?\*\*\s*\n*",
    ]
    for pattern in prefixes:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE | re.MULTILINE)

    result = re.sub(r"\n*-{3,}\s*$", "", result)
    return result.strip()


def normalize_detected_language(detected_lang: str | None, page_text: str) -> str:
    lowered = (detected_lang or "und").strip().lower()
    normalized = normalize_language_tag(LANG_MAP.get(lowered, lowered))
    if normalized and normalized not in {"unknown", "un", "und", "xx", "zl"}:
        return normalized
    script = analyze_script(page_text, min_chars=1, min_ratio=0.3)
    return script.language if script.is_non_english and not script.ambiguous else "und"


async def detect_page_languages(
    pages: list[dict],
    log: Optional[Callable[..., None]] = None,
    config: Optional[TranslationConfig] = None,
) -> dict[int, str]:
    """Decide each page's language, gating on script regex before any model call.

    Use script as evidence where possible. Use language detection for Latin
    and other scripts without a reliable script-derived language.
    """
    config = config or load_translation_config()

    if config.script_gate_enabled:
        return await _detect_via_script_gate(pages, config, log=log)

    if log:
        log("Script gate DISABLED — falling back to per-line pyfranc detection for all pages")
    return await _detect_via_lang_detect(pages, log=log)


async def _detect_via_script_gate(
    pages: list[dict],
    config: TranslationConfig,
    log: Optional[Callable[..., None]] = None,
) -> dict[int, str]:
    detected_languages: dict[int, str] = {}
    non_english: list[int] = []
    ambiguous: list[int] = []

    if log:
        log(
            "Script gate: scanning %s page(s) with regex (min_chars=%s min_ratio=%s)",
            len(pages),
            config.script_min_chars,
            config.script_min_ratio,
        )

    for i, page in enumerate(pages):
        text = page.get("edited_markdown") or page.get("original_markdown", "")
        page_no = page.get("page_number", i + 1)
        analysis = analyze_script(
            text,
            min_chars=config.script_min_chars,
            min_ratio=config.script_min_ratio,
        )

        if not analysis.is_non_english:
            language = detect_any(text) if analysis.letter_chars >= 80 else None
            detected_languages[i] = normalize_detected_language(language, text) if language else "und"
            if log:
                log("Page %s: language=%s", page_no, detected_languages[i])
            continue

        detected_languages[i] = analysis.language
        non_english.append(page_no)
        if log:
            log("Page %s: regex → %s → TRANSLATE", page_no, analysis.summary())
        if analysis.ambiguous:
            ambiguous.append(i)

    if ambiguous:
        await _disambiguate_languages(pages, ambiguous, detected_languages, log=log)

    if log:
        log(
            "Script gate result: %s/%s page(s) identified by script %s; %s evaluated separately",
            len(non_english),
            len(pages),
            non_english or "[]",
            len(pages) - len(non_english),
        )

    return detected_languages


async def _disambiguate_languages(
    pages: list[dict],
    indices: list[int],
    detected_languages: dict[int, str],
    log: Optional[Callable[..., None]] = None,
) -> None:
    """Refine shared-script pages (e.g. Devanagari → hi vs mr) via pyfranc.

    Runs in-process — no network call, nothing to be unavailable — but a
    lookup failure on one line is still non-fatal: the script-derived default
    already translates correctly, so we just keep it and move on.
    """
    if log:
        log(
            "Script gate: %s page(s) on a shared script, disambiguating with pyfranc",
            len(indices),
        )

    for i in indices:
        page = pages[i]
        page_no = page.get("page_number", i + 1)
        default_lang = detected_languages[i]
        text = page.get("edited_markdown") or page.get("original_markdown", "")
        lines = [line.strip() for line in text.split("\n") if len(line.strip()) >= 10]

        result = disambiguate(default_lang, lines)

        if result.winner:
            detected_languages[i] = result.winner
            if log:
                log(
                    "Page %s: pyfranc refined %s → %s (votes=%s)",
                    page_no,
                    default_lang,
                    result.winner,
                    result.votes,
                )
        elif result.attempted and not result.votes and log:
            log(
                "Page %s: pyfranc found nothing in the %s family, keeping %s",
                page_no,
                "/".join(result.family),
                default_lang,
            )


async def _detect_via_lang_detect(
    pages: list[dict],
    log: Optional[Callable[..., None]] = None,
) -> dict[int, str]:
    """Detect the language of each page as a whole, leaving short pages unknown."""
    detected_languages: dict[int, str] = {}

    for i, page in enumerate(pages):
        text = page.get("edited_markdown") or page.get("original_markdown", "")
        if not text or len(text.strip()) < 20:
            detected_languages[i] = "und"
            continue
        detected_languages[i] = normalize_detected_language(detect_any(text), text)

    return detected_languages


async def translate_pages(
    pages: list[dict],
    *,
    target_language: str = "en",
    config: Optional[TranslationConfig] = None,
    log: Optional[Callable[..., None]] = None,
) -> list[dict]:
    config = config or load_translation_config(target_language=target_language)
    provider = None if config.provider == "none" else get_translation_provider(config)

    if log:
        log("Processing %s pages for translation", len(pages))
        log("Using translation provider=%s model=%s", config.provider, config.model)
        log(
            "Translation runtime config: concurrency=%s max_retries=%s retry_base_seconds=%s target_language=%s",
            config.page_concurrency,
            config.max_retries,
            config.retry_base_seconds,
            config.target_language,
        )

    declared = {
        i: language for i, page in enumerate(pages)
        if (language := normalize_language_tag((page.get("metadata") or {}).get("declared_language")))
    }
    missing = [i for i in range(len(pages)) if i not in declared]
    detected_languages = dict(declared)
    if missing:
        detected = await detect_page_languages([pages[i] for i in missing], log=log, config=config)
        detected_languages.update({missing[i]: language for i, language in detected.items()})

    pages_to_translate: list[tuple[int, dict, str]] = []
    for i, page in enumerate(pages):
        if i not in detected_languages:
            continue
        detected_lang = detected_languages[i]
        page["detected_language"] = detected_lang
        target = config.target_language
        variant = (page.get("translations") or {}).get(target) or {}
        already_available = bool(variant.get("edited_translation") or variant.get("translated_markdown"))
        if (provider and not already_available and detected_lang not in {"und", "mul", target}
                and not detected_lang.startswith(f"{target}-")):
            pages_to_translate.append((i, page, detected_lang))

    if log:
        log(
            "Found %s of %s page(s) needing translation: %s",
            len(pages_to_translate),
            len(pages),
            [p.get("page_number") for _, p, _ in pages_to_translate] or "none",
        )

    semaphore = asyncio.Semaphore(config.page_concurrency)

    async def translate_page(idx: int, page: dict, lang: str) -> tuple[int, str | None, str | None]:
        async with semaphore:
            text = page.get("edited_markdown") or page.get("original_markdown", "")
            if log:
                log("Translating page %s from %s", page.get("page_number"), lang)
            for attempt in range(1, config.max_retries + 1):
                try:
                    raw_translation = await asyncio.to_thread(
                        provider.translate,
                        text,
                        source_lang=lang,
                        target_language=config.target_language,
                    )
                    return (idx, clean_translation(raw_translation), None)
                except Exception as exc:
                    error_text = str(exc)
                    is_rate_limited = "429" in error_text or "rate limit" in error_text.lower()
                    is_retryable = is_rate_limited or any(
                        token in error_text.lower()
                        for token in ("timeout", "timed out", "connection", "503", "502", "504", "unavailable")
                    )
                    if is_retryable and attempt < config.max_retries:
                        backoff_seconds = config.retry_base_seconds * (2 ** (attempt - 1))
                        if log:
                            log(
                                "Translation error for page %s on attempt %s/%s (%s), retrying in %.1fs",
                                page.get("page_number"),
                                attempt,
                                config.max_retries,
                                error_text,
                                backoff_seconds,
                            )
                        await asyncio.sleep(backoff_seconds)
                        continue
                    if log:
                        log("Translation error for page %s: %s", page.get("page_number"), exc)
                    return (idx, None, error_text)
            return (idx, None, "translation failed")

    results = (
        await asyncio.gather(*[translate_page(i, p, lang) for i, p, lang in pages_to_translate])
        if pages_to_translate
        else []
    )

    translated_count = 0
    translated_at = datetime.utcnow().isoformat()
    failures: list[str] = []
    for idx, translation, error in results:
        if translation:
            pages[idx]["translated_markdown"] = translation
            pages[idx]["translation_provider"] = config.provider
            pages[idx]["translation_model"] = config.model
            pages[idx]["translation_target_language"] = config.target_language
            pages[idx]["translated_at"] = translated_at
            variants = dict(pages[idx].get("translations") or {})
            variants[config.target_language] = {
                "translated_markdown": translation,
                "provider": config.provider,
                "model": config.model,
                "translated_at": translated_at,
            }
            pages[idx]["translations"] = variants
            translated_count += 1
        elif error:
            page_no = pages[idx].get("page_number", idx)
            failures.append(f"page {page_no}: {error}")

    if failures:
        raise RuntimeError(
            f"Translation failed for {len(failures)}/{len(pages_to_translate)} page(s). "
            f"First error: {failures[0]}"
        )

    if log:
        log(
            "Translation complete: %s/%s pages translated",
            translated_count,
            len(pages_to_translate),
        )
    return pages
