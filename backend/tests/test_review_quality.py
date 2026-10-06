"""RE-10 incremental push reviews, INT-02 check runs, RE-11 review profiles."""

from __future__ import annotations

import pytest

from apps.git_providers.base import CompareResult
from apps.git_providers.fake import FakeGitProvider
from apps.llm.base import ProviderUnavailable
from apps.reviews import pipeline
from apps.reviews.engine.profiles import get_profile
from apps.reviews.engine.prompts import build_system_prompt
from apps.reviews.models import ReviewRun
from apps.reviews.services import create_run
from tests.factories import ScriptedLLM, added_file, finding, make_pull_request, make_repository, pr_info

pytestmark = pytest.mark.django_db

REPO = "acme/app"
OLD, NEW = "a" * 40, "c" * 40
APP_LINES = [f"value_{i} = compute({i})" for i in range(1, 21)]
README_LINES = ["# App", "TODO: document setup", "More text"]


@pytest.fixture
def repo(fake_credential):
    return make_repository(credential=fake_credential, name="app")


@pytest.fixture
def git():
    provider = FakeGitProvider()
    provider.add_pull_request(
        REPO,
        pr_info(1, head_sha=NEW),
        [added_file("src/app.py", APP_LINES), added_file("README.md", README_LINES)],
    )
    return provider


def configure(repo, **settings):
    for key, value in settings.items():
        setattr(repo.settings, key, value)
    repo.settings.save()


def push_run(repo, head_sha=NEW, last_reviewed=OLD, **settings):
    configure(repo, **settings)
    pr = make_pull_request(repo, 1, head_sha=head_sha, last_reviewed_sha=last_reviewed)
    return create_run(pr, trigger=ReviewRun.Trigger.PUSH, head_sha=head_sha)


def execute(run_obj, git, llm):
    pipeline.execute(run_obj.pk, git=git, llm=llm)
    run_obj.refresh_from_db()
    return run_obj


def empty_review(system, user):
    return {"summary": "ok", "findings": []}


# --- RE-10 ---------------------------------------------------------------------------------------


def test_push_reviews_only_changes_since_last_reviewed_commit(repo, git):
    git.add_comparison(REPO, OLD, NEW, CompareResult("ahead", [added_file("README.md", README_LINES)]))
    llm = ScriptedLLM([{"summary": "Docs only.", "findings": [finding("README.md", 2, title="Vague TODO")]}])
    result = execute(push_run(repo), git, llm)
    assert result.status == "completed", result.error
    assert (result.incremental, result.compare_base_sha) == (True, OLD)
    prompt = llm.calls[0]["user"]
    assert "README.md" in prompt and "src/app.py" not in prompt
    # The finding is anchored against the full PR diff, so it is posted inline.
    assert result.findings.get().post_status == "posted"
    assert "Incremental review of changes since aaaaaaa" in git.posted[0].body
    assert result.pull_request.last_reviewed_sha == NEW


def test_files_outside_the_pr_are_not_reviewed(repo, git):
    # e.g. a merge from the base branch brings in unrelated files.
    files = [added_file("README.md", README_LINES), added_file("vendor/other.py", ["x = 1"])]
    git.add_comparison(REPO, OLD, NEW, CompareResult("ahead", files))
    llm = ScriptedLLM([{"summary": "", "findings": []}])
    execute(push_run(repo), git, llm)
    assert "vendor/other.py" not in llm.calls[0]["user"]


def test_force_push_falls_back_to_full_review(repo, git):
    git.add_comparison(REPO, OLD, NEW, CompareResult("diverged", []))
    llm = ScriptedLLM([{"summary": "", "findings": []}])
    result = execute(push_run(repo), git, llm)
    assert result.incremental is False
    assert "src/app.py" in llm.calls[0]["user"] and "README.md" in llm.calls[0]["user"]


def test_missing_comparison_falls_back_to_full_review(repo, git):
    llm = ScriptedLLM([{"summary": "", "findings": []}])
    result = execute(push_run(repo), git, llm)
    assert git.compare_calls == [(REPO, OLD, NEW)]
    assert result.incremental is False and result.status == "completed"


def test_push_without_previous_review_is_a_full_review(repo, git):
    llm = ScriptedLLM([{"summary": "", "findings": []}])
    result = execute(push_run(repo, last_reviewed=""), git, llm)
    assert git.compare_calls == []
    assert result.incremental is False


def test_no_reviewable_changes_since_last_review(repo, git):
    git.add_comparison(REPO, OLD, NEW, CompareResult("ahead", [added_file("package-lock.json", ["{}"])]))
    llm = ScriptedLLM([])
    result = execute(push_run(repo), git, llm)
    assert (result.status, result.status_reason) == ("skipped", "no_reviewable_files")
    assert result.summary == "No reviewable changes since aaaaaaa."
    assert llm.calls == []


def test_outdated_push_run_is_superseded(repo, git):
    # The run was queued for commit b, but the PR head has since moved to c.
    llm = ScriptedLLM([])
    result = execute(push_run(repo, head_sha="b" * 40), git, llm)
    assert (result.status, result.status_reason) == ("cancelled", "superseded")
    assert llm.calls == [] and git.posted == [] and git.check_runs == []


def test_cancelled_run_is_never_executed(repo, git):
    run_obj = push_run(repo)
    ReviewRun.objects.filter(pk=run_obj.pk).update(status="cancelled")
    llm = ScriptedLLM([])
    execute(run_obj, git, llm)
    assert llm.calls == []


# --- INT-02 --------------------------------------------------------------------------------------


