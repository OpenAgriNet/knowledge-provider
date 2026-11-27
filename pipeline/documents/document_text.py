"""Shared document text normalization and passage evidence helpers."""

import re


def clean_html_tags(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def clean_latex_notation(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = re.sub(r"\[\^[0-9]+\]", "", text)
    text = re.sub(r"\$\s*\{\s*\}\s*\^\{[0-9]+\}\s*\$", "", text)
    text = re.sub(r"\$\s*\^\{[0-9]+\}\s*\$", "", text)
    text = re.sub(r"\$\s*\$", "", text)
    text = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", text)
    text = re.sub(r"\\[a-zA-Z]+", "", text)
    text = re.sub(r"[\\{}]", "", text)
    return text


def format_table_content(text: str) -> str:
    if not isinstance(text, str):
        return text
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if re.match(r"^[\s\|]*$", line):
            continue
        if re.match(r"^[\s\|\-\:]*$", line):
            continue
        line = re.sub(r"\|\s*\|", "|", line)
        line = re.sub(r"^\|\s*", "", line)
        line = re.sub(r"\s*\|$", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def clean_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = clean_html_tags(text)
    text = clean_latex_notation(text)
    text = format_table_content(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n\s*\n\s*\n", "\n\n", text)
    text = re.sub(r"\n ", "\n", text)
    text = re.sub(r" \n", "\n", text)
    return text.strip()


def _infer_section(text: str, section_title: str | None = None) -> str:
    """Best-effort section heading for chunk provenance."""
    if section_title and str(section_title).strip():
        return str(section_title).strip()
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def is_reference_section(text: str) -> bool:
    """
    Detect if text is primarily a reference/bibliography section.
    Returns True if the text appears to be citations/references.
    """
    if not text or len(text) < 50:
        return False

    ref_headers = [
        r"^\s*#{1,3}\s*(?:references|bibliography|citations|works cited|literature cited)\s*$",
        r"^\s*\*{1,2}(?:references|bibliography)\*{1,2}\s*$",
    ]
    for pattern in ref_headers:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            return True

    lines = text.split("\n")
    total_lines = len([line for line in lines if line.strip()])
    if total_lines == 0:
        return False

    citation_patterns = [
        r"^\s*\d{1,3}[\.\)]\s+[A-Z][a-z]+[\s,].*(?:\d{4}|\(\d{4}\))",
        r"doi[:\s]*10\.\d{4,}",
        r"(?:J\.|Journal|Int\.|Proceedings|Trans\.).*\d{4}",
        r"\(\d{4}\)\s*$",
        r"\bet\s+al\b",
        r"(?:Vol\.?\s*\d+|\d+\s*\(\d+\)\s*:)",
        r"(?:pp?\.?\s*\d+[-–]\d+|:\s*\d+[-–]\d+)",
    ]

    citation_line_count = 0
    for line in lines:
        if not line.strip():
            continue
        for pattern in citation_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                citation_line_count += 1
                break

    return (citation_line_count / total_lines) > 0.4
