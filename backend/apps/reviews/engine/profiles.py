"""Review profiles (RE-11): a prompt focus, an optional category filter, and default thresholds.

The thresholds are presets: choosing a profile in the dashboard copies them into the repository's
editable settings. The prompt focus and category filter are applied by the pipeline at review time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_PROFILE = "balanced"


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    description: str
    prompt_focus: str
    allowed_categories: frozenset[str] | None = None  # None = every category
    defaults: dict[str, object] = field(default_factory=dict)


PROFILES: dict[str, Profile] = {
    "strict": Profile(
        id="strict",
        label="Strict",
        description="Thorough review: reports minor issues and style problems that hurt readability.",
        prompt_focus=(
            "Be thorough. Report every real problem, including minor maintainability and readability "
            "issues, but still never report pure formatting."
        ),
        defaults={"min_severity": "low", "min_confidence": 0.4, "max_inline_comments": 50},
    ),
    "balanced": Profile(
        id="balanced",
        label="Balanced",
        description="Default: bugs, security, performance and important maintainability issues.",
        prompt_focus="Prioritize issues a careful senior reviewer would block or request changes for.",
        defaults={"min_severity": "low", "min_confidence": 0.5, "max_inline_comments": 25},
    ),
    "lenient": Profile(
        id="lenient",
        label="Lenient",
        description="Only high-impact problems; no style comments.",
        prompt_focus=(
            "Only report problems that are likely to cause incorrect behavior, security issues, data loss, "
            "or significant performance regressions. Do not report style or minor maintainability issues."
        ),
        allowed_categories=frozenset({"bug", "security", "performance", "maintainability", "test"}),
        defaults={"min_severity": "high", "min_confidence": 0.7, "max_inline_comments": 10},
    ),
    "security": Profile(
        id="security",
        label="Security-focused",
        description="Security findings only, guided by the OWASP Top 10.",
        prompt_focus=(
            "Review for security only. Look for injection (SQL, command, template, LDAP), broken access "
            "control and missing authorization checks, authentication and session flaws, sensitive data "
            "exposure and secrets in code, insecure deserialization, SSRF, path traversal, XSS, insecure "
            "cryptography, unsafe defaults, and vulnerable dependency usage. Report nothing else."
        ),
        allowed_categories=frozenset({"security"}),
        defaults={"min_severity": "medium", "min_confidence": 0.5, "max_inline_comments": 25},
    ),
}

PROFILE_CHOICES = [(p.id, p.label) for p in PROFILES.values()]


def get_profile(profile_id: str | None) -> Profile:
    return PROFILES.get(profile_id or DEFAULT_PROFILE, PROFILES[DEFAULT_PROFILE])
