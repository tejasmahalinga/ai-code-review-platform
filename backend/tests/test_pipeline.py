from __future__ import annotations

import pytest

from apps.git_providers.base import ChangedFile, GitProviderError
from apps.git_providers.fake import FakeGitProvider
from apps.llm.base import AuthenticationFailed
from apps.reviews import pipeline
from apps.reviews.models import Finding, LLMUsage, ReviewRun
from apps.reviews.services import create_run
from tests.factories import ScriptedLLM, added_file, finding, make_pull_request, make_repository, pr_info

pytestmark = pytest.mark.django_db

REPO = "acme/app"
APP_LINES = [f"line_{i} = {i}" for i in range(1, 21)]


@pytest.fixture
def repo(fake_credential):
    return make_repository(credential=fake_credential, name="app")


@pytest.fixture
def git():
    provider = FakeGitProvider()
    provider.add_pull_request(REPO, pr_info(1), [added_file("src/app.py", APP_LINES)])
    return provider


def queue(repo, **settings):
    if settings:
        for key, value in settings.items():
            setattr(repo.settings, key, value)
        repo.settings.save()
    pr = make_pull_request(repo, 1)
    return create_run(pr, trigger=ReviewRun.Trigger.MANUAL, head_sha="a" * 40)


def run(run_obj, git, llm):
    pipeline.execute(run_obj.pk, git=git, llm=llm)
    run_obj.refresh_from_db()
    return run_obj


def test_posts_one_review_with_inline_and_summary_findings(repo, git):
    llm = ScriptedLLM(
        [
            {
                "summary": "Some issues.",
                "findings": [
                    finding("src/app.py", 3, title="First"),
                    finding("src/app.py", 5, severity="critical", category="security", title="Second"),
                    finding("src/app.py", 7, severity="medium", title="Third", suggestion="line_7 = 70"),
                    finding("src/app.py", 400, title="Outside the diff"),
                ],
            }
        ]
    )
    result = run(queue(repo), git, llm)
    assert result.status == "completed", result.error
    assert len(git.posted) == 1
    posted = git.posted[0]
    assert [c.line for c in posted.comments] == [5, 3, 7]  # severity order
    assert "Outside the diff" in posted.body
    assert "```suggestion\nline_7 = 70\n```" in posted.comments[2].body
    statuses = dict(Finding.objects.values_list("title", "post_status"))
    assert statuses == {
        "First": "posted",
        "Second": "posted",
        "Third": "posted",
        "Outside the diff": "in_summary",
    }
    assert all(f.provider_comment_url for f in Finding.objects.filter(post_status="posted"))
    assert result.provider_review_url
    assert result.pull_request.last_reviewed_sha == "a" * 40
    # Usage recorded per LLM call (KEY-03).
    usage = LLMUsage.objects.get()
    assert (usage.input_tokens, usage.output_tokens, usage.status) == (100, 20, "ok")
    assert (result.input_tokens, result.output_tokens) == (100, 20)


def test_severity_and_confidence_thresholds_and_cap(repo, git):
    findings = [finding("src/app.py", n, title=f"F{n}", severity="medium") for n in range(1, 8)]
    findings += [
        finding("src/app.py", 10, title="Info", severity="info"),
        finding("src/app.py", 11, title="Unsure", confidence=0.2),
    ]
    llm = ScriptedLLM([{"summary": "", "findings": findings}])
    result = run(queue(repo, max_inline_comments=5, min_severity="low"), git, llm)
    counts = {}
    for status in Finding.objects.values_list("post_status", flat=True):
        counts[status] = counts.get(status, 0) + 1
    assert counts == {"posted": 5, "cap_exceeded": 2, "below_threshold": 1, "low_confidence": 1}
    assert len(git.posted[0].comments) == 5
    assert "2 more finding(s)" in git.posted[0].body
    assert result.status == "completed"


def test_rerun_does_not_duplicate_comments(repo, git):
    response = {"summary": "", "findings": [finding("src/app.py", 4, title="Same issue")]}
    llm = ScriptedLLM(lambda s, u: response)
    first = run(queue(repo), git, llm)
    second = run(create_run(first.pull_request, trigger="manual", head_sha="a" * 40), git, llm)
    assert len(git.posted[0].comments) == 1
    assert len(git.posted[1].comments) == 0
    assert second.findings.get().post_status == "duplicate"


def test_duplicate_detected_after_code_shifts(repo, git):
    llm = ScriptedLLM(
        lambda s, u: {"summary": "", "findings": [finding("src/app.py", 4, title="Same issue")]}
    )
    first = run(queue(repo), git, llm)
    # New commit inserts lines above: identical code is now at line 14.
    git.add_pull_request(REPO, pr_info(1, head_sha="c" * 40), [added_file("src/app.py", APP_LINES, start=11)])
    llm2 = ScriptedLLM([{"summary": "", "findings": [finding("src/app.py", 14, title="Same issue")]}])
    second = run(create_run(first.pull_request, trigger="manual", head_sha="c" * 40), git, llm2)
    assert second.findings.get().post_status == "duplicate"


def test_only_ignored_files_makes_no_llm_calls(repo, git):
    git.add_pull_request(REPO, pr_info(1), [added_file("package-lock.json", ["{}"])])
    llm = ScriptedLLM([])
    result = run(queue(repo), git, llm)
    assert result.status == "skipped"
    assert result.status_reason == "no_reviewable_files"
    assert llm.calls == []
    assert result.files_ignored == [
        {"path": "package-lock.json", "reason": "ignore_pattern", "pattern": "package-lock.json"}
    ]


