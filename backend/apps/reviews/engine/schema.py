"""The structured-output contract between the review engine and every LLM provider (RE-05).

The same JSON schema is sent to providers that support constrained decoding and used to validate
every response. It deliberately avoids numeric/string constraints, which some providers reject;
ranges are enforced here instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

CATEGORIES = ["bug", "security", "performance", "maintainability", "style", "test"]
SEVERITIES = ["critical", "high", "medium", "low", "info"]

FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "path",
        "line_start",
        "line_end",
        "category",
        "severity",
        "confidence",
        "title",
        "body",
        "suggestion",
        "rule_id",
    ],
    "properties": {
        "path": {"type": "string", "description": "File path exactly as shown after 'File:'."},
        "line_start": {"type": "integer", "description": "First new-file line number of the issue."},
        "line_end": {"type": "integer", "description": "Last new-file line number of the issue."},
        "category": {"type": "string", "enum": CATEGORIES},
        "severity": {"type": "string", "enum": SEVERITIES},
        "confidence": {"type": "number", "description": "0.0 to 1.0"},
        "title": {"type": "string", "description": "One-line summary, under 100 characters."},
        "body": {"type": "string", "description": "Why it is a problem and how to fix it (Markdown)."},
        "suggestion": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Replacement code for lines line_start..line_end, or null.",
        },
        "rule_id": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Id of the repository rule this finding violates, or null.",
        },
    },
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string", "description": "2-4 sentence assessment of this part of the change."},
        "findings": {"type": "array", "items": FINDING_SCHEMA},
    },
}

_validator = Draft202012Validator(REVIEW_SCHEMA)

MAX_TITLE = 300
MAX_BODY = 6000
MAX_SUGGESTION = 6000
MAX_FINDINGS_PER_CHUNK = 50


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class RawFinding:
    path: str
    line_start: int
    line_end: int
    category: str
    severity: str
    confidence: float
    title: str
    body: str
    suggestion: str
    rule_id: str = ""


@dataclass(frozen=True)
class ChunkReview:
    summary: str
    findings: list[RawFinding]


def parse_review(data: Any) -> ChunkReview:
    errors = sorted(_validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.path) or "<root>"
        raise SchemaError(f"{location}: {first.message}"[:500])
    findings = []
    for item in data["findings"][:MAX_FINDINGS_PER_CHUNK]:
        start, end = int(item["line_start"]), int(item["line_end"])
        if end < start:
            start, end = end, start
        findings.append(
            RawFinding(
                path=item["path"].strip().lstrip("/"),
                line_start=max(start, 1),
                line_end=max(end, 1),
                category=item["category"],
                severity=item["severity"],
                confidence=min(max(float(item["confidence"]), 0.0), 1.0),
                title=" ".join(item["title"].split())[:MAX_TITLE] or "Untitled finding",
                body=item["body"].strip()[:MAX_BODY],
                suggestion=(item["suggestion"] or "")[:MAX_SUGGESTION],
                rule_id=str(item.get("rule_id") or "")[:64],
            )
        )
    return ChunkReview(summary=data["summary"].strip()[:4000], findings=findings)
