"""Repository-level review configuration: structured rules (RE-14), the `.reviewbot.yml` file (RE-15),
and base-branch filters (REPO-03). Pure functions, no database access.

`.reviewbot.yml` is read from the pull request's **base** commit, never from the head, so a pull request
cannot weaken its own review. Cost guards (size and token limits), the LLM key, and the check-run gate
can only be set in the dashboard.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

import pathspec
import yaml

from apps.reviews.engine.profiles import PROFILES

CONFIG_PATH = ".reviewbot.yml"
MAX_RULES = 50
MAX_CONFIG_BYTES = 64 * 1024
SEVERITIES = ("critical", "high", "medium", "low", "info")
RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
FILE_KEYS = {
    "profile",
    "min_severity",
    "min_confidence",
    "max_inline_comments",
    "ignore_patterns",
    "instructions",
    "rules",
}


class ConfigError(ValueError):
    pass


# --- rules ---------------------------------------------------------------------------------------


def normalize_rules(raw: Any) -> list[dict[str, Any]]:
    """Validates and normalizes a list of rules. Raises ConfigError with a human-readable message."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("rules must be a list")
    if len(raw) > MAX_RULES:
        raise ConfigError(f"at most {MAX_RULES} rules are allowed")
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"rule {index} must be a mapping")
        rule_id = str(item.get("id", "")).strip()
        if not RULE_ID.match(rule_id):
            raise ConfigError(f"rule {index}: id must be 1-64 letters, digits, '-', '_' or '.'")
        if rule_id in seen:
            raise ConfigError(f"rule {rule_id}: duplicate id")
        seen.add(rule_id)
        description = " ".join(str(item.get("description", "")).split())
        if not description or len(description) > 500:
            raise ConfigError(f"rule {rule_id}: description is required (max 500 characters)")
        severity = str(item.get("severity", "medium")).lower()
        if severity not in SEVERITIES:
            raise ConfigError(f"rule {rule_id}: severity must be one of {', '.join(SEVERITIES)}")
        paths = item.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths) or len(paths) > 20:
            raise ConfigError(f"rule {rule_id}: paths must be a list of at most 20 glob patterns")
        rules.append(
            {
                "id": rule_id,
                "description": description,
                "severity": severity,
                "paths": [p.strip() for p in paths if p.strip()],
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return rules


def rule_applies(rule: dict[str, Any], paths: list[str]) -> bool:
    if not rule.get("enabled", True):
        return False
    if not rule.get("paths"):
        return True
    spec = pathspec.GitIgnoreSpec.from_lines(rule["paths"])
    return any(spec.match_file(p) for p in paths)


def applicable_rules(rules: list[dict[str, Any]], paths: list[str]) -> list[dict[str, Any]]:
    return [r for r in rules if rule_applies(r, paths)]


def render_rules(rules: list[dict[str, Any]]) -> str:
    if not rules:
        return ""
    lines = [
        "<repository_rules>",
        "Check the change against these team rules. When a finding violates one, set rule_id to the "
        "rule's id and use at least the rule's severity.",
    ]
    for rule in rules:
        scope = f" (applies to: {', '.join(rule['paths'])})" if rule.get("paths") else ""
        lines.append(f"- [{rule['id']}] ({rule['severity']}) {rule['description']}{scope}")
    lines.append("</repository_rules>")
    return "\n".join(lines)


# --- base branch filter --------------------------------------------------------------------------


def branch_matches(branch: str, patterns: list[str]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(branch, p) for p in patterns)


# --- .reviewbot.yml ------------------------------------------------------------------------------


@dataclass
class FileConfig:
    values: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_config_file(text: str) -> FileConfig:
    """Parses `.reviewbot.yml`. Raises ConfigError on invalid content; unknown keys become warnings."""
    if len(text.encode()) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{CONFIG_PATH} is larger than {MAX_CONFIG_BYTES // 1024} KB")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise ConfigError(f"invalid YAML{where}") from exc
    if data is None:
        return FileConfig()
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_PATH} must be a mapping of settings")
    result = FileConfig()
    for key in sorted(set(data) - FILE_KEYS):
        result.warnings.append(f"unknown key '{key}' ignored")
    values: dict[str, Any] = {}
    if "profile" in data:
        if data["profile"] not in PROFILES:
            raise ConfigError(f"profile must be one of {', '.join(PROFILES)}")
        values["profile"] = data["profile"]
    if "min_severity" in data:
        if str(data["min_severity"]).lower() not in SEVERITIES:
            raise ConfigError(f"min_severity must be one of {', '.join(SEVERITIES)}")
        values["min_severity"] = str(data["min_severity"]).lower()
    if "min_confidence" in data:
        try:
            confidence = float(data["min_confidence"])
        except (TypeError, ValueError) as exc:
            raise ConfigError("min_confidence must be a number between 0 and 1") from exc
        if not 0 <= confidence <= 1:
            raise ConfigError("min_confidence must be a number between 0 and 1")
        values["min_confidence"] = confidence
    if "max_inline_comments" in data:
        cap = data["max_inline_comments"]
        if not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap <= 100:
            raise ConfigError("max_inline_comments must be an integer between 0 and 100")
        values["max_inline_comments"] = cap
    if "ignore_patterns" in data:
        patterns = data["ignore_patterns"] or []
        if (
            not isinstance(patterns, list)
            or not all(isinstance(p, str) for p in patterns)
            or len(patterns) > 200
        ):
            raise ConfigError("ignore_patterns must be a list of at most 200 strings")
        values["ignore_patterns"] = [p.strip() for p in patterns if p.strip()]
    if "instructions" in data:
        instructions = data["instructions"]
        if not isinstance(instructions, str) or len(instructions) > 4000:
            raise ConfigError("instructions must be text of at most 4000 characters")
        values["instructions"] = instructions.strip()
    if "rules" in data:
        values["rules"] = normalize_rules(data["rules"])
    result.values = values
    return result


def merge_config(snapshot: dict[str, Any], file_config: FileConfig) -> dict[str, Any]:
    """Applies `.reviewbot.yml` on top of dashboard settings.

    Scalars in the file win. Ignore patterns and instructions are added to the dashboard's.
    Rules are merged by id, with the file's version winning.
    """
    merged = dict(snapshot)
    values = file_config.values
    for key in ("profile", "min_severity", "min_confidence", "max_inline_comments"):
        if key in values:
            merged[key] = values[key]
    if values.get("ignore_patterns"):
        merged["ignore_patterns"] = [*snapshot.get("ignore_patterns", []), *values["ignore_patterns"]]
    if values.get("instructions"):
        existing = snapshot.get("custom_instructions", "").strip()
        merged["custom_instructions"] = "\n\n".join(p for p in (existing, values["instructions"]) if p)
    if "rules" in values:
        by_id = {r["id"]: r for r in snapshot.get("rules", [])}
        by_id.update({r["id"]: r for r in values["rules"]})
        merged["rules"] = list(by_id.values())
    return merged
