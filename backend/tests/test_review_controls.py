"""Batch A: `.reviewbot.yml` (RE-15), rules (RE-14), PR comment commands (RE-17), branch filters (REPO-03)."""

from __future__ import annotations

import pytest

from apps.git_providers.fake import FakeGitProvider
from apps.reviews import pipeline
from apps.reviews.engine.repo_config import (
    ConfigError,
    FileConfig,
    applicable_rules,
    branch_matches,
    merge_config,
    normalize_rules,
    parse_config_file,
)
from apps.reviews.models import PullRequest, ReviewRun
from apps.reviews.services import create_run
from apps.webhooks.github_handlers import parse_command
from apps.webhooks.models import WebhookDelivery
from tests.factories import (
    ScriptedLLM,
    added_file,
    finding,
    make_connection,
    make_pull_request,
    make_repository,
    pr_info,
)
from tests.test_webhooks import post, pr_payload

REPO = "acme/app"
BASE = "b" * 40
RULE = {
    "id": "no-print",
    "description": "No print() in library code",
    "paths": ["src/**"],
    "severity": "medium",
}


# --- pure config logic ---------------------------------------------------------------------------


def test_normalize_rules_defaults_and_validation():
    rules = normalize_rules([{"id": "R1", "description": "  Use   logging "}])
    assert rules == [
        {"id": "R1", "description": "Use logging", "severity": "medium", "paths": [], "enabled": True}
    ]
    for bad, message in [
        ([{"id": "bad id", "description": "x"}], "id must be"),
        ([{"id": "R1", "description": "x"}, {"id": "R1", "description": "y"}], "duplicate"),
        ([{"id": "R1", "description": ""}], "description is required"),
        ([{"id": "R1", "description": "x", "severity": "blocker"}], "severity"),
        ("nope", "must be a list"),
    ]:
        with pytest.raises(ConfigError, match=message):
            normalize_rules(bad)


def test_rules_scoped_by_path():
    rules = normalize_rules([RULE, {"id": "global", "description": "Everywhere"}])
    assert [r["id"] for r in applicable_rules(rules, ["src/app.py"])] == ["no-print", "global"]
    assert [r["id"] for r in applicable_rules(rules, ["docs/readme.md"])] == ["global"]
    disabled = normalize_rules([{**RULE, "enabled": False}])
    assert applicable_rules(disabled, ["src/app.py"]) == []


def test_branch_matching():
    assert branch_matches("anything", [])
    assert branch_matches("release/1.2", ["main", "release/*"])
    assert not branch_matches("feature/x", ["main", "release/*"])


def test_parse_config_file():
    config = parse_config_file(
        "profile: security\nmin_severity: HIGH\nignore_patterns: [docs/]\ninstructions: Use pytest.\n"
        "rules:\n  - id: R1\n    description: No eval\nmax_changed_lines: 99999\n"
    )
    assert config.values["profile"] == "security"
    assert config.values["min_severity"] == "high"
    assert config.values["rules"][0]["id"] == "R1"
    assert config.warnings == ["unknown key 'max_changed_lines' ignored"]  # cost guards stay in the dashboard


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("profile: [unclosed", "invalid YAML"),
        ("- just a list", "must be a mapping"),
        ("profile: paranoid", "profile must be"),
        ("min_confidence: 3", "min_confidence"),
        ("max_inline_comments: lots", "max_inline_comments"),
    ],
)
def test_parse_config_file_errors(text, message):
    with pytest.raises(ConfigError, match=message):
        parse_config_file(text)


def test_merge_config():
    snapshot = {
        "profile": "balanced",
        "ignore_patterns": ["vendor/"],
        "custom_instructions": "Dashboard text.",
        "rules": [{"id": "R1", "description": "old"}, {"id": "R2", "description": "keep"}],
        "max_changed_lines": 2000,
    }
    merged = merge_config(
        snapshot,
        FileConfig(
            values={
                "profile": "strict",
                "ignore_patterns": ["docs/"],
                "instructions": "File text.",
                "rules": [{"id": "R1", "description": "new"}],
            }
        ),
    )
    assert merged["profile"] == "strict"
    assert merged["ignore_patterns"] == ["vendor/", "docs/"]
    assert merged["custom_instructions"] == "Dashboard text.\n\nFile text."
    assert {r["id"]: r["description"] for r in merged["rules"]} == {"R1": "new", "R2": "keep"}
    assert merged["max_changed_lines"] == 2000


