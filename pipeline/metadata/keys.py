"""Shared lookup keys for admitted and reviewed metadata labels."""

import unicodedata


def normalized_key(label: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", label).casefold().split())


def season_key(label: str) -> str:
    key = normalized_key(label)
    return key.removesuffix(" season") or key
