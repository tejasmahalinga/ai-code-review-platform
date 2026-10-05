"""Stable finding fingerprints for de-duplication across runs (RE-09).

Line numbers are deliberately excluded so a finding is still recognized after code above it moves.
"""

from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"[a-z0-9]+")


def normalize_title(title: str) -> str:
    return " ".join(_WORD.findall(title.lower()))


def normalize_code(code: str) -> str:
    return "\n".join(" ".join(line.split()) for line in code.splitlines() if line.strip())


def fingerprint(path: str, category: str, title: str, context: str) -> str:
    material = "\x1f".join([path, category, normalize_title(title), normalize_code(context)])
    return hashlib.sha256(material.encode()).hexdigest()
