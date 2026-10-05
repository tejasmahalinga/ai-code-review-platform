"""Unified-diff parsing and line mapping (RE-02).

GitHub returns per-file ``patch`` strings containing only hunks (no ``---``/``+++`` headers). We parse
them into hunks with old/new line numbers so that (a) the LLM sees new-file line numbers it can cite,
and (b) every finding can be checked against the set of lines GitHub accepts inline comments on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
LINE_NUMBER_WIDTH = 6


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "+" added, "-" removed, " " context
    text: str
    old: int | None
    new: int | None


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    section: str
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_len} +{self.new_start},{self.new_len} @@{self.section}"


@dataclass
class FileDiff:
    path: str
    status: str
    hunks: list[Hunk]
    additions: int = 0
    deletions: int = 0
    previous_path: str | None = None

    @property
    def commentable_lines(self) -> set[int]:
        """New-file lines GitHub accepts RIGHT-side review comments on (added + context)."""
        return {line.new for hunk in self.hunks for line in hunk.lines if line.new is not None}

    @property
    def added_lines(self) -> set[int]:
        return {
            line.new
            for hunk in self.hunks
            for line in hunk.lines
            if line.kind == "+" and line.new is not None
        }

    def new_line_text(self) -> dict[int, str]:
        return {line.new: line.text for hunk in self.hunks for line in hunk.lines if line.new is not None}

    def context_around(self, line: int, radius: int = 3) -> str:
        """New-side text near ``line`` (only lines visible in the diff), used for fingerprints."""
        texts = self.new_line_text()
        return "\n".join(texts[n] for n in range(line - radius, line + radius + 1) if n in texts)

    def header(self, part: str = "") -> str:
        rename = (
            f" (renamed from {self.previous_path})" if self.previous_path and self.status == "renamed" else ""
        )
        return f"### File: {self.path} ({self.status}){rename}{part}"


def parse_patch(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    current: Hunk | None = None
    old_no = new_no = 0
    for raw in patch.splitlines():
        match = HUNK_HEADER.match(raw)
        if match:
            old_start, old_len, new_start, new_len, section = match.groups()
            current = Hunk(
                old_start=int(old_start),
                old_len=int(old_len) if old_len is not None else 1,
                new_start=int(new_start),
                new_len=int(new_len) if new_len is not None else 1,
                section=section or "",
            )
            hunks.append(current)
            old_no, new_no = current.old_start, current.new_start
            continue
        if current is None or raw.startswith("\\"):
            # Text before the first hunk, or "\ No newline at end of file".
            continue
        kind, text = (raw[0], raw[1:]) if raw else (" ", "")
        if kind == "+":
            current.lines.append(DiffLine("+", text, None, new_no))
            new_no += 1
        elif kind == "-":
            current.lines.append(DiffLine("-", text, old_no, None))
            old_no += 1
        else:
            current.lines.append(DiffLine(" ", text if kind == " " else raw, old_no, new_no))
            old_no += 1
            new_no += 1
    return hunks


def render_lines(lines: list[DiffLine]) -> list[str]:
    out = []
    for line in lines:
        if line.kind == "-":
            out.append(f"{'':>{LINE_NUMBER_WIDTH}} - {line.text}")
        else:
            out.append(f"{line.new:>{LINE_NUMBER_WIDTH}} {line.kind} {line.text}".rstrip())
    return out


def render_hunk(hunk: Hunk, lines: list[DiffLine] | None = None) -> str:
    return "\n".join([hunk.header, *render_lines(lines if lines is not None else hunk.lines)])
