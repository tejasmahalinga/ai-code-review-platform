"""Markdown rendering for PR comments (RE-06). Model output is neutralized before posting."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from apps.reviews.engine.risk import risk_bucket

ZWJ = "‍"
_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")
_FENCE = re.compile(r"^(\s*)```", re.M)
SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}
SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
MAX_SUMMARY_LIST = 20
MAX_COMMENT_CHARS = 60_000  # GitHub's limit is 65,536


class RenderableFinding(Protocol):
    path: str
    line_start: int | None
    line_end: int | None
    category: str
    severity: str
    confidence: float
    title: str
    body: str
    suggestion: str


def neutralize(text: str) -> str:
    """Prevents model output from pinging users/teams (``@name`` → ``@‍name``)."""
    return _MENTION.sub("@" + ZWJ, text)


def _safe_suggestion(code: str) -> str:
    # A ``` inside the suggestion would close the fence early.
    return _FENCE.sub(lambda m: m.group(1) + "`" + ZWJ + "``", code.rstrip("\n"))


def inline_comment_body(finding: RenderableFinding, *, with_suggestion: bool) -> str:
    icon = SEVERITY_ICON.get(finding.severity, "")
    parts = [
        f"{icon} **{finding.severity.capitalize()} · {finding.category}** — {neutralize(finding.title)}",
        "",
        neutralize(finding.body),
    ]
    if with_suggestion and finding.suggestion.strip():
        parts += ["", "```suggestion", _safe_suggestion(finding.suggestion), "```"]
    rule_id = getattr(finding, "rule_id", "")
    rule = f" · rule `{rule_id}`" if rule_id else ""
    parts += ["", f"<sub>Reviewbot · confidence {finding.confidence:.2f}{rule}</sub>"]
    return "\n".join(parts)[:MAX_COMMENT_CHARS]


def severity_counts(findings: Iterable[RenderableFinding]) -> dict[str, int]:
    counts = Counter(f.severity for f in findings)
    return {s: counts.get(s, 0) for s in SEVERITY_ORDER}


def counts_line(counts: dict[str, int]) -> str:
    parts = [f"{SEVERITY_ICON[s]} {n} {s}" for s, n in counts.items() if n]
    return " · ".join(parts) if parts else "No issues found."


@dataclass
class SummaryContext:
    summaries: list[str]
    reported: Sequence[RenderableFinding]  # everything posted inline or listed in the summary
    in_summary: Sequence[RenderableFinding]  # findings listed in the body (not inline)
    hidden_count: int  # posted nowhere because of the inline cap
    skipped_files: list[tuple[str, str]]  # (path, reason)
    failed_chunks: int
    total_chunks: int
    model: str
    head_sha: str
    dashboard_url: str
    scope_note: str = ""  # e.g. "Incremental review of changes since abc1234."
    risk_score: int | None = None


def summary_body(ctx: SummaryContext) -> str:
    lines = ["## Reviewbot review", ""]
    if ctx.scope_note:
        lines += [f"_{ctx.scope_note}_", ""]
    summaries = [neutralize(s.strip()) for s in ctx.summaries if s.strip()]
    if len(summaries) == 1:
        lines += [summaries[0], ""]
    elif summaries:
        lines += [f"- {s}" for s in summaries] + [""]
    lines += [f"**Findings:** {counts_line(severity_counts(ctx.reported))}", ""]
    if ctx.risk_score is not None:
        lines += [f"**Risk:** {ctx.risk_score}/100 ({risk_bucket(ctx.risk_score)})", ""]

    if ctx.in_summary:
        lines += ["### Findings outside the changed lines", ""]
        for f in list(ctx.in_summary)[:MAX_SUMMARY_LIST]:
            location = f"`{f.path}:{f.line_start}`" if f.line_start else f"`{f.path}`"
            body = neutralize(" ".join(f.body.split()))[:400]
            icon = SEVERITY_ICON.get(f.severity, "")
            lines.append(f"- {icon} **{f.severity}** {location} — {neutralize(f.title)}. {body}")
        if len(ctx.in_summary) > MAX_SUMMARY_LIST:
            lines.append(f"- … and {len(ctx.in_summary) - MAX_SUMMARY_LIST} more in the dashboard.")
        lines.append("")

    if ctx.hidden_count:
        lines += [
            f"_{ctx.hidden_count} more finding(s) are listed in the [dashboard]({ctx.dashboard_url})._",
            "",
        ]
    if ctx.failed_chunks:
        lines += [
            f"> ⚠️ {ctx.failed_chunks} of {ctx.total_chunks} parts of this diff could not be reviewed "
            "(LLM error). Re-run the review from the dashboard.",
            "",
        ]
    if ctx.skipped_files:
        lines += [f"<details><summary>{len(ctx.skipped_files)} file(s) not reviewed</summary>", ""]
        lines += [f"- `{path}` — {reason}" for path, reason in ctx.skipped_files[:100]]
        lines += ["", "</details>", ""]
    lines.append(
        f"<sub>Model `{ctx.model}` · commit {ctx.head_sha[:7]} · [details]({ctx.dashboard_url})</sub>"
    )
    return "\n".join(lines)[:MAX_COMMENT_CHARS]


def skipped_body(reason: str, dashboard_url: str) -> str:
    return f"## Reviewbot review\n\n{reason}\n\n<sub>[details]({dashboard_url})</sub>"
