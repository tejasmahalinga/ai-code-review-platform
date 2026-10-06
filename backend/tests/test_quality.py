"""Review quality: code context, consolidation of repeated findings, and the evaluation harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.git_providers.base import ChangedFile
from apps.git_providers.fake import FakeGitProvider
from apps.reviews import evaluation, pipeline
from apps.reviews.engine.chunker import build_chunks, estimate_tokens
from apps.reviews.engine.consolidate import Item, consolidate
from apps.reviews.engine.context import extract_context
from apps.reviews.engine.diff import FileDiff, parse_patch
from apps.reviews.models import Finding, ReviewRun
from apps.reviews.services import create_run
from tests.factories import ScriptedLLM, added_file, finding, make_pull_request, make_repository, pr_info

PY_SOURCE = "\n".join(
    [
        "import os",  # 1
        "from pathlib import Path",  # 2
        "",  # 3
        "",  # 4
        "class Store:",  # 5
        "    def __init__(self, root):",  # 6
        "        self.root = Path(root)",  # 7
        "",  # 8
        "    def read(self, name):",  # 9
        "        path = self.root / name",  # 10
        "        if not path.exists():",  # 11
        "            return None",  # 12
        "        return path.read_text()",  # 13
    ]
)
PY_PATCH = (
    "@@ -11,3 +11,3 @@\n         if not path.exists():\n-            raise KeyError(name)\n"
    "+            return None\n         return path.read_text()"
)


# --- context -------------------------------------------------------------------------------------


def test_context_has_imports_and_enclosing_definition_but_not_diff_lines():
    hunks = parse_patch(PY_PATCH)
    context = extract_context(PY_SOURCE, hunks)
    lines = context.splitlines()
    assert "    1 | import os" in lines and "    2 | from pathlib import Path" in lines
    assert "    9 |     def read(self, name):" in lines
    assert "   10 |         path = self.root / name" in lines
    assert not any(line.startswith("   11 |") or line.startswith("   12 |") for line in lines)  # in the diff
    assert "    5 | class Store:" not in lines  # only the nearest enclosing definition


def test_context_for_other_languages_and_far_definitions():
    js = ["import { db } from './db';", "", "export async function save(user) {"]
    js += [f"  step{i}();" for i in range(40)] + ["  await db.insert(user);", "}"]
    patch = "@@ -44,1 +44,1 @@\n-  db.insert(user);\n+  await db.insert(user);"
    context = extract_context("\n".join(js), parse_patch(patch))
    assert "    3 | export async function save(user) {" in context
    assert "    4 |   step0();" in context  # signature region
    assert "   43 |   step39();" in context  # lead-in right above the hunk
    assert "   20 |" not in context  # the middle of a long function is skipped
    java = (
        "class A {\n    public int size(List<String> xs) {\n        int n = 0;\n        return n;\n    }\n}"
    )
    assert "public int size" in extract_context(
        java, parse_patch("@@ -4,1 +4,1 @@\n-        return 0;\n+        return n;")
    )


def test_context_is_capped():
    source = "\n".join(["import a"] * 25 + ["def f():"] + ["    x = 1"] * 200)
    context = extract_context(
        source, parse_patch("@@ -220,1 +220,1 @@\n-    x = 2\n+    x = 1"), max_lines=10
    )
    assert context.splitlines()[-1].strip() == "⋮ (truncated)" and len(context.splitlines()) <= 12


def test_chunks_include_context_within_budget():
    diff = FileDiff("store.py", "modified", parse_patch(PY_PATCH))
    diff.context = extract_context(PY_SOURCE, diff.hunks)
    chunks = build_chunks([diff], 2_000)
    assert "Surrounding code" in chunks[0].text and "+             return None" in chunks[0].text
    tiny = build_chunks([diff], 60)
    assert all(c.tokens <= 60 or len(c.segments) == 1 for c in tiny)
    assert "+             return None" in "".join(c.text for c in tiny)  # diff lines are never displaced


# --- consolidation -------------------------------------------------------------------------------


def item(
    key, path="a.py", line=10, title="SQL injection in `query`", category="security", rank=(3, 0.9), rule=""
):
    return Item(key, path, line, line, category, rule, title, rank)


def test_near_duplicates_are_merged_keeping_the_best():
    result = consolidate(
        [item(0, rank=(2, 0.9)), item(1, line=12, title="SQL injection via `name`", rank=(3, 0.8))]
    )
    assert result.duplicates == {0: 1} and result.repeats == {}
    far = consolidate([item(0), item(1, line=40)])
    assert far.duplicates == {}


def test_repeated_patterns_are_capped():
    items = [
        item(i, path=f"cfg{i}.py", title=f"Hard-coded credential in `KEY_{i}`", rank=(3, 0.9 - i / 100))
        for i in range(5)
    ]
    result = consolidate(items)
    assert result.repeats == {3: 0, 4: 0}
    by_rule = consolidate([item(i, path=f"f{i}.py", title=f"T{i}", rule="no-eval") for i in range(4)])
    assert set(by_rule.repeats) == {3}


@pytest.fixture
def repo(fake_credential):
    return make_repository(credential=fake_credential, name="app")


def review(repo, files, script, contents=None, number=1, **settings):
    for key, value in settings.items():
        setattr(repo.settings, key, value)
    repo.settings.save()
    git = FakeGitProvider()
    git.add_pull_request("acme/app", pr_info(number), files)
    git.contents.update(contents or {})
    pr = make_pull_request(repo, number)
    run = create_run(pr, trigger=ReviewRun.Trigger.MANUAL, head_sha="a" * 40)
    llm = ScriptedLLM(script)
    pipeline.execute(run.pk, git=git, llm=llm)
    run.refresh_from_db()
    return run, git, llm


@pytest.mark.django_db
def test_pipeline_consolidates_repeated_findings(repo):
    files = [added_file(f"cfg/{i}.py", ["KEY = 'x'"]) for i in range(5)]
    script = [
        {
            "summary": "",
            "findings": [
                finding(f"cfg/{i}.py", 1, title=f"Hard-coded secret `KEY{i}`", category="security")
                for i in range(5)
            ],
        }
    ]
    run, git, _ = review(repo, files, script)
    statuses = sorted(Finding.objects.filter(review_run=run).values_list("post_status", flat=True))
    assert statuses == ["consolidated", "consolidated", "posted", "posted", "posted"]
    assert len(git.posted[0].comments) == 3
    assert "### Repeated findings" in git.posted[0].body and "2 more occurrences" in git.posted[0].body


@pytest.mark.django_db
def test_pipeline_sends_context_for_modified_files(repo):
    changed = ChangedFile("store.py", "modified", 1, 1, PY_PATCH)
    script = lambda s, u: {"summary": "", "findings": []}  # noqa: E731
    _, _, llm = review(repo, [changed], script, contents={("acme/app", "store.py", "a" * 40): PY_SOURCE})
    assert "Surrounding code" in llm.calls[0]["user"] and "def read(self, name):" in llm.calls[0]["user"]
    _, _, llm = review(
        repo,
        [changed],
        script,
        contents={("acme/app", "store.py", "a" * 40): PY_SOURCE},
        number=2,
        extended_context=False,
    )
    assert "Surrounding code" not in llm.calls[0]["user"]


@pytest.mark.django_db
def test_context_is_dropped_before_skipping_for_size(repo):
    changed = ChangedFile("store.py", "modified", 1, 1, PY_PATCH)
    contents = {("acme/app", "store.py", "a" * 40): PY_SOURCE}
    _, _, llm = review(
        repo, [changed], [{"summary": "", "findings": []}], contents=contents, extended_context=False
    )
    plain = build_chunks([FileDiff("store.py", "modified", parse_patch(PY_PATCH))], 100_000)[0].tokens
    limit = estimate_tokens(llm.calls[0]["system"]) + 200 + plain + 5  # fits only without context
    run, _, llm = review(
        repo,
        [changed],
        [{"summary": "", "findings": []}],
        contents=contents,
        number=2,
        extended_context=True,
        max_input_tokens=limit,
    )
    assert run.status == "completed" and "Surrounding code" not in llm.calls[0]["user"]


# --- evaluation harness --------------------------------------------------------------------------

CASES = Path(__file__).resolve().parents[1] / "evals" / "cases"


def test_seed_cases_load():
    cases = evaluation.load_cases(CASES)
    assert len(cases) >= 7
    assert all(d.hunks for c in cases for d in c.diffs)


def test_run_case_scores_found_missed_and_false_positives():
    case = evaluation.load_cases(CASES, ["sql-injection"])[0]
    hit = finding("app/users.py", 6, severity="critical", category="security", title="SQL injection")
    noise = finding("app/users.py", 2, category="style", title="Blank line")
    result = evaluation.run_case(case, ScriptedLLM([{"summary": "", "findings": [hit, noise]}]))
    assert result.found == ["app/users.py:6-6"] and result.missed == []
    assert result.false_positives == 1 and result.unexpected == 0
    miss = evaluation.run_case(case, ScriptedLLM([{"summary": "", "findings": []}]))
    assert miss.missed == ["app/users.py:6-6"]
    report = evaluation.summarize([result, miss], model="m", settings={})
    assert report["totals"]["recall"] == 0.5 and report["totals"]["precision"] == 0.5
    md = evaluation.markdown(report, baseline=report)
    assert "Recall: **0.5** (+0.000)" in md


def test_bad_cases_are_rejected(tmp_path):
    (tmp_path / "x.yaml").write_text("files:\n  - path: a.py\n    patch: 'not a diff'\nexpected: []\n")
    with pytest.raises(evaluation.CaseError):
        evaluation.load_cases(tmp_path)


@pytest.mark.django_db
def test_evaluate_command_with_demo_provider(tmp_path, settings):
    settings.REVIEWBOT_ENABLE_FAKE_PROVIDER = True
    out = tmp_path / "report.json"
    call_command(
        "evaluate_reviews", "--provider", "fake", "--out", str(out), "--markdown", str(tmp_path / "r.md")
    )
    report = json.loads(out.read_text())
    assert report["totals"]["cases"] >= 7 and report["prompt_version"]
    assert "hardcoded-secret" in {c["name"] for c in report["cases"] if c["found"]}
    with pytest.raises(CommandError, match="Recall"):
        call_command("evaluate_reviews", "--provider", "fake", "--min-recall", "0.99")
    with pytest.raises(CommandError):
        call_command("evaluate_reviews", "--provider", "openai")  # no API key in the environment