def manual_run(repo, **settings):
    configure(repo, **settings)
    pr = make_pull_request(repo, 1, head_sha=NEW)
    return create_run(pr, trigger=ReviewRun.Trigger.MANUAL, head_sha=NEW)


def test_check_run_success_when_clean(repo, git):
    result = execute(manual_run(repo), git, ScriptedLLM(empty_review))
    check = git.check_runs[0]
    assert (check.head_sha, check.name, check.status, check.conclusion) == (
        NEW,
        "Reviewbot",
        "completed",
        "success",
    )
    assert check.details_url.endswith(f"/reviews/{result.pk}")
    assert result.check_run_id == check.id


def test_check_run_neutral_with_findings_and_no_gate(repo, git):
    llm = ScriptedLLM([{"summary": "", "findings": [finding("src/app.py", 3, severity="critical")]}])
    execute(manual_run(repo), git, llm)
    assert (git.check_runs[0].conclusion, git.check_runs[0].title) == ("neutral", "1 finding(s)")


def test_check_run_fails_at_gate_severity(repo, git):
    findings = [
        finding("src/app.py", 3, severity="critical", title="A"),
        finding("src/app.py", 5, severity="low", title="B"),
    ]
    execute(manual_run(repo, gate_severity="high"), git, ScriptedLLM([{"summary": "", "findings": findings}]))
    check = git.check_runs[0]
    assert (check.conclusion, check.title) == ("failure", "1 finding(s) at or above high")
    assert "1 critical" in check.summary


def test_findings_below_gate_do_not_fail(repo, git):
    llm = ScriptedLLM([{"summary": "", "findings": [finding("src/app.py", 3, severity="medium")]}])
    execute(manual_run(repo, gate_severity="high"), git, llm)
    assert git.check_runs[0].conclusion == "neutral"


def test_failed_review_never_blocks_merges(repo, git):
    llm = ScriptedLLM([ProviderUnavailable("down"), ProviderUnavailable("down")])
    result = execute(manual_run(repo, gate_severity="info"), git, llm)
    assert result.status == "failed"
    assert (git.check_runs[0].conclusion, git.check_runs[0].title) == ("neutral", "Review failed")


def test_skipped_review_marks_check_skipped(repo, git):
    git.add_pull_request(REPO, pr_info(1, head_sha=NEW), [added_file("yarn.lock", ["x"])])
    execute(manual_run(repo), git, ScriptedLLM([]))
    assert git.check_runs[0].conclusion == "skipped"


def test_missing_checks_permission_is_not_fatal(repo, git):
    git.checks_forbidden = True
    result = execute(manual_run(repo), git, ScriptedLLM(empty_review))
    assert result.status == "completed" and result.check_run_id == ""


def test_check_runs_can_be_disabled(repo, git):
    execute(manual_run(repo, check_runs=False), git, ScriptedLLM(empty_review))
    assert git.check_runs == []


# --- RE-11 ---------------------------------------------------------------------------------------


def test_security_profile_filters_categories_and_focuses_prompt(repo, git):
    findings = [
        finding("src/app.py", 3, category="security", severity="high", title="Injection"),
        finding("src/app.py", 5, category="bug", severity="high", title="Off by one"),
    ]
    llm = ScriptedLLM([{"summary": "", "findings": findings}])
    result = execute(manual_run(repo, profile="security", min_severity="medium"), git, llm)
    statuses = dict(result.findings.values_list("title", "post_status"))
    assert statuses == {"Injection": "posted", "Off by one": "category_filtered"}
    assert "Review focus (Security-focused)" in llm.calls[0]["system"]
    assert "Only report findings in these categories: security." in llm.calls[0]["system"]
    assert [c.line for c in git.posted[0].comments] == [3]


def test_profile_prompts_differ():
    balanced = build_system_prompt("", profile=get_profile("balanced"))
    lenient = build_system_prompt("", profile=get_profile("lenient"))
    assert balanced != lenient
    assert "Only report findings in these categories" not in balanced
    assert get_profile("unknown").id == "balanced"


def test_profiles_endpoint_and_settings_round_trip(api, repo):
    profiles = {p["id"]: p for p in api.get("/api/v1/review-profiles").json()}
    assert set(profiles) == {"strict", "balanced", "lenient", "security"}
    assert profiles["security"]["categories"] == ["security"]
    assert profiles["lenient"]["defaults"] == {
        "min_severity": "high",
        "min_confidence": 0.7,
        "max_inline_comments": 10,
    }

    patch = {"profile": "security", "review_on_push": False, "check_runs": False, "gate_severity": "critical"}
    response = api.patch(f"/api/v1/repositories/{repo.pk}/settings", patch, format="json")
    assert response.status_code == 200, response.json()
    data = api.get(f"/api/v1/repositories/{repo.pk}/settings").json()
    assert {k: data[k] for k in patch} == patch
    assert (
        api.patch(f"/api/v1/repositories/{repo.pk}/settings", {"profile": "bogus"}, format="json").status_code
        == 400
    )


def test_review_detail_exposes_new_fields(api, repo, git):
    git.add_comparison(REPO, OLD, NEW, CompareResult("ahead", [added_file("README.md", README_LINES)]))
    result = execute(push_run(repo, profile="strict"), git, ScriptedLLM(empty_review))
    data = api.get(f"/api/v1/reviews/{result.pk}").json()
    assert (data["trigger"], data["incremental"], data["compare_base_sha"], data["profile"]) == (
        "push",
        True,
        OLD,
        "strict",
    )
