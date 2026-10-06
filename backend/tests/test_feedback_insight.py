"""Batch B: feedback (RE-16), run comparison (PR-04), filters/search (PR-05), risk score (RE-12),
test suggestions (RE-13)."""

from __future__ import annotations

import pytest

from apps.git_providers.fake import FakeGitProvider
from apps.reviews import pipeline
from apps.reviews.engine.risk import compute_risk, risk_bucket
from apps.reviews.engine.test_gaps import TEST_PROMPT, is_test_path, needs_test_suggestions
from apps.reviews.models import Finding, FindingFeedback, ReviewRun
from apps.reviews.services import create_run
from tests.factories import ScriptedLLM, added_file, finding, make_pull_request, make_repository, pr_info

REPO = "acme/app"


# --- pure functions ------------------------------------------------------------------------------


def test_risk_score_is_deterministic_and_bounded():
    first = compute_risk(["critical", "high"], ["src/app.py"], 120)
    assert first == compute_risk(["critical", "high"], ["src/app.py"], 120)
    assert first.score == 30 + 15 + 3
    assert compute_risk(["critical"] * 10, [".github/workflows/ci.yml", "auth/views.py"], 5000).score == 100


@pytest.mark.parametrize(
    ("path", "label"),
    [
        (".github/workflows/deploy.yml", "CI workflows"),
        ("app/auth/backends.py", "authentication / authorization"),
        ("app/migrations/0002_x.py", "database migrations"),
        ("Dockerfile", "container / deployment"),
        ("web/package.json", "dependency manifests"),
    ],
)
def test_sensitive_paths(path, label):
    result = compute_risk([], [path], 0)
    assert label in result.sensitive and result.score > 0


def test_workflow_change_adds_at_least_15():
    assert compute_risk([], [".github/workflows/ci.yml"], 0).score >= 15


def test_risk_buckets():
    assert [risk_bucket(s) for s in (0, 29, 30, 59, 60, 100, None)] == [
        "low", "low", "medium", "medium", "high", "high", "unknown"
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["src/foo.py"], True),
        (["src/foo.py", "tests/test_foo.py"], False),
        (["web/app.ts", "web/app.test.ts"], False),
        (["README.md", "docs/guide.md"], False),
        (["pkg/server.go", "pkg/server_test.go"], False),
    ],
)
def test_needs_test_suggestions(paths, expected):
    assert needs_test_suggestions(paths) is expected


def test_is_test_path():
    assert is_test_path("spec/models/user_spec.rb")
    assert is_test_path("src/__tests__/a.tsx")
    assert not is_test_path("src/testimony.py")


# --- pipeline ------------------------------------------------------------------------------------


@pytest.fixture
def repo(fake_credential):
    return make_repository(credential=fake_credential, name="app")


def review(repo, files, script, number=1, **settings):
    for key, value in settings.items():
        setattr(repo.settings, key, value)
    repo.settings.save()
    git = FakeGitProvider()
    git.add_pull_request(REPO, pr_info(number), files)
    pr = repo.pull_requests.filter(number=number).first() or make_pull_request(repo, number)
    run = create_run(pr, trigger=ReviewRun.Trigger.MANUAL, head_sha="a" * 40)
    llm = ScriptedLLM(script)
    pipeline.execute(run.pk, git=git, llm=llm)
    run.refresh_from_db()
    return run, git, llm


SOURCE = [added_file("src/foo.py", [f"x{i} = {i}" for i in range(80)])]


@pytest.mark.django_db
def test_test_suggestions_prompted_and_capped(repo):
    script = [{"summary": "", "findings": [finding("src/foo.py", 2, category="test", severity="critical")]}]
    run, _, llm = review(repo, SOURCE, script)
    assert TEST_PROMPT in llm.calls[0]["system"]
    assert run.findings.get().severity == "medium"


@pytest.mark.django_db
def test_no_test_prompt_when_tests_change(repo):
    files = [*SOURCE, added_file("tests/test_foo.py", ["def test_x(): pass"])]
    _, _, llm = review(repo, files, [{"summary": "", "findings": []}])
    assert TEST_PROMPT not in llm.calls[0]["system"]


@pytest.mark.django_db
def test_test_suggestions_can_be_disabled(repo):
    script = [{"summary": "", "findings": [finding("src/foo.py", 2, category="test", severity="low")]}]
    run, _, llm = review(repo, SOURCE, script, suggest_tests=False)
    assert TEST_PROMPT not in llm.calls[0]["system"]
    assert run.findings.get().post_status == "category_filtered"


@pytest.mark.django_db
def test_risk_score_recorded_and_in_summary(repo):
    files = [*SOURCE, added_file(".github/workflows/ci.yml", ["on: push"])]
    script = [{"summary": "", "findings": [finding("src/foo.py", 2, severity="high")]}]
    run, git, _ = review(repo, files, script)
    assert run.risk_score == 15 + 3 + 15  # high finding + 81 changed lines + workflow change
    assert f"**Risk:** {run.risk_score}/100 (medium)" in git.posted[0].body
    assert f"risk {run.risk_score}/100" in git.check_runs[0].summary


@pytest.mark.django_db
def test_dismissed_findings_are_not_reposted(repo):
    script = lambda s, u: {"summary": "", "findings": [finding("src/foo.py", 2, title="Noisy")]}  # noqa: E731
    first, _git, _ = review(repo, SOURCE, script)
    Finding.objects.filter(review_run=first).update(state="dismissed", dismiss_reason="false_positive")
    second, git2, _ = review(repo, SOURCE, script)
    f = second.findings.get()
    assert (f.post_status, f.state) == ("dismissed_earlier", "dismissed")
    assert git2.posted[0].comments == []


