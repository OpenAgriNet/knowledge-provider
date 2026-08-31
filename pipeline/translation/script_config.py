"""Loading and compiling the script → language config JSON.

Split out from ``script_detect.py`` so config loading (file I/O, the
SCRIPT_FAMILIES_CONFIG_PATH override, JSON parsing) can be unit-tested on its
own, independent of the regex-matching logic that consumes it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).with_name("script_families.json")


def load_config() -> dict:
    """Read the script → language config JSON.

    Defaults to the bundled ``script_families.json``; SCRIPT_FAMILIES_CONFIG_PATH,
    if set, fully replaces it (no merging) — a deployer supplies their own
    complete table.
    """
    override = os.environ.get("SCRIPT_FAMILIES_CONFIG_PATH", "").strip()
    path = Path(override) if override else DEFAULT_CONFIG_PATH
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def compile_scripts(raw: dict) -> tuple[tuple[str, str, re.Pattern, tuple[str, ...]], ...]:
    """Compile the "scripts" array into (lang, script_name, regex, family) tuples."""
    compiled = []
    for entry in raw["scripts"]:
        char_ranges = "".join(
            f"{chr(int(start, 16))}-{chr(int(end, 16))}" for start, end in entry["ranges"]
        )
        pattern = re.compile(f"[{char_ranges}]")
        compiled.append((entry["lang"], entry["script"], pattern, tuple(entry.get("family", ()))))
    return tuple(compiled)


def compile_neutral(raw: dict) -> re.Pattern:
    """Compile "neutral_codepoints" into a regex matching characters that
    carry no language signal on their own (danda punctuation, ₹, ...)."""
    points = raw.get("neutral_codepoints", {}).get("points", [])
    chars = "".join(chr(int(p, 16)) for p in points)
    return re.compile(f"[{re.escape(chars)}]") if chars else re.compile(r"(?!)")


def extract_iso3_map(raw: dict) -> dict[str, str]:
    """Our short language codes → ISO 639-3, straight from the "iso3" map."""
    return dict(raw.get("iso3", {}))


@dataclass
class ScriptConfig:
    """Fully assembled, ready-to-use script-detection config — everything
    script_detect.py needs, already loaded and compiled."""

    compiled: tuple[tuple[str, str, re.Pattern, tuple[str, ...]], ...]
    lang_to_iso3: dict[str, str]
    iso3_to_lang: dict[str, str]
    neutral: re.Pattern


def load_script_config() -> ScriptConfig:
    """Load, compile, and assemble the full config in one call.

    This is the only function script_detect.py needs — it doesn't call
    load_config()/compile_scripts()/etc. itself, so it never has to know how
    raw config becomes usable structures, only that this function gives it
    some.
    """
    raw = load_config()
    lang_to_iso3 = extract_iso3_map(raw)
    return ScriptConfig(
        compiled=compile_scripts(raw),
        lang_to_iso3=lang_to_iso3,
        iso3_to_lang={v: k for k, v in lang_to_iso3.items()},
        neutral=compile_neutral(raw),
    )
