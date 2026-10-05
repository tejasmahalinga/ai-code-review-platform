"""Packs file diffs into LLM-sized chunks (RE-03).

Guarantees: every hunk line appears in exactly one chunk, and no chunk exceeds the token budget (as
estimated) unless a single diff line alone is larger than the budget. Token counts are tracked as
running upper bounds (sum of parts + separators) so packing stays linear in the diff size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from apps.reviews.engine.diff import FileDiff, Hunk, render_lines

CHARS_PER_TOKEN = 3.5  # Conservative for source code; providers report exact usage afterwards.


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass
class Segment:
    path: str
    text: str
    tokens: int


@dataclass
class Chunk:
    index: int
    segments: list[Segment] = field(default_factory=list)
    tokens: int = 0  # upper bound for estimate_tokens(self.text)

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.segments)

    @property
    def paths(self) -> list[str]:
        return list(dict.fromkeys(s.path for s in self.segments))


def _hunk_pieces(hunk: Hunk, budget: int) -> list[str]:
    """Renders a hunk as one or more pieces, each (with the hunk header) within ``budget``."""
    header = hunk.header
    header_tokens = estimate_tokens(header) + 1
    rendered = render_lines(hunk.lines)
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = header_tokens
    for text in rendered:
        line_tokens = estimate_tokens(text) + 1
        if current and current_tokens + line_tokens > budget:
            pieces.append("\n".join([header, *current]))
            current, current_tokens = [], header_tokens
        current.append(text)
        current_tokens += line_tokens
    if current or not pieces:
        pieces.append("\n".join([header, *current]))
    return pieces


def file_segments(diff: FileDiff, budget: int) -> list[Segment]:
    header = diff.header(" [part 99]")  # longest header variant, for budgeting
    header_tokens = estimate_tokens(header) + 1
    pieces = [piece for hunk in diff.hunks for piece in _hunk_pieces(hunk, budget - header_tokens)]

    groups: list[list[str]] = []
    group: list[str] = []
    group_tokens = header_tokens
    for piece in pieces:
        piece_tokens = estimate_tokens(piece) + 1
        if group and group_tokens + piece_tokens > budget:
            groups.append(group)
            group, group_tokens = [], header_tokens
        group.append(piece)
        group_tokens += piece_tokens
    if group or not groups:
        groups.append(group)

    segments = []
    for index, parts in enumerate(groups):
        title = diff.header() if len(groups) == 1 else diff.header(f" [part {index + 1}/{len(groups)}]")
        text = "\n".join([title, *parts])
        segments.append(Segment(diff.path, text, estimate_tokens(text)))
    return segments


def build_chunks(diffs: list[FileDiff], budget_tokens: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    current = Chunk(index=0)
    for diff in diffs:
        for segment in file_segments(diff, budget_tokens):
            if current.segments and current.tokens + segment.tokens + 1 > budget_tokens:
                chunks.append(current)
                current = Chunk(index=len(chunks))
            current.tokens += segment.tokens + (1 if current.segments else 0)
            current.segments.append(segment)
    if current.segments:
        chunks.append(current)
    return chunks