# --- API -----------------------------------------------------------------------------------------


@pytest.fixture
def run_with_findings(repo):
    pr = make_pull_request(repo, 1)
    run = ReviewRun.objects.create(
        pull_request=pr, trigger="manual", head_sha="x", status="completed", risk_score=70
    )
    for fp, title, category, severity in [
        ("f1", "SQL injection", "security", "critical"),
        ("f2", "Off by one", "bug", "low"),
    ]:
        Finding.objects.create(
            review_run=run, fingerprint=fp, path="a.py", category=category, severity=severity,
            title=title, body="b", post_status="posted",
        )  # fmt: skip
    return run


@pytest.mark.django_db
def test_accept_dismiss_and_vote(api, admin_user, run_with_findings):
    f = run_with_findings.findings.get(fingerprint="f2")
    response = api.patch(
        f"/api/v1/findings/{f.pk}", {"state": "dismissed", "dismiss_reason": "false_positive"}, format="json"
    )
    assert response.status_code == 200
    f.refresh_from_db()
    assert (f.state, f.dismiss_reason, f.state_changed_by_id) == (
        "dismissed",
        "false_positive",
        admin_user.pk,
    )
    assert f.state_changed_at is not None
    assert (
        api.patch(f"/api/v1/findings/{f.pk}", {"state": "accepted"}, format="json").json()["dismiss_reason"]
        == ""
    )
    assert api.patch(f"/api/v1/findings/{f.pk}", {"state": "bogus"}, format="json").status_code == 400

    data = api.put(f"/api/v1/findings/{f.pk}/feedback", {"vote": "up"}, format="json").json()
    assert data["votes"] == {"up": 1, "down": 0, "mine": "up"}
    data = api.put(f"/api/v1/findings/{f.pk}/feedback", {"vote": "down"}, format="json").json()
    assert data["votes"] == {"up": 0, "down": 1, "mine": "down"}  # one vote per user
    assert FindingFeedback.objects.count() == 1
    data = api.delete(f"/api/v1/findings/{f.pk}/feedback").json()
    assert data["votes"] == {"up": 0, "down": 0, "mine": None}


@pytest.mark.django_db
def test_feedback_stats(api, run_with_findings):
    security = run_with_findings.findings.get(fingerprint="f1")
    bug = run_with_findings.findings.get(fingerprint="f2")
    api.patch(f"/api/v1/findings/{security.pk}", {"state": "accepted"}, format="json")
    api.patch(
        f"/api/v1/findings/{bug.pk}",
        {"state": "dismissed", "dismiss_reason": "false_positive"},
        format="json",
    )
    api.put(f"/api/v1/findings/{security.pk}/feedback", {"vote": "up"}, format="json")
    stats = {row["category"]: row for row in api.get("/api/v1/feedback-stats").json()}
    assert stats["security"]["acceptance_rate"] == 1.0 and stats["security"]["up"] == 1
    assert stats["bug"]["acceptance_rate"] == 0.0 and stats["bug"]["false_positive"] == 1


@pytest.mark.django_db
def test_compare_runs(api, repo, run_with_findings):
    newer = ReviewRun.objects.create(
        pull_request=run_with_findings.pull_request, trigger="manual", head_sha="y", status="completed"
    )
    for fp, title in [("f1", "SQL injection"), ("f3", "New issue")]:
        Finding.objects.create(
            review_run=newer, fingerprint=fp, path="a.py", category="bug", severity="high",
            title=title, body="b", post_status="posted",
        )  # fmt: skip
    data = api.get(f"/api/v1/reviews/{newer.pk}/compare", {"with": run_with_findings.pk}).json()
    assert [f["title"] for f in data["added"]] == ["New issue"]
    assert [f["title"] for f in data["resolved"]] == ["Off by one"]
    assert data["unchanged"] == 1
    Finding.objects.filter(review_run=newer, fingerprint="f1").update(post_status="dismissed_earlier")
    again = api.get(f"/api/v1/reviews/{newer.pk}/compare", {"with": run_with_findings.pk}).json()
    assert again["unchanged"] == 1
    other_pr_run = ReviewRun.objects.create(
        pull_request=make_pull_request(repo, 2), trigger="manual", head_sha="z"
    )
    assert api.get(f"/api/v1/reviews/{newer.pk}/compare", {"with": other_pr_run.pk}).status_code == 400


@pytest.mark.django_db
def test_pull_request_filters(api, repo, run_with_findings):
    calm = make_pull_request(repo, 2, title="Docs tweak")
    ReviewRun.objects.create(
        pull_request=calm, trigger="manual", head_sha="z", status="completed", risk_score=5
    )

    def numbers(**params):
        return sorted(r["number"] for r in api.get("/api/v1/pull-requests", params).json()["results"])

    assert numbers(min_severity="high") == [1]
    assert numbers(min_severity="info") == [1]
    assert numbers(risk="high") == [1]
    assert numbers(risk="low") == [2]
    assert numbers(q="injection") == [1]  # matches a finding title
    assert numbers(q="docs") == [2]
    assert api.get("/api/v1/pull-requests", {"risk": "extreme"}).status_code == 400
    item = api.get("/api/v1/pull-requests", {"q": "injection"}).json()["results"][0]
    assert item["latest_review"]["risk_score"] == 70


@pytest.mark.django_db
def test_suggest_tests_setting_round_trip(api, repo):
    response = api.patch(f"/api/v1/repositories/{repo.pk}/settings", {"suggest_tests": False}, format="json")
    assert response.json()["suggest_tests"] is False