def test_parse_command():
    assert parse_command("/reviewbot review") == "review"
    assert parse_command("  /ReviewBot   IGNORE please\nmore") == "ignore"
    assert parse_command("/reviewbot") == "help"
    assert parse_command("looks good /reviewbot review") is None
    assert parse_command("") is None


# --- pipeline ------------------------------------------------------------------------------------


@pytest.fixture
def repo(fake_credential):
    return make_repository(credential=fake_credential, name="app")


@pytest.fixture
def git():
    provider = FakeGitProvider()
    provider.add_pull_request(
        REPO, pr_info(1, base_sha=BASE), [added_file("src/app.py", [f"x{i} = {i}" for i in range(10)])]
    )
    return provider


def run_review(repo, git, llm, **settings):
    for key, value in settings.items():
        setattr(repo.settings, key, value)
    repo.settings.save()
    run_obj = create_run(make_pull_request(repo, 1), trigger=ReviewRun.Trigger.MANUAL, head_sha="a" * 40)
    pipeline.execute(run_obj.pk, git=git, llm=llm)
    run_obj.refresh_from_db()
    return run_obj


@pytest.mark.django_db
def test_rules_reach_prompt_and_tag_findings(repo, git):
    llm = ScriptedLLM(
        [
            {
                "summary": "",
                "findings": [
                    finding("src/app.py", 3, severity="low", title="print call", rule_id="no-print"),
                    finding("src/app.py", 5, title="Made-up rule", rule_id="ghost"),
                ],
            }
        ]
    )
    result = run_review(repo, git, llm, rules=normalize_rules([RULE]))
    assert "[no-print] (medium) No print() in library code" in llm.calls[0]["user"]
    findings = {f.title: f for f in result.findings.all()}
    assert (findings["print call"].rule_id, findings["print call"].severity) == (
        "no-print",
        "medium",
    )  # raised
    assert findings["Made-up rule"].rule_id == ""  # unknown ids are dropped
    assert "rule `no-print`" in next(c.body for c in git.posted[0].comments if c.line == 3)


@pytest.mark.django_db
def test_config_file_from_base_commit_is_applied(repo, git):
    git.contents[(REPO, ".reviewbot.yml", BASE)] = (
        "profile: security\ninstructions: Flag raw SQL.\nrules:\n  - id: SEC1\n    description: No eval\n"
    )
    llm = ScriptedLLM([{"summary": "", "findings": [finding("src/app.py", 3, category="bug")]}])
    result = run_review(repo, git, llm)
    assert (result.config_source, result.config_error) == (".reviewbot.yml", "")
    assert "Flag raw SQL." in llm.calls[0]["system"]
    assert "Security-focused" in llm.calls[0]["system"]
    assert "[SEC1]" in llm.calls[0]["user"]
    assert result.settings_snapshot["profile"] == "security"
    assert result.findings.get().post_status == "category_filtered"


@pytest.mark.django_db
def test_config_file_on_head_branch_is_ignored(repo, git):
    git.contents[(REPO, ".reviewbot.yml", "a" * 40)] = "profile: lenient\n"  # only on the PR head
    llm = ScriptedLLM([{"summary": "", "findings": []}])
    result = run_review(repo, git, llm)
    assert result.config_source == "dashboard"
    assert "Lenient" not in llm.calls[0]["system"]


@pytest.mark.django_db
def test_invalid_config_file_does_not_block_review(repo, git):
    git.contents[(REPO, ".reviewbot.yml", BASE)] = "profile: [oops"
    result = run_review(repo, git, ScriptedLLM([{"summary": "", "findings": []}]))
    assert result.status == "completed"
    assert result.config_source == "dashboard"
    assert "invalid YAML" in result.config_error
    assert "Configuration problem" in git.posted[0].body


# --- webhooks: branch filters and commands -------------------------------------------------------


@pytest.fixture
def enqueued(monkeypatch):
    from tests.factories import EnqueueRecorder

    calls = EnqueueRecorder()
    monkeypatch.setattr("apps.reviews.services.enqueue", calls)
    return calls


