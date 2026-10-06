"""Offline evaluation of review quality (review quality / RE-18).

A case is a small pull request with labelled expectations: issues a good review must find, and places where
it must stay quiet. ``evaluate`` runs the real review engine (prompts, context, chunking, LLM, parsing,
anchoring, consolidation) against a provider and scores the result, so prompt or model changes can be compared
with numbers instead of anecdotes. Run it with ``manage.py evaluate_reviews``.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from apps.llm.base import LLMProvider
from apps.repositories.models import SEVERITY_RANK
from apps.reviews.engine import prompts
from apps.reviews.engine.chunker import build_chunks, estimate_tokens
from apps.reviews.engine.consolidate import Item, consolidate
from apps.reviews.engine.context import extract_context
from apps.reviews.engine.diff import FileDiff, parse_patch
from apps.reviews.engine.profiles import get_profile

LINE_TOLERANCE = 3
CASE_SUFFIXES = (".yaml", ".yml")


class CaseError(ValueError):
    pass


@dataclass
class Expectation:
    path: str
    lines: tuple[int, int]
    category: str = ""
    min_severity: str = ""
    keywords: list[str] = field(default_factory=list)

    def matches(self, finding: dict[str, Any]) -> bool:
        if finding["path"] != self.path or finding["line_end"] is None:
            return False
        start = finding["line_start"] or finding["line_end"]
        if start > self.lines[1] + LINE_TOLERANCE or finding["line_end"] < self.lines[0] - LINE_TOLERANCE:
            return False
        if self.category and finding["category"] != self.category:
            return False
        if self.min_severity and SEVERITY_RANK[finding["severity"]] < SEVERITY_RANK[self.min_severity]:
            return False
        text = f"{finding['title']} {finding['body']}".lower()
        return not self.keywords or any(k.lower() in text for k in self.keywords)


@dataclass
class Quiet:
    """A region where any reported finding is a false positive."""

    path: str
    lines: tuple[int, int] | None = None  # None: the whole file

    def matches(self, finding: dict[str, Any]) -> bool:
        if finding["path"] != self.path:
            return False
        if self.lines is None or finding["line_end"] is None:
            return True
        start = finding["line_start"] or finding["line_end"]
        return start <= self.lines[1] and finding["line_end"] >= self.lines[0]


@dataclass
class Case:
    name: str
    title: str
    diffs: list[FileDiff]
    expected: list[Expectation]
    quiet: list[Quiet]
    description: str = ""


def _lines(value: Any, name: str) -> tuple[int, int]:
    if isinstance(value, int):
        return value, value
    if isinstance(value, list | tuple) and len(value) == 2 and all(isinstance(v, int) for v in value):
        return int(value[0]), int(value[1])
    raise CaseError(f"{name}: 'lines' must be a line number or [start, end]")


def parse_case(name: str, data: dict[str, Any]) -> Case:
    try:
        diffs = []
        for f in data["files"]:
            hunks = parse_patch(f["patch"])
            if not hunks:
                raise CaseError(f"{name}: {f['path']} has no hunks")
            diff = FileDiff(path=f["path"], status=f.get("status", "modified"), hunks=hunks)
            diff.context = str(f.get("source", ""))  # full new version; turned into an excerpt at run time
            diffs.append(diff)
        expected = [
            Expectation(
                path=e["path"],
                lines=_lines(e["lines"], name),
                category=e.get("category", ""),
                min_severity=e.get("min_severity", ""),
                keywords=list(e.get("keywords", [])),
            )
            for e in data.get("expected", [])
        ]
        quiet = [
            Quiet(path=q["path"], lines=_lines(q["lines"], name) if "lines" in q else None)
            for q in data.get("quiet", [])
        ]
    except KeyError as exc:
        raise CaseError(f"{name}: missing key {exc}") from exc
    if not expected and not quiet:
        raise CaseError(f"{name}: a case needs 'expected' or 'quiet' entries")
    return Case(name, str(data.get("title", name)), diffs, expected, quiet, str(data.get("description", "")))


def load_cases(directory: Path, only: list[str] | None = None) -> list[Case]:
    cases = []
    for path in sorted(p for p in directory.iterdir() if p.suffix in CASE_SUFFIXES):
        if only and path.stem not in only:
            continue
        cases.append(parse_case(path.stem, yaml.safe_load(path.read_text())))
    if not cases:
        raise CaseError(f"No cases found in {directory}")
    return cases


@dataclass
class CaseResult:
    name: str
    findings: list[dict[str, Any]]
    found: list[str]  # expectations met, as "path:start-end"
    missed: list[str]
    false_positives: int  # findings in quiet regions
    unexpected: int  # findings matching no expectation and no quiet region
    input_tokens: int
    output_tokens: int
    errors: list[str]
    seconds: float

    @property
    def expected_total(self) -> int:
        return len(self.found) + len(self.missed)


def run_case(
    case: Case,
    provider: LLMProvider,
    *,
    profile: str = "balanced",
    extended_context: bool = True,
    min_confidence: float = 0.5,
    chunk_tokens: int = 12_000,
) -> CaseResult:
    from apps.reviews.pipeline import _anchor, _review_chunks

    started = time.monotonic()
    diffs = [FileDiff(d.path, d.status, d.hunks) for d in case.diffs]
    for diff, original in zip(diffs, case.diffs, strict=True):
        if extended_context and original.context:
            diff.context = extract_context(original.context, diff.hunks)
    system = prompts.build_system_prompt(profile=get_profile(profile))
    overhead = estimate_tokens(system) + 200
    chunks = build_chunks(diffs, max(chunk_tokens - overhead, 1_000))
    results = _review_chunks(provider, chunks, system, case.title)
    by_path = {d.path: d for d in diffs}
    raw: list[dict[str, Any]] = []
    errors = [str(r.error) for r in results if r.error is not None]
    for result in results:
        for f in result.review.findings if result.review else []:
            if f.confidence < min_confidence:
                continue
            _anchored, start, end = _anchor(f, by_path.get(f.path))
            raw.append(
                {
                    "path": f.path,
                    "line_start": start,
                    "line_end": end,
                    "category": f.category,
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "title": f.title,
                    "body": f.body,
                    "rule_id": f.rule_id or "",
                }
            )
    grouped = consolidate(
        [
            Item(
                i,
                f["path"],
                f["line_start"],
                f["line_end"],
                f["category"],
                f["rule_id"],
                f["title"],
                (SEVERITY_RANK[f["severity"]], f["confidence"]),
            )
            for i, f in enumerate(raw)
        ]
    )
    findings = [f for i, f in enumerate(raw) if i not in grouped.duplicates and i not in grouped.repeats]

    found: list[str] = []
    missed: list[str] = []
    for e in case.expected:
        label = f"{e.path}:{e.lines[0]}-{e.lines[1]}"
        (found if any(e.matches(f) for f in findings) else missed).append(label)
    false_positives = sum(1 for f in findings if any(q.matches(f) for q in case.quiet))
    unexpected = sum(
        1
        for f in findings
        if not any(e.matches(f) for e in case.expected) and not any(q.matches(f) for q in case.quiet)
    )
    usage = [call[0] for r in results for call in r.calls]
    return CaseResult(
        name=case.name,
        findings=findings,
        found=found,
        missed=missed,
        false_positives=false_positives,
        unexpected=unexpected,
        input_tokens=sum(u.input_tokens for u in usage),
        output_tokens=sum(u.output_tokens for u in usage),
        errors=errors,
        seconds=round(time.monotonic() - started, 2),
    )


def summarize(results: list[CaseResult], *, model: str, settings: dict[str, Any]) -> dict[str, Any]:
    expected = sum(r.expected_total for r in results)
    found = sum(len(r.found) for r in results)
    reported = sum(len(r.findings) for r in results)
    matched = reported - sum(r.false_positives + r.unexpected for r in results)
    return {
        "model": model,
        "prompt_version": prompts.PROMPT_VERSION,
        "settings": settings,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "totals": {
            "cases": len(results),
            "expected": expected,
            "found": found,
            "recall": round(found / expected, 3) if expected else None,
            "reported": reported,
            "precision": round(matched / reported, 3) if reported else None,
            "false_positives": sum(r.false_positives for r in results),
            "unexpected": sum(r.unexpected for r in results),
            "errors": sum(len(r.errors) for r in results),
            "input_tokens": sum(r.input_tokens for r in results),
            "output_tokens": sum(r.output_tokens for r in results),
        },
        "cases": [asdict(r) for r in results],
    }


def markdown(report: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    t = report["totals"]

    def delta(key: str) -> str:
        if not baseline or baseline["totals"].get(key) is None or t.get(key) is None:
            return ""
        diff = t[key] - baseline["totals"][key]
        return f" ({diff:+.3f})" if isinstance(diff, float) else f" ({diff:+d})"

    lines = [
        f"## Review evaluation: `{report['model']}` (prompt {report['prompt_version']})",
        "",
        f"- Recall: **{t['recall']}**{delta('recall')} ({t['found']}/{t['expected']} expected issues found)",
        f"- Precision: **{t['precision']}**{delta('precision')} ({t['reported']} findings reported)",
        f"- False positives in quiet regions: **{t['false_positives']}**{delta('false_positives')}",
        f"- Tokens: {t['input_tokens']:,} in / {t['output_tokens']:,} out · LLM errors: {t['errors']}",
        "",
        "| Case | Found | Missed | False positives | Other findings |",
        "|---|---|---|---|---|",
    ]
    for c in report["cases"]:
        lines.append(
            f"| {c['name']} | {len(c['found'])} | {', '.join(c['missed']) or '-'} | "
            f"{c['false_positives']} | {c['unexpected']} |"
        )
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, default=str))
