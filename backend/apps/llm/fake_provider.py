"""Deterministic, offline provider for demos, local development, and end-to-end tests.

It scans the rendered diff in the prompt and flags a few obvious patterns on added lines.
Enabled only when ``REVIEWBOT_ENABLE_FAKE_PROVIDER`` is true.
"""

from __future__ import annotations

import re
from typing import Any

from apps.llm.base import AuthenticationFailed, LLMResponse, TokenUsage

FILE_HEADER = re.compile(r"^### File: (?P<path>\S+)")
ADDED_LINE = re.compile(r"^\s*(?P<line>\d+) \+ ?(?P<code>.*)$")

RULES: list[tuple[re.Pattern[str], dict[str, Any]]] = [
    (
        re.compile(r"(password|secret|api_key)\s*=\s*['\"][^'\"]+['\"]", re.I),
        {
            "category": "security",
            "severity": "high",
            "title": "Hard-coded credential",
            "body": "A credential appears to be committed in source. Load it from configuration instead.",
        },
    ),
    (
        re.compile(r"\beval\("),
        {
            "category": "security",
            "severity": "critical",
            "title": "Use of eval()",
            "body": "eval() executes arbitrary code; avoid it for untrusted input.",
        },
    ),
    (
        re.compile(r"\bTODO\b"),
        {
            "category": "maintainability",
            "severity": "low",
            "title": "Unresolved TODO",
            "body": "This TODO is being added in the change; consider tracking it in an issue.",
        },
    ),
    (
        re.compile(r"^\s*print\("),
        {
            "category": "style",
            "severity": "info",
            "title": "Debug print statement",
            "body": "Use the logging module instead of print().",
        },
    ),
]


class FakeProvider:
    name = "fake"
    strict_schema = True

    def __init__(self, *, api_key: str, model: str, base_url: str | None = None, http_client: Any = None):
        self.api_key = api_key
        self.model = model

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResponse:
        findings: list[dict[str, Any]] = []
        current_path: str | None = None
        for raw in user.splitlines():
            header = FILE_HEADER.match(raw)
            if header:
                current_path = header.group("path")
                continue
            added = ADDED_LINE.match(raw)
            if not (added and current_path):
                continue
            line_no = int(added.group("line"))
            for pattern, template in RULES:
                if pattern.search(added.group("code")):
                    findings.append(
                        {
                            **template,
                            "path": current_path,
                            "line_start": line_no,
                            "line_end": line_no,
                            "confidence": 0.9,
                            "suggestion": None,
                        }
                    )
                    break
        summary = (
            f"Automated demo review found {len(findings)} potential issue(s)."
            if findings
            else "Automated demo review found no issues."
        )
        usage = TokenUsage(input_tokens=(len(system) + len(user)) // 4, output_tokens=50 + 40 * len(findings))
        data = {"summary": summary, "findings": findings}
        return LLMResponse(data=data, raw_text=str(data), usage=usage, model=self.model)

    def validate(self) -> None:
        if self.api_key == "invalid":
            raise AuthenticationFailed("Fake provider rejects the key 'invalid'.")
