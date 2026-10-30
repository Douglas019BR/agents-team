"""Glob matching with ``**`` semantics, used to check which files an agent changed."""

from __future__ import annotations

import re
from functools import cache


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def match(path: str, pattern: str) -> bool:
    """True when the relative ``path`` matches ``pattern`` (``**`` crosses directories)."""
    return _compile(pattern.lstrip("/")).match(path.lstrip("/")) is not None


def match_any(path: str, patterns: list[str]) -> bool:
    return any(match(path, p) for p in patterns)
