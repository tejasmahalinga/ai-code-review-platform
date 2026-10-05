"""Unit tests for the pure review-engine modules (no database, no network)."""

from __future__ import annotations

import pytest

from apps.reviews.engine import render
from apps.reviews.engine.chunker import build_chunks, estimate_tokens
from apps.reviews.engine.diff import FileDiff, parse_patch, render_hunk
from apps.reviews.engine.fingerprint import fingerprint
from apps.reviews.engine.ignore import IgnoreMatcher
from apps.reviews.engine.prompts import build_system_prompt, build_user_prompt
from apps.reviews.engine.schema import REVIEW_SCHEMA, SchemaError, parse_review

MODIFY_PATCH = """@@ -10,7 +10,8 @@ def handler(request):
     user = request.user
-    data = request.GET["q"]
+    data = request.GET.get("q", "")
+    data = data.strip()
     if not data:
         return None
     return search(data)
\\ No newline at end of file"""


class TestDiffParsing:
    def test_modify_patch_line_numbers(self):
        hunks = parse_patch(MODIFY_PATCH)
        assert len(hunks) == 1
        hunk = hunks[0]
        assert (hunk.old_start, hunk.new_start, hunk.new_len) == (10, 10, 8)
        kinds = [(line.kind, line.old, line.new) for line in hunk.lines]
        assert kinds == [
            (" ", 10, 10),
            ("-", 11, None),
            ("+", None, 11),
            ("+", None, 12),
            (" ", 12, 13),
            (" ", 13, 14),
            (" ", 14, 15),
        ]

    def test_commentable_and_added_lines(self):
        diff = FileDiff(path="app.py", status="modified", hunks=parse_patch(MODIFY_PATCH))
        assert diff.added_lines == {11, 12}
        assert diff.commentable_lines == {10, 11, 12, 13, 14, 15}

    def test_multiple_hunks_and_single_line_headers(self):
        patch = "@@ -1 +1 @@\n-a\n+b\n@@ -20,2 +20,3 @@\n x\n+y\n z"
        hunks = parse_patch(patch)
        assert [h.new_start for h in hunks] == [1, 20]
        assert hunks[0].lines[1].new == 1
        assert [line.new for line in hunks[1].lines] == [20, 21, 22]

    def test_new_file(self):
        hunks = parse_patch("@@ -0,0 +1,3 @@\n+a\n+b\n+c")
        assert [line.new for line in hunks[0].lines] == [1, 2, 3]

    def test_render_shows_new_line_numbers(self):
        rendered = render_hunk(parse_patch(MODIFY_PATCH)[0])
        assert "    11 +     data = request.GET.get" in rendered
        assert '       -     data = request.GET["q"]' in rendered
        assert "No newline" not in rendered

    def test_context_around_ignores_line_numbers(self):
        diff = FileDiff(path="a.py", status="modified", hunks=parse_patch(MODIFY_PATCH))
        assert "data.strip()" in diff.context_around(12)


def big_diff(path: str, lines: int) -> FileDiff:
    body = "\n".join(
        f"+    value_{i} = compute_something(argument_{i}, other_argument_{i})" for i in range(lines)
    )
    return FileDiff(
        path=path, status="added", hunks=parse_patch(f"@@ -0,0 +1,{lines} @@\n{body}"), additions=lines
    )


class TestChunker:
    def test_large_pr_split_under_budget_with_every_line_once(self):
        diffs = [big_diff(f"src/module_{i}.py", 500) for i in range(10)]  # 5,000 added lines
        budget = 16_000
        chunks = build_chunks(diffs, budget)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert estimate_tokens(chunk.text) <= budget
        seen: dict[tuple[str, int], int] = {}
        for chunk in chunks:
            current = None
            for line in chunk.text.splitlines():
                if line.startswith("### File: "):
                    current = line.split()[2]
                elif line.strip() and line.split()[0].isdigit():
                    key = (current, int(line.split()[0]))
                    seen[key] = seen.get(key, 0) + 1
        assert len(seen) == 5_000
        assert set(seen.values()) == {1}

    def test_single_file_larger_than_budget_is_split_into_parts(self):
        chunks = build_chunks([big_diff("huge.py", 2_000)], 4_000)
        assert len(chunks) > 1
        assert all(estimate_tokens(c.text) <= 4_000 for c in chunks)
        assert "[part 1/" in chunks[0].text

    def test_small_files_share_a_chunk(self):
        diffs = [big_diff(f"f{i}.py", 3) for i in range(5)]
        chunks = build_chunks(diffs, 16_000)
        assert len(chunks) == 1
        assert chunks[0].paths == [f"f{i}.py" for i in range(5)]


