"""Gitignore-style file filtering applied before anything is sent to an LLM (RE-07)."""

from __future__ import annotations

from dataclasses import dataclass

import pathspec

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    # Dependency lockfiles
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
    "*.lock",
    # Vendored and build output
    "vendor/",
    "node_modules/",
    "dist/",
    "build/",
    ".next/",
    "*.min.js",
    "*.min.css",
    "*.map",
    # Generated code
    "*.pb.go",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.generated.*",
    "__snapshots__/",
    "*.snap",
    # Binary / media
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.ico",
    "*.pdf",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.zip",
    "*.gz",
    "*.jar",
)


@dataclass(frozen=True)
class IgnoreDecision:
    ignored: bool
    pattern: str | None = None


class IgnoreMatcher:
    def __init__(self, patterns: list[str] | tuple[str, ...], *, include_defaults: bool = True):
        lines = [*(DEFAULT_IGNORE_PATTERNS if include_defaults else ()), *patterns]
        self._lines = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        self._spec = pathspec.GitIgnoreSpec.from_lines(self._lines)

    def check(self, path: str) -> IgnoreDecision:
        result = self._spec.check_file(path)
        if result.include:
            pattern = self._lines[result.index] if result.index is not None else None
            return IgnoreDecision(True, pattern)
        return IgnoreDecision(False)
