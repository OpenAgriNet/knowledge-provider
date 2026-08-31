"""Regex script detection — decides which pages actually need translation.

Per-line language detection (the old lang-detect service, and pyfranc when
run unrestricted) is unreliable on the short, noisy lines OCR produces
(headings, table fragments, page numbers). A single misdetected line used to
mark a whole page non-English, so English pages were sent to the translation
model as Swahili/German/Hungarian/French/Romanian.

This module gates that decision on what the page is actually written in: the
Unicode block of its characters. Documents in this corpus are English plus
Indic scripts, and every Indic language has its own block, so a regex over
those ranges answers "is this page non-English" without a model call and
without false positives from Latin-script noise.

Script → language is 1:1 except Devanagari (Hindi/Marathi/…) and Bengali
(Bengali/Assamese); those stay ambiguous here and are handed to pyfranc to
disambiguate, but only for pages this gate has already flagged.

The script → language table itself lives in ``script_families.json`` next to
this file, not in code: adding a new unambiguous script (say, Myanmar for
Burmese) is a JSON edit, not a Python change. Adding a language to an
*already-ambiguous* script (a second Bengali-family language, say) also needs
an ISO 639-3 code added to that same file's ``iso3`` map, since the
disambiguation library (pyfranc) is ISO-639-3-based — see ``iso3_map()``
below. A deployer can point SCRIPT_FAMILIES_CONFIG_PATH at their own JSON
file (same shape) to replace the table entirely without touching this repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .script_config import compile_neutral, compile_scripts, extract_iso3_map, load_config

_RAW_CONFIG = load_config()
_COMPILED = compile_scripts(_RAW_CONFIG)
_ISO3_MAP: dict[str, str] = extract_iso3_map(_RAW_CONFIG)
# Code points that carry no language signal on their own (e.g. the Devanagari
# danda ।/॥, the ₹ sign) and would otherwise push an otherwise-English page
# over the detection threshold by themselves. Sourced from the same config
# file/override as the script table — see "neutral_codepoints" there.
_NEUTRAL = compile_neutral(_RAW_CONFIG)


def iso3_map() -> dict[str, str]:
    """Our short language codes → ISO 639-3, for pyfranc's whitelist/results.

    Sourced from the same config file as the script table, so a deployer
    adding a language to an ambiguous family's ``family`` list adds its ISO
    639-3 code here too, in the same edit.
    """
    return dict(_ISO3_MAP)

DEFAULT_MIN_CHARS = 15
DEFAULT_MIN_RATIO = 0.05


@dataclass
class ScriptAnalysis:
    """Outcome of the regex gate for one page."""

    is_non_english: bool
    language: str = "en"
    script: str = "Latin"
    script_chars: int = 0
    letter_chars: int = 0
    ratio: float = 0.0
    ambiguous: bool = False
    candidates: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    def summary(self) -> str:
        """One-line, log-friendly description of the decision."""
        if not self.is_non_english:
            return (
                f"English (script={self.script} non_latin_chars={self.script_chars} "
                f"letters={self.letter_chars} ratio={self.ratio:.3f}) — {self.reason}"
            )
        return (
            f"non-English lang={self.language} script={self.script} "
            f"chars={self.script_chars} letters={self.letter_chars} ratio={self.ratio:.3f}"
            + (f" ambiguous_within={'/'.join(self.candidates)}" if self.ambiguous else "")
        )


def analyze_script(
    text: str,
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> ScriptAnalysis:
    """Classify a page as English or non-English from its character ranges.

    A page counts as non-English only when it clears *both* thresholds: an
    absolute count (so one stray glyph is not enough) and a share of all
    letters (so a mostly-English page with a single decorative word is not
    shipped off for translation).
    """
    if not text or not text.strip():
        return ScriptAnalysis(False, reason="empty page")

    scrubbed = _NEUTRAL.sub("", text)
    letters = sum(1 for ch in scrubbed if ch.isalpha())

    counts: dict[str, int] = {}
    for lang, name, pattern, family in _COMPILED:
        found = len(pattern.findall(scrubbed))
        if found:
            counts[name] = found

    if not counts:
        return ScriptAnalysis(
            False,
            letter_chars=letters,
            reason="no non-Latin script found",
        )

    dominant_script = max(counts, key=lambda k: counts[k])
    script_chars = counts[dominant_script]
    lang, _name, _pattern, family = next(s for s in _COMPILED if s[1] == dominant_script)

    # Ratio is against all letters so Latin-heavy pages score low.
    ratio = script_chars / letters if letters else 1.0

    if script_chars < min_chars:
        return ScriptAnalysis(
            False,
            script=dominant_script,
            script_chars=script_chars,
            letter_chars=letters,
            ratio=ratio,
            reason=f"only {script_chars} {dominant_script} char(s), below min_chars={min_chars}",
        )

    if ratio < min_ratio:
        return ScriptAnalysis(
            False,
            script=dominant_script,
            script_chars=script_chars,
            letter_chars=letters,
            ratio=ratio,
            reason=f"{dominant_script} ratio {ratio:.3f} below min_ratio={min_ratio}",
        )

    return ScriptAnalysis(
        True,
        language=lang,
        script=dominant_script,
        script_chars=script_chars,
        letter_chars=letters,
        ratio=ratio,
        ambiguous=bool(family),
        candidates=family,
    )


def script_family(language: str) -> tuple[str, ...]:
    """Languages sharing a script with ``language`` (empty when unambiguous)."""
    for lang, _name, _pattern, family in _COMPILED:
        if lang == language:
            return family
    return ()