def test_too_many_changed_lines_is_skipped_with_comment(repo, git):
    llm = ScriptedLLM([])
    result = run(queue(repo, max_changed_lines=5), git, llm)
    assert (result.status, result.status_reason) == ("skipped", "too_large")
    assert llm.calls == []
    assert len(git.posted) == 1 and "above the limit of 5" in git.posted[0].body


def test_deleted_and_binary_files_are_not_sent(repo, git):
    git.add_pull_request(
        REPO,
        pr_info(1),
        [
            added_file("src/app.py", APP_LINES),
            ChangedFile("old.py", "removed", 0, 10, "@@ -1,1 +0,0 @@\n-x"),
            ChangedFile("data.bin", "added", 0, 0, None),
        ],
    )
    llm = ScriptedLLM([{"summary": "", "findings": []}])
    result = run(queue(repo), git, llm)
    reasons = {i["path"]: i["reason"] for i in result.files_ignored}
    assert reasons == {"old.py": "deleted", "data.bin": "no_textual_changes"}
    assert "old.py" not in llm.calls[0]["user"]


def test_invalid_json_gets_one_repair_retry(repo, git):
    llm = ScriptedLLM(["garbage", {"summary": "ok", "findings": []}])
    result = run(queue(repo), git, llm)
    assert result.status == "completed"
    assert len(llm.calls) == 2
    assert "previous answer was not valid" in llm.calls[1]["user"]
    assert LLMUsage.objects.filter(status="error").count() == 1


def test_repeated_invalid_output_fails_run(repo, git):
    llm = ScriptedLLM(["garbage", {"summary": "x"}])
    result = run(queue(repo), git, llm)
    assert (result.status, result.status_reason, result.stage) == ("failed", "llm_failed", "llm")
    assert git.posted == []


def test_auth_error_marks_credential_invalid(repo, git, fake_credential):
    llm = ScriptedLLM([AuthenticationFailed("bad key")])
    result = run(queue(repo), git, llm)
    assert result.status_reason == "llm_auth_failed"
    fake_credential.refresh_from_db()
    assert fake_credential.status == "invalid"


def test_revoked_credential_fails_fast(repo, git, fake_credential):
    run_obj = queue(repo)
    fake_credential.revoke()
    result = run(run_obj, git, ScriptedLLM([]))
    assert (result.status, result.status_reason) == ("failed", "no_credential")


def test_rejected_inline_positions_fall_back_to_summary(repo, git):
    git.reject_inline = True
    llm = ScriptedLLM([{"summary": "", "findings": [finding("src/app.py", 2, title="Moved")]}])
    result = run(queue(repo), git, llm)
    assert result.status == "completed"
    assert len(git.posted) == 1 and git.posted[0].comments == []
    assert "Moved" in git.posted[0].body
    assert result.findings.get().post_status == "in_summary"


def test_retryable_git_error_requeues(repo, git):
    git.fail_with = GitProviderError("rate limited", status=403, retry_after=30)
    run_obj = queue(repo)
    with pytest.raises(pipeline.RetryLater):
        pipeline.execute(run_obj.pk, git=git, llm=ScriptedLLM([]))
    run_obj.refresh_from_db()
    assert run_obj.status == "queued"


def test_access_denied_fails_with_hint(repo, git):
    git.fail_with = GitProviderError("Not Found", status=404)
    result = run(queue(repo), git, ScriptedLLM([]))
    assert (result.status, result.status_reason) == ("failed", "git_access_denied")
    assert "still installed" in result.error


def test_multi_chunk_review_merges_findings(repo, git):
    files = [
        added_file(
            f"src/m{i}.py", [f"value_{j} = compute({j}) # padding padding padding" for j in range(150)]
        )
        for i in range(4)
    ]
    git.add_pull_request(REPO, pr_info(1), files)

    def script(system, user):
        path = next(line.split()[2] for line in user.splitlines() if line.startswith("### File:"))
        return {"summary": f"Reviewed {path}", "findings": [finding(path, 2, title=f"Issue in {path}")]}

    llm = ScriptedLLM(script)
    result = run(queue(repo, chunk_tokens=4_000), git, llm)
    assert result.chunk_count > 1
    assert len(llm.calls) == result.chunk_count
    assert LLMUsage.objects.count() == result.chunk_count
    assert result.findings.count() == result.chunk_count
    assert result.status == "completed"


def test_custom_instructions_reach_the_prompt(repo, git):
    llm = ScriptedLLM([{"summary": "", "findings": []}])
    run(queue(repo, custom_instructions="We use Django; flag raw SQL."), git, llm)
    assert "We use Django; flag raw SQL." in llm.calls[0]["system"]


def test_completed_run_is_not_reprocessed(repo, git):
    llm = ScriptedLLM(lambda s, u: {"summary": "", "findings": []})
    first = run(queue(repo), git, llm)
    pipeline.execute(first.pk, git=git, llm=llm)
    assert len(git.posted) == 1


def test_closed_pull_request_is_skipped(repo, git):
    git.add_pull_request(REPO, pr_info(1, state="closed"), [])
    result = run(queue(repo), git, ScriptedLLM([]))
    assert (result.status, result.status_reason) == ("skipped", "pull_request_closed")


def test_no_findings_posts_clean_summary_by_default(repo, git):
    run(queue(repo), git, ScriptedLLM([{"summary": "Looks good.", "findings": []}]))
    assert "No issues found." in git.posted[0].body


def test_no_findings_can_post_nothing(repo, git):
    result = run(
        queue(repo, post_when_no_findings=False), git, ScriptedLLM([{"summary": "", "findings": []}])
    )
    assert result.status == "completed"
    assert git.posted == []