def comment_payload(repo, body, association="MEMBER", number=7, user_type="User"):
    return {
        "action": "created",
        "installation": {"id": repo.installation.external_id},
        "repository": {"id": repo.external_id, "full_name": repo.full_name},
        "issue": {
            "number": number,
            "title": "Add search",
            "user": {"login": "octocat"},
            "pull_request": {"url": "https://api.github.com/x"},
            "html_url": f"https://github.com/{repo.full_name}/pull/{number}",
        },
        "comment": {
            "body": body,
            "author_association": association,
            "user": {"login": "dev", "type": user_type},
        },
    }


def last_reason():
    return WebhookDelivery.objects.order_by("-id").first().reason


@pytest.mark.django_db
def test_base_branch_filter(anon, fake_credential, enqueued):
    make_connection()
    repo = make_repository(credential=fake_credential, base_branch_patterns=["release/*"])
    post(anon, "pull_request", pr_payload(repo))  # targets main
    assert last_reason() == "base_branch_filtered"
    assert ReviewRun.objects.count() == 0


@pytest.mark.django_db
def test_review_command_queues_review(anon, fake_credential, enqueued):
    make_connection()
    repo = make_repository(credential=fake_credential, auto_review=False)
    response = post(anon, "issue_comment", comment_payload(repo, "/reviewbot review"))
    assert response.status_code == 202
    run = ReviewRun.objects.get()
    assert (run.trigger, run.status) == ("command", "queued")
    assert PullRequest.objects.get().number == 7
    post(anon, "issue_comment", comment_payload(repo, "/reviewbot review"))
    assert last_reason() == "review_already_running"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("body", "association", "user_type", "reason"),
    [
        ("/reviewbot review", "CONTRIBUTOR", "User", "insufficient_permission"),
        ("/reviewbot review", "NONE", "User", "insufficient_permission"),
        ("/reviewbot dance", "MEMBER", "User", "unknown_command:dance"),
        ("nice work", "MEMBER", "User", "no_command"),
        ("/reviewbot review", "MEMBER", "Bot", "comment_from_bot"),
    ],
)
def test_commands_are_rejected(anon, fake_credential, enqueued, body, association, user_type, reason):
    make_connection()
    repo = make_repository(credential=fake_credential)
    post(anon, "issue_comment", comment_payload(repo, body, association, user_type=user_type))
    assert last_reason() == reason
    assert ReviewRun.objects.count() == 0


@pytest.mark.django_db
def test_comment_on_plain_issue_is_ignored(anon, fake_credential, enqueued):
    make_connection()
    repo = make_repository(credential=fake_credential)
    payload = comment_payload(repo, "/reviewbot review")
    del payload["issue"]["pull_request"]
    post(anon, "issue_comment", payload)
    assert last_reason() == "not_a_pull_request"


@pytest.mark.django_db
def test_ignore_and_resume_commands(anon, fake_credential, enqueued):
    make_connection()
    repo = make_repository(credential=fake_credential)
    post(anon, "issue_comment", comment_payload(repo, "/reviewbot ignore"))
    assert PullRequest.objects.get().reviews_paused is True
    post(anon, "pull_request", pr_payload(repo, "reopened"))
    assert last_reason() == "reviews_paused"
    post(anon, "issue_comment", comment_payload(repo, "/reviewbot resume"))
    assert PullRequest.objects.get().reviews_paused is False
    post(anon, "pull_request", pr_payload(repo, "reopened"))
    assert last_reason() == "review_queued"


# --- API -----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_settings_api_rules_and_branches(api, fake_credential):
    repo = make_repository(credential=fake_credential)
    url = f"/api/v1/repositories/{repo.pk}/settings"
    response = api.patch(
        url, {"rules": [RULE], "base_branch_patterns": ["main", " ", "release/*"]}, format="json"
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["base_branch_patterns"] == ["main", "release/*"]
    assert data["rules"][0]["id"] == "no-print" and data["rules"][0]["enabled"] is True
    bad = api.patch(url, {"rules": [{"id": "x y", "description": "d"}]}, format="json")
    assert bad.status_code == 400 and "id must be" in bad.json()["error"]["message"]
