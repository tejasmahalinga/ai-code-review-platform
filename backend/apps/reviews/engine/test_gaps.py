"""Detects source changes that come without test changes, to request test suggestions (RE-13)."""

from __future__ import annotations

import re
from collections.abc import Iterable

TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|testing)/"
    r"|(^|/)test_[^/]+\.py$|_test\.(py|go|rb|exs?)$|\.(test|spec)\.[cm]?[jt]sx?$|Tests?\.(java|kt|cs|swift)$"
    r"|_spec\.rb$",
    re.I,
)
SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rb", ".java", ".kt", ".cs", ".rs",
    ".php", ".swift", ".scala", ".c", ".cc", ".cpp", ".h", ".hpp", ".ex", ".exs",
}  # fmt: skip
MAX_TEST_SEVERITY = "medium"

TEST_PROMPT = (
    "Test coverage: this change modifies source files but no test files. If it adds or changes behavior that "
    'should be tested, report one finding with category "test" (severity medium or lower) on the most '
    "relevant changed line, naming the untested behavior and sketching a concrete test case."
)


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def is_source_path(path: str) -> bool:
    dot = path.rfind(".")
    return dot != -1 and path[dot:].lower() in SOURCE_EXTENSIONS and not is_test_path(path)


def needs_test_suggestions(paths: Iterable[str]) -> bool:
    """True when the change touches source code but no tests."""
    path_list = list(paths)
    return any(is_source_path(p) for p in path_list) and not any(is_test_path(p) for p in path_list)
