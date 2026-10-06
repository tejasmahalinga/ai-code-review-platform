"""Consolidates repeated findings within one review (review quality).

Chunks are reviewed independently, so the same problem can come back several times: twice for nearly the
same lines, or once per file for a repeated pattern (e.g. a hard-coded key in ten config files). Posting all
of them is noise. We keep the best instances and mark the rest; they stay visible on the dashboard and the
summary says how often the issue repeats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NEAR_LINES = 3
MIN_TITLE_SIMILARITY = 0.5
MAX_REPEATS = 3  # instances of one pattern posted before the rest are consolidated

_QUOTED = re.compile(r"`[^`]*`|'[^']*'|\"[^\"]*\"")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "a",
    "an",
    "the",
    "in",
    "of",
    "to",
    "on",
    "for",
    "is",
    "and",
    "or",
    "with",
    "this",
    "be",
    "may",
    "can",
}


@dataclass(frozen=True)
class Item:
    key: int
    path: str
    line_start: int | None
    line_end: int | None
    category: str
    rule_id: str
    title: str
    rank: tuple[int, float]  # (severity rank, confidence): higher is better


def _words(title: str) -> set[str]:
    return {w for w in _WORD.findall(_QUOTED.sub(" ", title.lower())) if w not in _STOP}


def pattern_key(item: Item) -> str:
    """Findings with the same rule, or the same category and identifier-free title, describe one pattern."""
    if item.rule_id:
        return f"rule:{item.rule_id}"
    return f"{item.category}:{' '.join(sorted(_words(item.title)))}"


def _similar(a: Item, b: Item) -> bool:
    if a.path != b.path or a.category != b.category:
        return False
    if a.line_end is None or b.line_end is None:
        return a.line_end == b.line_end and _words(a.title) == _words(b.title)
    a_start, b_start = a.line_start or a.line_end, b.line_start or b.line_end
    if a_start > b.line_end + NEAR_LINES or b_start > a.line_end + NEAR_LINES:
        return False
    wa, wb = _words(a.title), _words(b.title)
    union = wa | wb
    return bool(union) and len(wa & wb) / len(union) >= MIN_TITLE_SIMILARITY


@dataclass
class Consolidation:
    duplicates: dict[int, int]  # same issue reported twice: key -> key kept
    repeats: dict[int, int]  # further instances of a repeated pattern: key -> key kept


def consolidate(items: list[Item]) -> Consolidation:
    ordered = sorted(items, key=lambda i: i.rank, reverse=True)
    duplicates: dict[int, int] = {}
    kept: list[Item] = []
    for item in ordered:  # near-duplicates: same place, same issue
        primary = next((k for k in kept if _similar(k, item)), None)
        if primary is None:
            kept.append(item)
        else:
            duplicates[item.key] = primary.key
    groups: dict[str, list[Item]] = {}
    for item in kept:  # repeated patterns across the change
        groups.setdefault(pattern_key(item), []).append(item)
    repeats = {item.key: group[0].key for group in groups.values() for item in group[MAX_REPEATS:]}
    return Consolidation(duplicates=duplicates, repeats=repeats)
