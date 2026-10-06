"""Deterministic pull request risk score, 0-100 (RE-12).

The score is computed from the reported findings, the size of the change, and changes to sensitive paths.
It is a pure function of its inputs (no LLM), so the same review always yields the same score.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

SEVERITY_POINTS = {"critical": 30, "high": 15, "medium": 6, "low": 2, "info": 0}
FINDINGS_CAP = 60

# (label, pattern, points) - each category counts once.
SENSITIVE_PATHS: list[tuple[str, re.Pattern[str], int]] = [
    ("CI workflows", re.compile(r"(^|/)\.github/workflows/|(^|/)\.gitlab-ci\.yml$|(^|/)Jenkinsfile$"), 15),
    (
        "authentication / authorization",
        re.compile(r"(^|/)(auth|authn|authz|security|permissions?|login|session)s?(/|[._-])", re.I),
        15,
    ),
    ("database migrations", re.compile(r"(^|/)migrations?/|\.sql$", re.I), 10),
    (
        "container / deployment",
        re.compile(
            r"(^|/)(Dockerfile|docker-compose[^/]*\.ya?ml|Chart\.yaml)$|(^|/)(deploy|k8s|helm|terraform)/|\.tf$"
        ),
        10,
    ),
    (
        "dependency manifests",
        re.compile(
            r"(^|/)(package\.json|requirements[^/]*\.txt|pyproject\.toml|go\.mod|Cargo\.toml|Gemfile|pom\.xml|build\.gradle(\.kts)?)$"
        ),
        5,
    ),
    (
        "secrets / configuration",
        re.compile(r"(^|/)(\.env[^/]*|settings[^/]*\.py|config/[^/]+\.(ya?ml|json|toml))$", re.I),
        5,
    ),
]


@dataclass(frozen=True)
class RiskBreakdown:
    score: int
    findings_points: int
    size_points: int
    sensitive: tuple[str, ...]

    @property
    def bucket(self) -> str:
        return risk_bucket(self.score)


def risk_bucket(score: int | None) -> str:
    if score is None:
        return "unknown"
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def size_points(changed_lines: int) -> int:
    if changed_lines > 1000:
        return 15
    if changed_lines > 500:
        return 10
    if changed_lines > 200:
        return 6
    if changed_lines > 50:
        return 3
    return 0


def compute_risk(severities: Iterable[str], paths: Iterable[str], changed_lines: int) -> RiskBreakdown:
    findings_points = min(sum(SEVERITY_POINTS.get(s, 0) for s in severities), FINDINGS_CAP)
    path_list = list(paths)
    sensitive: list[str] = []
    sensitive_points = 0
    for label, pattern, points in SENSITIVE_PATHS:
        if any(pattern.search(p) for p in path_list):
            sensitive.append(label)
            sensitive_points += points
    size = size_points(changed_lines)
    score = min(findings_points + size + sensitive_points, 100)
    return RiskBreakdown(
        score=score, findings_points=findings_points, size_points=size, sensitive=tuple(sensitive)
    )
