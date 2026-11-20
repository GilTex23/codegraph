"""Token accounting for returned text. The vocabulary ships in the dependency."""

from functools import cache

import puretiktoken


@cache
def _encoding():
    return puretiktoken.get_encoding("o200k_base")


def token_count(text: str) -> int:
    return len(_encoding().encode_ordinary(text))


def fit_lines(text: str, limit: int) -> str:
    """Keep complete evidence lines and include the truncation mark in the budget."""
    if token_count(text) <= limit:
        return text
    marker = "…"
    lines = text.splitlines()
    low, high = 0, len(lines)
    while low < high:
        mid = (low + high + 1) // 2
        if token_count("\n".join([*lines[:mid], marker])) <= limit:
            low = mid
        else:
            high = mid - 1
    return "\n".join([*lines[:low], marker])
