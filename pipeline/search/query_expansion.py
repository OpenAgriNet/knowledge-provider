"""Literal search expansions supplied by a deployment."""

import json
import os


def expand_query(query: str, profile: str) -> str:
    text = query.strip()
    if not text or not profile or profile == "none":
        return text
    profiles = json.loads(os.environ.get("QUERY_EXPANSION_RULES", "{}"))
    if not isinstance(profiles, dict):
        raise ValueError("QUERY_EXPANSION_RULES must be an object of named profiles")
    rules = profiles.get(profile, {})
    if not isinstance(rules, dict) or len(rules) > 128:
        raise ValueError("A query expansion profile must map at most 128 phrases to text")
    lower = text.casefold()
    additions = []
    for phrase, expansion in rules.items():
        if not phrase.strip() or not isinstance(expansion, str) or len(expansion) > 256:
            raise ValueError("Query expansion phrases must have bounded text")
        if phrase.casefold() in lower:
            additions.append(expansion.strip())
    return " ".join([text, *dict.fromkeys(additions)])[:4096]
