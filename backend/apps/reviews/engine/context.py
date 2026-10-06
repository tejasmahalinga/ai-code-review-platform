"""Surrounding code for changed hunks (review quality).

A diff hunk shows only a few lines around a change, so the model often cannot see the function it is in or
what is imported. For modified files we add a compact, read-only excerpt of the new version: the import
block and the signature (plus the lines leading up to the hunk) of the enclosing function or class. Lines
already shown in the diff are left out. The excerpt is language-agnostic (indentation and keyword
heuristics) and size-capped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from apps.reviews.engine.diff import Hunk

DEFINITION = re.compile(
    r"""^\s*(?:
        (?:export\s+)?(?:default\s+)?(?:async\s+)?function\b          # JS/TS, PHP
      | (?:async\s+)?def\s                                             # Python, Ruby
      | class\s | module\s | interface\s | trait\s | enum\s | struct\s | impl\b
      | func\s | fun\s                                                 # Go, Swift, Kotlin
      | (?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s                     # Rust
      | (?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>   # arrow functions
      | (?:(?:public|private|protected|internal|static|final|abstract|override|virtual|async)\s+)+
        [\w<>\[\],.?]+\s+\w+\s*\(                                      # Java, C#, Kotlin methods
    )""",
    re.VERBOSE,
)
IMPORT = re.compile(
    r"^\s*(?:import\s|from\s+\S+\s+import\s|#include\s|using\s|use\s|require[\s(]|package\s"
    r"|(?:const|let|var)\s+\S+\s*=\s*require\()"
)
COMMENT_OR_BLANK = re.compile(r"^\s*(?:$|#(?!include)|//|/\*|\*|--)")
MAX_LOOKBACK = 120  # lines searched above a hunk for its enclosing definition
MAX_LEAD_IN = 12  # lines shown right above a hunk when the definition is far away
MAX_IMPORT_LINES = 30


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" \t"))


def _import_range(lines: list[str]) -> tuple[int, int] | None:
    first = last = None
    for number, text in enumerate(lines[:80], start=1):
        if IMPORT.match(text):
            first = first or number
            last = number
        elif not COMMENT_OR_BLANK.match(text) and first is not None:
            break
    if first is None or last is None:
        return None
    return first, min(last, first + MAX_IMPORT_LINES - 1)


def _enclosing_definition(lines: list[str], hunk: Hunk) -> int | None:
    new_side = [line.text for line in hunk.lines if line.new is not None and line.text.strip()]
    hunk_indent = min((_indent(t) for t in new_side), default=0)
    start = max(hunk.new_start, 1)
    for number in range(start - 1, max(start - MAX_LOOKBACK, 0), -1):
        text = lines[number - 1] if number - 1 < len(lines) else ""
        if DEFINITION.match(text) and (_indent(text) < hunk_indent or _indent(text) == 0):
            return number
    return None


def _merge(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(r for r in ranges if r[0] <= r[1]):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def extract_context(source: str, hunks: list[Hunk], *, max_lines: int = 80) -> str:
    """Numbered excerpt of ``source`` (the new file version) that helps review ``hunks``; may be empty."""
    lines = source.splitlines()
    if not lines or not hunks:
        return ""
    shown = {line.new for hunk in hunks for line in hunk.lines if line.new is not None}
    wanted: list[tuple[int, int]] = []
    imports = _import_range(lines)
    if imports:
        wanted.append(imports)
    for hunk in hunks:
        definition = _enclosing_definition(lines, hunk)
        if definition is None:
            continue
        before = hunk.new_start - 1
        if before - definition <= MAX_LEAD_IN * 2:
            wanted.append((definition, before))
        else:
            wanted.append((definition, definition + 2))  # the signature
            wanted.append((before - MAX_LEAD_IN + 1, before))

    out: list[str] = []
    budget = max_lines
    for start, end in _merge(wanted):
        numbers = [n for n in range(max(start, 1), min(end, len(lines)) + 1) if n not in shown]
        if not numbers:
            continue
        if out:
            out.append("   ⋮")
        for number in numbers:
            if budget <= 0:
                out.append("   ⋮ (truncated)")
                return "\n".join(out)
            out.append(f"{number:>5} | {lines[number - 1]}")
            budget -= 1
    return "\n".join(out)
