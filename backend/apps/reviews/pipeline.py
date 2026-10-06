"""The review pipeline: fetch diff → filter → chunk → LLM → aggregate → post.

Each stage is persisted on the ReviewRun so the dashboard shows progress and failures point at the
stage that broke. The LLM never gets tools or write access; its output is schema-validated and only
ever posted as a comment.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.logging import get_logger
from apps.credentials import budgets, pricing
from apps.credentials.models import LLMCredential
from apps.credentials.services import mark_invalid, provider_for
from apps.git_providers import registry as git_registry
from apps.git_providers.base import ChangedFile, GitProvider, GitProviderError, InlineComment
from apps.git_providers.github.client import InlineCommentsRejected
from apps.llm.base import AuthenticationFailed, InvalidResponse, LLMError, LLMProvider, TokenUsage
from apps.repositories.models import SEVERITY_RANK
from apps.reviews.engine import prompts, render
from apps.reviews.engine.chunker import Chunk, build_chunks, estimate_tokens
from apps.reviews.engine.diff import FileDiff, parse_patch
from apps.reviews.engine.fingerprint import fingerprint
from apps.reviews.engine.ignore import IgnoreMatcher
from apps.reviews.engine.profiles import get_profile
from apps.reviews.engine.repo_config import (
    CONFIG_PATH,
    ConfigError,
    applicable_rules,
    merge_config,
    parse_config_file,
    render_rules,
)
from apps.reviews.engine.risk import compute_risk
from apps.reviews.engine.schema import REVIEW_SCHEMA, ChunkReview, RawFinding, SchemaError, parse_review
from apps.reviews.engine.test_gaps import MAX_TEST_SEVERITY, TEST_PROMPT, needs_test_suggestions
from apps.reviews.models import Finding, LLMUsage, PullRequest, ReviewRun

logger = get_logger(__name__)

MAX_OUTPUT_TOKENS = 8_000
LLM_CONCURRENCY = 3
Status = ReviewRun.Status
Stage = ReviewRun.Stage
PostStatus = Finding.PostStatus


class RetryLater(Exception):
    def __init__(self, countdown: float, reason: str):
        super().__init__(reason)
        self.countdown = countdown


class StageFailure(Exception):
    def __init__(self, message: str, *, reason: str = "error"):
        super().__init__(message)
        self.reason = reason


@dataclass
class ChunkResult:
    chunk: Chunk
    review: ChunkReview | None = None
    error: LLMError | None = None
    calls: list[tuple[TokenUsage, int, str]] = field(default_factory=list)  # (usage, latency_ms, error_code)


def dashboard_url(run: ReviewRun) -> str:
    return f"{settings.PUBLIC_URL}/reviews/{run.pk}"


# --- entry point -------------------------------------------------------------------------------


def execute(
    run_id: int, *, git: GitProvider | None = None, llm: LLMProvider | None = None
) -> ReviewRun | None:
    with transaction.atomic():
        run = (
            ReviewRun.objects.select_for_update(of=("self",))
            .select_related("pull_request__repository__installation__connection", "credential")
            .filter(pk=run_id)
            .first()
        )
        if run is None or run.status not in (Status.QUEUED, Status.RUNNING):
            return run
        lease = timezone.now() - timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)
        if run.status == Status.RUNNING and run.started_at and run.started_at > lease:
            # Another worker is processing this run (duplicate delivery); only take over stale runs.
            return run
        if run.provider_review_id:
            # Comments were already posted before a crash; never post twice.
            _finish(run, Status.COMPLETED)
            return run
        run.status = Status.RUNNING
        run.started_at = run.started_at or timezone.now()
        run.error = ""
        run.save(update_fields=["status", "started_at", "error"])

    log = logger.bind(
        review_run_id=run.pk, repo=run.pull_request.repository.full_name, pr=run.pull_request.number
    )
    log.info("review.started")
    provider: GitProvider | None = git
    try:
        provider = provider or git_registry.provider_for(run.pull_request.repository)
        _run(run, git=provider, llm=llm, log=log)
    except RetryLater:
        run.status = Status.QUEUED
        run.save(update_fields=["status"])
        raise
    except StageFailure as exc:
        run.error = str(exc)[:2000]
        _finish(run, Status.FAILED, reason=exc.reason)
        log.warning("review.failed", stage=run.stage, error=str(exc))
    except Exception as exc:
        log.exception("review.crashed", stage=run.stage)
        run.error = f"Internal error during '{run.stage}': {type(exc).__name__}"
        _finish(run, Status.FAILED, reason="internal_error")
    _complete_check(run, provider, log)
    return run


def _set_stage(run: ReviewRun, stage: str) -> None:
    run.stage = stage
    run.save(update_fields=["stage"])


def _finish(run: ReviewRun, status: str, *, reason: str = "") -> None:
    run.status = status
    run.status_reason = reason
    run.finished_at = timezone.now()
    if status == Status.COMPLETED:
        run.stage = Stage.DONE
    run.save(update_fields=["status", "status_reason", "finished_at", "stage", "error"])


def _skip(run: ReviewRun, reason: str, message: str, git: GitProvider | None = None) -> None:
    run.summary = message
    run.save(update_fields=["summary"])
    if git is not None:
        _set_stage(run, Stage.POST_COMMENTS)
        pr = run.pull_request
        review = git.post_review(
            pr.repository.full_name,
            pr.number,
            commit_sha=run.head_sha,
            body=render.skipped_body(message, dashboard_url(run)),
            comments=[],
        )
        run.provider_review_id, run.provider_review_url = review.id, review.html_url
        run.save(update_fields=["provider_review_id", "provider_review_url"])
    _finish(run, Status.SKIPPED, reason=reason)


# --- stages -------------------------------------------------------------------------------------


def _run(run: ReviewRun, *, git: GitProvider, llm: LLMProvider | None, log: Any) -> None:
    snap = run.settings_snapshot
    pr = run.pull_request
    repository = pr.repository
    credential = run.credential
    if credential is None or credential.status != LLMCredential.Status.VALID:
        raise StageFailure(
            "No valid LLM key is configured for this repository (the key was revoked or is invalid).",
            reason="no_credential",
        )

    # 1. Fetch
    _set_stage(run, Stage.FETCH_DIFF)
    try:
        info = git.get_pull_request(repository.full_name, pr.number)
        if info.state != "open":
            _skip(run, "pull_request_closed", "The pull request is no longer open.")
            return
        PullRequest.objects.filter(pk=pr.pk).update(
            head_sha=info.head_sha, title=info.title, is_draft=info.is_draft, state=info.state
        )
        if run.trigger == ReviewRun.Trigger.PUSH and info.head_sha != run.head_sha:
            # A newer push arrived; its own run reviews the latest commit.
            run.summary = "Superseded by a newer push."
            run.save(update_fields=["summary"])
            _finish(run, Status.CANCELLED, reason="superseded")
            return
        run.head_sha, run.base_sha = info.head_sha, info.base_sha
        run.save(update_fields=["head_sha", "base_sha"])
        _start_check(run, git, snap, log)
        snap = _apply_repo_config(run, git, snap, log)
        files = git.list_files(repository.full_name, pr.number, max_files=snap["max_files"] + 1)
    except GitProviderError as exc:
        _raise_git(exc)

    if budgets.is_exceeded(credential) and not _budget_override(run):
        message = (
            "Reviews are paused: the LLM key for this repository has reached its monthly budget. "
            "An admin can raise the budget, or start a review from the dashboard."
        )
        # Pushes would repeat the notice on every commit; the check run already shows it as skipped.
        _skip(run, "budget_exceeded", message, None if run.trigger == ReviewRun.Trigger.PUSH else git)
        return

    if len(files) > snap["max_files"]:
        _skip(
            run,
            "too_large",
            f"This pull request changes more than {snap['max_files']} files, the limit configured for this "
            "repository. It was not reviewed.",
            git,
        )
        return

    review_files = _incremental_files(run, git, files, log)

    # 2. Filter
    _set_stage(run, Stage.FILTER)
    diffs, ignored = _filter_files(review_files, snap)
    # Inline comments must target lines of the PR diff (base...head), even for incremental reviews.
    anchor_diffs = _filter_files(files, snap)[0] if run.incremental else diffs
    run.files_ignored = ignored
    run.files_reviewed = [
        {"path": d.path, "status": d.status, "additions": d.additions, "deletions": d.deletions}
        for d in diffs
    ]
    run.save(update_fields=["files_ignored", "files_reviewed"])
    if not diffs:
        run.summary = (
            f"No reviewable changes since {run.compare_base_sha[:7]}."
            if run.incremental
            else "No reviewable files (all changed files are ignored, deleted, or binary)."
        )
        run.save(update_fields=["summary"])
        _finish(run, Status.SKIPPED, reason="no_reviewable_files")
        return
    changed_lines = sum(d.additions + d.deletions for d in diffs)
    if changed_lines > snap["max_changed_lines"]:
        _skip(
            run,
            "too_large",
            f"This pull request changes {changed_lines} lines in reviewable files, above the limit of "
            f"{snap['max_changed_lines']} configured for this repository. It was not reviewed to bound cost.",
            git,
        )
        return

    # 3. Chunk
    _set_stage(run, Stage.CHUNK)
    pr_paths = [f.path for f in files]
    wants_tests = bool(snap.get("suggest_tests", True)) and needs_test_suggestions(pr_paths)
    system_prompt = prompts.build_system_prompt(
        snap.get("custom_instructions", ""),
        profile=get_profile(snap.get("profile")),
        extra_sections=[TEST_PROMPT] if wants_tests else None,
    )
    overhead = estimate_tokens(system_prompt) + 200
    budget = max(snap["chunk_tokens"] - overhead, 1_000)
    chunks = build_chunks(diffs, budget)
    estimated_input = sum(c.tokens + overhead for c in chunks)
    run.chunk_count = len(chunks)
    run.save(update_fields=["chunk_count"])
    if estimated_input > snap["max_input_tokens"]:
        _skip(
            run,
            "too_large",
            f"Reviewing this pull request would send about {estimated_input:,} tokens to the LLM, above the "
            f"limit of {snap['max_input_tokens']:,} configured for this repository.",
            git,
        )
        return

    # 4. LLM
    _set_stage(run, Stage.LLM)
    provider = llm or provider_for(credential, model=run.model or None)
    results = _review_chunks(provider, chunks, system_prompt, pr.title, snap.get("rules", []))
    _record_usage(run, credential, provider, results)
    try:
        budgets.check_thresholds(credential)
    except Exception:  # alerts must never fail a review
        log.exception("budget.check_failed")
    auth_errors = [r.error for r in results if isinstance(r.error, AuthenticationFailed)]
    if auth_errors:
        mark_invalid(credential, auth_errors[0])
        raise StageFailure(
            f"The LLM provider rejected the API key: {auth_errors[0]}", reason="llm_auth_failed"
        )
    failed = [r for r in results if r.review is None]
    if len(failed) == len(results):
        raise StageFailure(f"All LLM requests failed. Last error: {failed[-1].error}", reason="llm_failed")
    run.chunks_failed = len(failed)

    # 5. Aggregate
    _set_stage(run, Stage.AGGREGATE)
    by_path = {d.path: d for d in anchor_diffs}
    findings = _build_findings(run, results, by_path, snap)
    summaries = [r.review.summary for r in results if r.review and r.review.summary]
    run.summary = "\n\n".join(summaries)
    reported = [f.severity for f in findings if f.post_status in REPORTED_FOR_GATE]
    run.risk_score = compute_risk(
        reported, pr_paths, sum(d.additions + d.deletions for d in anchor_diffs)
    ).score
    run.save(update_fields=["summary", "chunks_failed", "risk_score"])

    # 6. Post
    _set_stage(run, Stage.POST_COMMENTS)
    try:
        _post(run, git, findings, summaries, ignored, len(failed), len(results), snap)
    except GitProviderError as exc:
        Finding.objects.filter(review_run=run).update(post_status=PostStatus.NOT_POSTED)
        _raise_git(exc)
    PullRequest.objects.filter(pk=pr.pk).update(last_reviewed_sha=run.head_sha)
    _finish(run, Status.COMPLETED)
    log.info("review.completed", findings=len(findings), chunks=len(chunks), failed_chunks=len(failed))


def _apply_repo_config(run: ReviewRun, git: GitProvider, snap: dict[str, Any], log: Any) -> dict[str, Any]:
    """Reads `.reviewbot.yml` from the PR's base commit (never the head) and merges it over the dashboard
    settings. Invalid files never block a review; the error is shown in the summary and the dashboard."""
    run.config_source, run.config_error = "dashboard", ""
    text = (
        git.get_file(run.pull_request.repository.full_name, CONFIG_PATH, run.base_sha)
        if run.base_sha
        else None
    )
    if text is not None:
        try:
            file_config = parse_config_file(text)
        except ConfigError as exc:
            run.config_error = f"{CONFIG_PATH}: {exc}"[:500]
            log.info("review.config_invalid", error=str(exc))
        else:
            snap = merge_config(snap, file_config)
            run.config_source = CONFIG_PATH
            if file_config.warnings:
                run.config_error = f"{CONFIG_PATH}: " + "; ".join(file_config.warnings)[:450]
            run.settings_snapshot = snap
    run.save(update_fields=["config_source", "config_error", "settings_snapshot"])
    return snap


def _incremental_files(
    run: ReviewRun, git: GitProvider, files: list[ChangedFile], log: Any
) -> list[ChangedFile]:
    """For push runs, narrows the review to files changed since the last reviewed commit (RE-10).

    Falls back to the full PR diff after a force push (base not an ancestor) or when the comparison
    is unavailable.
    """
    last = run.pull_request.last_reviewed_sha
    if run.trigger != ReviewRun.Trigger.PUSH or not last or last == run.head_sha:
        return files
    repo = run.pull_request.repository.full_name
    try:
        comparison = git.compare(repo, last, run.head_sha)
    except GitProviderError as exc:
        if exc.retryable:
            _raise_git(exc)
        log.info("review.compare_unavailable", error=str(exc))
        return files
    if comparison.status != "ahead":
        log.info("review.full_review_after_force_push", compare_status=comparison.status)
        return files
    pr_paths = {f.path for f in files}
    run.incremental = True
    run.compare_base_sha = last
    run.save(update_fields=["incremental", "compare_base_sha"])
    return [f for f in comparison.files if f.path in pr_paths]


# --- check runs (INT-02) ------------------------------------------------------------------------

CHECK_NAME = "Reviewbot"
REPORTED_FOR_GATE = (PostStatus.POSTED, PostStatus.IN_SUMMARY, PostStatus.CAP_EXCEEDED, PostStatus.DUPLICATE)


def _start_check(run: ReviewRun, git: GitProvider, snap: dict[str, Any], log: Any) -> None:
    if not snap.get("check_runs", True) or run.check_run_id:
        return
    try:
        run.check_run_id = git.create_check_run(
            run.pull_request.repository.full_name,
            run.head_sha,
            name=CHECK_NAME,
            details_url=dashboard_url(run),
        )
    except GitProviderError as exc:
        if exc.status in (403, 404):
            log.warning("github.checks_permission_missing", error=str(exc))
        else:
            log.warning("github.check_run_create_failed", error=str(exc))
        return
    run.save(update_fields=["check_run_id"])


def check_outcome(run: ReviewRun) -> tuple[str, str, str]:
    """Returns (conclusion, title, summary) for a finished run. Never fails a check because of our outage."""
    link = f"[Open the review in Reviewbot]({dashboard_url(run)})"
    if run.status == Status.COMPLETED:
        reported = list(
            run.findings.filter(post_status__in=REPORTED_FOR_GATE).values_list("severity", flat=True)
        )
        counts = render.counts_line({s: reported.count(s) for s in render.SEVERITY_ORDER})
        risk = f" · risk {run.risk_score}/100" if run.risk_score is not None else ""
        summary = f"{counts}{risk}\n\n{link}"
        if not reported:
            return "success", "No issues found", summary
        gate = run.settings_snapshot.get("gate_severity")
        if gate and any(SEVERITY_RANK[s] >= SEVERITY_RANK[gate] for s in reported):
            blocking = sum(1 for s in reported if SEVERITY_RANK[s] >= SEVERITY_RANK[gate])
            return "failure", f"{blocking} finding(s) at or above {gate}", summary
        return "neutral", f"{len(reported)} finding(s)", summary
    if run.status == Status.CANCELLED:
        return "skipped", "Superseded by a newer push", link
    if run.status == Status.SKIPPED:
        return "skipped", "Review skipped", f"{run.summary}\n\n{link}"
    return "neutral", "Review failed", f"The review could not be completed: {run.error}\n\n{link}"


def _complete_check(run: ReviewRun, git: GitProvider | None, log: Any) -> None:
    if not run.check_run_id or git is None or run.status not in ReviewRun.TERMINAL:
        return
    conclusion, title, summary = check_outcome(run)
    try:
        git.complete_check_run(
            run.pull_request.repository.full_name,
            run.check_run_id,
            conclusion=conclusion,
            title=title,
            summary=summary,
        )
    except GitProviderError as exc:
        log.warning("github.check_run_update_failed", error=str(exc))


def _raise_git(exc: GitProviderError) -> None:
    if exc.retryable:
        raise RetryLater(exc.retry_after or 60, str(exc)) from exc
    if exc.status in (401, 403, 404):
        raise StageFailure(
            f"GitHub denied access ({exc.status}). Check that the GitHub App is still installed on this "
            f"repository. Details: {exc}",
            reason="git_access_denied",
        ) from exc
    raise StageFailure(f"GitHub API error: {exc}", reason="git_error") from exc


def _filter_files(
    files: list[ChangedFile], snap: dict[str, Any]
) -> tuple[list[FileDiff], list[dict[str, Any]]]:
    matcher = IgnoreMatcher(
        snap.get("ignore_patterns", []), include_defaults=not snap.get("replace_default_ignores")
    )
    diffs: list[FileDiff] = []
    ignored: list[dict[str, Any]] = []
    for f in files:
        if f.status == "removed":
            ignored.append({"path": f.path, "reason": "deleted", "pattern": None})
            continue
        decision = matcher.check(f.path)
        if decision.ignored:
            ignored.append({"path": f.path, "reason": "ignore_pattern", "pattern": decision.pattern})
            continue
        if not f.patch:
            reason = "no_textual_changes" if f.additions == 0 and f.deletions == 0 else "binary_or_too_large"
            ignored.append({"path": f.path, "reason": reason, "pattern": None})
            continue
        hunks = parse_patch(f.patch)
        if not hunks:
            ignored.append({"path": f.path, "reason": "no_textual_changes", "pattern": None})
            continue
        diffs.append(
            FileDiff(
                path=f.path,
                status=f.status,
                hunks=hunks,
                additions=f.additions,
                deletions=f.deletions,
                previous_path=f.previous_path,
            )
        )
    return diffs, ignored


def _review_chunks(
    provider: LLMProvider,
    chunks: list[Chunk],
    system_prompt: str,
    title: str,
    rules: list[dict[str, Any]] | None = None,
) -> list[ChunkResult]:
    def work(chunk: Chunk) -> ChunkResult:
        result = ChunkResult(chunk=chunk)
        user_prompt = prompts.build_user_prompt(
            pr_title=title,
            chunk_text=chunk.text,
            chunk_index=chunk.index,
            chunk_count=len(chunks),
            rules_text=render_rules(applicable_rules(rules or [], chunk.paths)),
        )
        prompt = user_prompt
        for attempt in range(2):  # one repair retry for malformed output
            started = time.monotonic()
            try:
                response = provider.complete_json(
                    system=system_prompt,
                    user=prompt,
                    schema=REVIEW_SCHEMA,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                )
            except LLMError as exc:
                result.calls.append((exc.usage, int((time.monotonic() - started) * 1000), exc.code))
                if isinstance(exc, InvalidResponse) and attempt == 0:
                    prompt = prompts.repair_prompt(user_prompt, str(exc))
                    continue
                result.error = exc
                return result
            result.calls.append((response.usage, response.latency_ms, ""))
            try:
                result.review = parse_review(response.data)
                return result
            except SchemaError as exc:
                if attempt == 0:
                    prompt = prompts.repair_prompt(user_prompt, str(exc))
                    continue
                result.error = InvalidResponse(f"Output did not match the schema: {exc}")
        return result

    if len(chunks) == 1:
        return [work(chunks[0])]
    with ThreadPoolExecutor(max_workers=min(LLM_CONCURRENCY, len(chunks))) as pool:
        return list(pool.map(work, chunks))


def _record_usage(
    run: ReviewRun, credential: LLMCredential, provider: LLMProvider, results: list[ChunkResult]
) -> None:
    price = pricing.price_for(credential.provider, provider.model)
    rows = []
    for result in results:
        for usage, latency_ms, error_code in result.calls:
            rows.append(
                LLMUsage(
                    review_run=run,
                    credential=credential,
                    repository=run.pull_request.repository,
                    provider=credential.provider,
                    model=provider.model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=pricing.cost(price, usage.input_tokens, usage.output_tokens),
                    latency_ms=latency_ms,
                    status=LLMUsage.Status.ERROR if error_code else LLMUsage.Status.OK,
                    error_code=error_code,
                )
            )
    LLMUsage.objects.bulk_create(rows)
    costs = [r.cost_usd for r in rows]
    priced = [c for c in costs if c is not None]
    run.cost_usd = sum(priced, Decimal(0)) if costs and len(priced) == len(costs) else None
    run.input_tokens = sum(r.input_tokens for r in rows)
    run.output_tokens = sum(r.output_tokens for r in rows)
    run.save(update_fields=["input_tokens", "output_tokens", "cost_usd"])


def _budget_override(run: ReviewRun) -> bool:
    """An admin starting a review from the dashboard may exceed the budget; nothing else may."""
    user = run.created_by
    return run.trigger == ReviewRun.Trigger.MANUAL and user is not None and user.is_admin


def _anchor(raw: RawFinding, diff: FileDiff | None) -> tuple[bool, int | None, int | None]:
    """Returns (anchored, line_start, line_end) where anchored means GitHub accepts an inline comment."""
    if diff is None:
        return False, raw.line_start, raw.line_end
    commentable = diff.commentable_lines
    if raw.line_end in commentable:
        start = raw.line_start if raw.line_start in commentable else raw.line_end
        # A multi-line comment range must be one contiguous run of diff lines.
        if any(n not in commentable for n in range(start, raw.line_end + 1)):
            start = raw.line_end
        return True, start, raw.line_end
    if raw.line_start in commentable:
        return True, raw.line_start, raw.line_start
    return False, raw.line_start, raw.line_end


def _build_findings(
    run: ReviewRun, results: list[ChunkResult], by_path: dict[str, FileDiff], snap: dict[str, Any]
) -> list[Finding]:
    previous = set(
        Finding.objects.filter(
            review_run__pull_request=run.pull_request,
            post_status__in=[PostStatus.POSTED, PostStatus.IN_SUMMARY],
        )
        .exclude(review_run=run)
        .values_list("fingerprint", flat=True)
    )
    dismissed = set(
        Finding.objects.filter(review_run__pull_request=run.pull_request, state=Finding.State.DISMISSED)
        .exclude(review_run=run)
        .values_list("fingerprint", flat=True)
    )
    suggest_tests = bool(snap.get("suggest_tests", True))
    min_rank = SEVERITY_RANK[snap.get("min_severity", "low")]
    allowed_categories = get_profile(snap.get("profile")).allowed_categories
    rules = {r["id"]: r for r in snap.get("rules", []) if r.get("enabled", True)}
    min_confidence = float(snap.get("min_confidence", 0.5))
    seen: set[str] = set()
    findings: list[Finding] = []
    for result in results:
        if result.review is None:
            continue
        for raw in result.review.findings:
            diff = by_path.get(raw.path)
            rule = rules.get(raw.rule_id)
            severity = raw.severity
            if rule and SEVERITY_RANK[rule["severity"]] > SEVERITY_RANK[severity]:
                severity = rule["severity"]
            if raw.category == "test" and SEVERITY_RANK[severity] > SEVERITY_RANK[MAX_TEST_SEVERITY]:
                severity = MAX_TEST_SEVERITY
            anchored, start, end = _anchor(raw, diff)
            context = diff.context_around(end or raw.line_end) if diff else ""
            fp = fingerprint(raw.path, raw.category, raw.title, context)
            finding = Finding(
                review_run=run,
                fingerprint=fp,
                path=raw.path,
                line_start=start,
                line_end=end,
                anchored=anchored,
                category=raw.category,
                severity=severity,
                rule_id=raw.rule_id if rule else "",
                confidence=raw.confidence,
                title=raw.title,
                body=raw.body,
                suggestion=raw.suggestion,
            )
            if fp in dismissed:
                finding.post_status = PostStatus.DISMISSED_EARLIER
                finding.state = Finding.State.DISMISSED
            elif not _category_allowed(raw.category, allowed_categories, suggest_tests):
                finding.post_status = PostStatus.CATEGORY_FILTERED
            elif SEVERITY_RANK[severity] < min_rank:
                finding.post_status = PostStatus.BELOW_THRESHOLD
            elif raw.confidence < min_confidence:
                finding.post_status = PostStatus.LOW_CONFIDENCE
            elif fp in previous or fp in seen:
                finding.post_status = PostStatus.DUPLICATE
            seen.add(fp)
            findings.append(finding)

    candidates = [f for f in findings if f.post_status == PostStatus.NOT_POSTED]
    candidates.sort(key=lambda f: (-SEVERITY_RANK[f.severity], -f.confidence, f.path, f.line_start or 0))
    cap = int(snap.get("max_inline_comments", 25))
    inline = 0
    for f in candidates:
        if not f.anchored:
            f.post_status = PostStatus.IN_SUMMARY
        elif inline < cap:
            f.post_status = PostStatus.POSTED
            inline += 1
        else:
            f.post_status = PostStatus.CAP_EXCEEDED
    Finding.objects.bulk_create(findings)
    return findings


def _category_allowed(category: str, allowed: frozenset[str] | None, suggest_tests: bool) -> bool:
    if category == "test" and not suggest_tests:
        return False
    return allowed is None or category in allowed


def _post(
    run: ReviewRun,
    git: GitProvider,
    findings: list[Finding],
    summaries: list[str],
    ignored: list[dict[str, Any]],
    failed_chunks: int,
    total_chunks: int,
    snap: dict[str, Any],
) -> None:
    pr = run.pull_request
    inline = sorted(
        (f for f in findings if f.post_status == PostStatus.POSTED),
        key=lambda f: (-SEVERITY_RANK[f.severity], -f.confidence, f.path, f.line_start or 0),
    )
    in_summary = [f for f in findings if f.post_status == PostStatus.IN_SUMMARY]
    hidden = [f for f in findings if f.post_status == PostStatus.CAP_EXCEEDED]
    if (
        not inline
        and not in_summary
        and not hidden
        and not failed_chunks
        and not snap.get("post_when_no_findings", True)
    ):
        return

    def body(listed: list[Finding]) -> str:
        reported = [f for f in findings if f.post_status in (PostStatus.POSTED, PostStatus.IN_SUMMARY)]
        return render.summary_body(
            render.SummaryContext(
                summaries=summaries,
                reported=reported + hidden,
                in_summary=listed,
                hidden_count=len(hidden),
                skipped_files=[(i["path"], i["pattern"] or i["reason"]) for i in ignored],
                failed_chunks=failed_chunks,
                total_chunks=total_chunks,
                model=run.model,
                head_sha=run.head_sha,
                dashboard_url=dashboard_url(run),
                risk_score=run.risk_score,
                scope_note=" ".join(
                    note
                    for note in (
                        f"Incremental review of changes since {run.compare_base_sha[:7]}."
                        if run.incremental
                        else "",
                        f"Configuration problem: {run.config_error}." if run.config_error else "",
                    )
                    if note
                ),
            )
        )

    comments = [
        InlineComment(
            path=f.path,
            line=f.line_end or 0,
            start_line=f.line_start if f.line_start and f.line_start != f.line_end else None,
            body=render.inline_comment_body(f, with_suggestion=True),
        )
        for f in inline
    ]
    try:
        review = git.post_review(
            pr.repository.full_name,
            pr.number,
            commit_sha=run.head_sha,
            body=body(in_summary),
            comments=comments,
        )
    except InlineCommentsRejected:
        for f in inline:
            f.post_status = PostStatus.IN_SUMMARY
        Finding.objects.filter(pk__in=[f.pk for f in inline]).update(post_status=PostStatus.IN_SUMMARY)
        review = git.post_review(
            pr.repository.full_name,
            pr.number,
            commit_sha=run.head_sha,
            body=body(in_summary + inline),
            comments=[],
        )
        inline = []
    run.provider_review_id, run.provider_review_url = review.id, review.html_url
    run.save(update_fields=["provider_review_id", "provider_review_url"])
    for f in inline:
        found = review.comments.get((f.path, f.line_end or 0))
        if found:
            f.provider_comment_id, f.provider_comment_url = found
            f.save(update_fields=["provider_comment_id", "provider_comment_url"])