class TestIgnore:
    def test_defaults(self):
        matcher = IgnoreMatcher([])
        for path in [
            "package-lock.json",
            "web/yarn.lock",
            "vendor/x.go",
            "dist/app.js",
            "a.min.js",
            "x_pb2.py",
            "logo.png",
        ]:
            assert matcher.check(path).ignored, path
        assert not matcher.check("src/app.py").ignored

    def test_custom_patterns_and_negation(self):
        matcher = IgnoreMatcher(["src/generated/", "!src/generated/keep.py"])
        decision = matcher.check("src/generated/a.py")
        assert decision.ignored and decision.pattern == "src/generated/"
        assert not matcher.check("src/generated/keep.py").ignored

    def test_replace_defaults(self):
        matcher = IgnoreMatcher(["*.md"], include_defaults=False)
        assert not matcher.check("package-lock.json").ignored
        assert matcher.check("README.md").ignored


class TestSchema:
    def valid(self, **overrides):
        item = {
            "path": "a.py",
            "line_start": 3,
            "line_end": 2,
            "category": "bug",
            "severity": "high",
            "confidence": 1.7,
            "title": "  Off   by one ",
            "body": "x",
            "suggestion": None,
        }
        item.update(overrides)
        return {"summary": "ok", "findings": [item]}

    def test_normalizes_values(self):
        review = parse_review(self.valid())
        finding = review.findings[0]
        assert (finding.line_start, finding.line_end) == (2, 3)
        assert finding.confidence == 1.0
        assert finding.title == "Off by one"
        assert finding.suggestion == ""

    @pytest.mark.parametrize(
        "payload",
        [
            {"summary": "x"},
            {"summary": "x", "findings": "nope"},
            {"summary": "x", "findings": [{"path": "a"}]},
        ],
    )
    def test_rejects_invalid(self, payload):
        with pytest.raises(SchemaError):
            parse_review(payload)

    def test_rejects_unknown_enum_and_extra_keys(self):
        with pytest.raises(SchemaError):
            parse_review(self.valid(severity="blocker"))
        bad = self.valid()
        bad["findings"][0]["extra"] = 1
        with pytest.raises(SchemaError):
            parse_review(bad)

    def test_schema_is_strict_mode_compatible(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                    assert set(node["required"]) == set(node["properties"])
                for banned in ("minimum", "maximum", "minLength", "maxLength"):
                    assert banned not in node
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(REVIEW_SCHEMA)


class TestFingerprint:
    def test_stable_when_lines_shift(self):
        before = FileDiff("a.py", "modified", parse_patch("@@ -1,2 +1,3 @@\n x\n+danger()\n y"))
        after = FileDiff("a.py", "modified", parse_patch("@@ -1,2 +40,3 @@\n x\n+danger()\n y"))
        fp1 = fingerprint("a.py", "bug", "Dangerous call!", before.context_around(2))
        fp2 = fingerprint("a.py", "bug", "dangerous   call", after.context_around(41))
        assert fp1 == fp2

    def test_differs_by_path_category_and_context(self):
        base = fingerprint("a.py", "bug", "t", "x = 1")
        assert base != fingerprint("b.py", "bug", "t", "x = 1")
        assert base != fingerprint("a.py", "security", "t", "x = 1")
        assert base != fingerprint("a.py", "bug", "t", "x = 2")


class TestRender:
    class F:
        path = "a.py"
        line_start = 3
        line_end = 4
        category = "security"
        severity = "high"
        confidence = 0.8
        title = "Ping @admins and @org/team"
        body = "Contact me@example.com or @octocat"
        suggestion = "x = 1\n```\nevil\n```"

    def test_mentions_neutralized(self):
        body = render.inline_comment_body(self.F(), with_suggestion=False)
        assert "@admins" not in body and "@octocat" not in body and "@org/team" not in body
        assert "me@example.com" in body  # e-mail addresses are untouched

    def test_suggestion_fence_cannot_be_closed_early(self):
        body = render.inline_comment_body(self.F(), with_suggestion=True)
        assert body.count("```suggestion") == 1
        inner = body.split("```suggestion\n", 1)[1]
        assert inner.index("\n```") > inner.index("evil")

    def test_summary_lists_unanchored_and_counts(self):
        f = self.F()
        ctx = render.SummaryContext(
            summaries=["Looks mostly fine."],
            reported=[f, f],
            in_summary=[f],
            hidden_count=0,
            skipped_files=[("package-lock.json", "package-lock.json")],
            failed_chunks=0,
            total_chunks=1,
            model="m",
            head_sha="abcdef1234",
            dashboard_url="https://x/reviews/1",
        )
        body = render.summary_body(ctx)
        assert "2 high" in body
        assert "`a.py:3`" in body
        assert "package-lock.json" in body
        assert "abcdef1" in body


class TestPrompts:
    def test_custom_instructions_are_delimited(self):
        prompt = build_system_prompt("Flag raw SQL.")
        assert "<repository_instructions>\nFlag raw SQL.\n</repository_instructions>" in prompt
        assert "untrusted" in prompt

    def test_user_prompt_wraps_diff(self):
        prompt = build_user_prompt(
            pr_title="Fix\nbug", chunk_text="### File: a.py", chunk_index=1, chunk_count=3
        )
        assert "<pull_request_title>Fix bug</pull_request_title>" in prompt
        assert "part 2 of 3" in prompt
        assert prompt.endswith("</diff>")
