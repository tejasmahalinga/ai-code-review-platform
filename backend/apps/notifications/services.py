"""Turns review, budget and digest events into notifications for the subscribed channels."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.core.logging import get_logger
from apps.notifications.messages import Message
from apps.notifications.models import Event, NotificationChannel, NotificationDelivery
from apps.repositories.models import SEVERITY_RANK

if TYPE_CHECKING:
    from apps.credentials.budgets import BudgetStatus
    from apps.credentials.models import LLMCredential
    from apps.reviews.models import ReviewRun

logger = get_logger(__name__)

REPORTED = ("posted", "in_summary", "cap_exceeded", "duplicate")


def dispatch(
    event: str,
    message: Message,
    *,
    dedup_key: str,
    repository_id: int | None = None,
    accept: Callable[[NotificationChannel], bool] | None = None,
) -> int:
    """Queues ``message`` for every enabled channel subscribed to ``event``. Returns how many were queued."""
    from apps.notifications.tasks import deliver

    channels = NotificationChannel.objects.filter(enabled=True, events__contains=[event]).prefetch_related(
        "repositories"
    )
    queued = 0
    for channel in channels:
        repo_ids = {r.pk for r in channel.repositories.all()}
        if repo_ids and repository_id not in repo_ids:
            continue
        if accept is not None and not accept(channel):
            continue
        try:
            with transaction.atomic():
                delivery = NotificationDelivery.objects.create(
                    channel=channel, event=event, dedup_key=dedup_key, title=message.title[:300]
                )
        except IntegrityError:
            continue  # already notified
        transaction.on_commit(partial(deliver.delay, delivery.pk, message.as_dict()))
        queued += 1
    return queued


# --- reviews -------------------------------------------------------------------------------------


def _review_url(run: ReviewRun) -> str:
    return f"{settings.PUBLIC_URL}/reviews/{run.pk}"


def on_review_finished(run: ReviewRun) -> int:
    """Notifies about failed reviews and high-risk results. Called once per finished review run."""
    pr = run.pull_request
    repo = pr.repository
    pr_label = f"{repo.full_name}#{pr.number}"
    if run.status == "failed":
        message = Message(
            event=Event.REVIEW_FAILED,
            title=f"Review failed: {pr_label} {pr.title}",
            text=run.error or run.status_reason,
            url=_review_url(run),
            level="warning",
            fields=[("Reason", run.status_reason or "unknown"), ("Trigger", run.trigger)],
            data={
                "repository": repo.full_name,
                "number": pr.number,
                "review_id": run.pk,
                "reason": run.status_reason,
            },
        )
        return dispatch(Event.REVIEW_FAILED, message, dedup_key=f"run:{run.pk}", repository_id=repo.pk)
    if run.status != "completed":
        return 0

    counts = dict(
        run.findings.filter(post_status__in=REPORTED)
        .values_list("severity")
        .annotate(n=Count("id"))
        .values_list("severity", "n")
    )
    top_rank = max((SEVERITY_RANK[s] for s in counts), default=-1)
    risk = run.risk_score or 0

    def high_risk_for(channel: NotificationChannel) -> bool:
        if risk >= channel.min_risk:
            return True
        return bool(channel.min_severity) and top_rank >= SEVERITY_RANK.get(channel.min_severity, 99)

    from apps.reviews.engine.risk import risk_bucket

    summary = ", ".join(f"{counts[s]} {s}" for s in SEVERITY_RANK if counts.get(s)) or "no findings"
    message = Message(
        event=Event.HIGH_RISK,
        title=f"High-risk pull request: {pr_label} {pr.title}",
        text=f"Risk {risk}/100 ({risk_bucket(run.risk_score)}). Findings: {summary}.",
        url=_review_url(run),
        level="danger",
        fields=[
            ("Risk", f"{risk}/100"),
            ("Findings", summary),
            ("Author", pr.author_login or "unknown"),
            ("Pull request", pr.html_url or pr_label),
        ],
        data={
            "repository": repo.full_name,
            "number": pr.number,
            "title": pr.title,
            "author": pr.author_login,
            "pull_request_url": pr.html_url,
            "review_id": run.pk,
            "risk_score": run.risk_score,
            "findings": counts,
        },
    )
    return dispatch(
        Event.HIGH_RISK, message, dedup_key=f"run:{run.pk}", repository_id=repo.pk, accept=high_risk_for
    )


# --- budgets -------------------------------------------------------------------------------------


def on_budget_threshold(credential: LLMCredential, threshold: int, status: BudgetStatus, month: str) -> int:
    paused = threshold >= 100
    message = Message(
        event=Event.BUDGET,
        title=f'LLM key "{credential.name}" reached {threshold}% of its monthly budget',
        text=(
            "Automatic reviews with this key are paused until next month or until the budget is raised."
            if paused
            else "Automatic reviews pause at 100%."
        ),
        url=f"{settings.PUBLIC_URL}/settings/usage",
        level="danger" if paused else "warning",
        fields=[
            ("Spent", f"${status.spent_usd:.2f}"),
            ("Budget", f"${status.budget_usd:.2f}" if status.budget_usd is not None else "none"),
        ],
        data={
            "credential_id": credential.pk,
            "credential": credential.name,
            "threshold": threshold,
            "spent_usd": str(status.spent_usd),
            "budget_usd": str(status.budget_usd),
        },
    )
    return dispatch(Event.BUDGET, message, dedup_key=f"budget:{credential.pk}:{month}:{threshold}")


# --- weekly digest -------------------------------------------------------------------------------


def build_digest(end: datetime | None = None) -> Message:
    from apps.reviews.models import Finding, LLMUsage, PullRequest, ReviewRun

    end = end or timezone.now()
    start = end - timedelta(days=7)
    runs = ReviewRun.objects.filter(created_at__gte=start, created_at__lt=end)
    completed = runs.filter(status="completed")
    findings = Finding.objects.filter(review_run__in=completed, post_status__in=REPORTED)
    by_severity = dict(findings.values_list("severity").annotate(n=Count("id")).values_list("severity", "n"))
    triaged = Finding.objects.filter(state_changed_at__gte=start, state_changed_at__lt=end)
    accepted = triaged.filter(state="accepted").count()
    dismissed = triaged.filter(state="dismissed").count()
    cost = LLMUsage.objects.filter(created_at__gte=start, created_at__lt=end).aggregate(c=Sum("cost_usd"))[
        "c"
    ]
    failed = runs.filter(status="failed").count()

    risky = (
        completed.filter(risk_score__isnull=False)
        .select_related("pull_request__repository")
        .order_by("-risk_score", "-id")
    )
    lines: list[str] = []
    seen: set[int] = set()
    for run in risky:
        if run.pull_request_id in seen:
            continue
        seen.add(run.pull_request_id)
        pr = run.pull_request
        lines.append(f"{run.risk_score}/100 {pr.repository.full_name}#{pr.number} {pr.title}")
        if len(lines) == 5:
            break

    severity_text = ", ".join(f"{by_severity[s]} {s}" for s in SEVERITY_RANK if by_severity.get(s)) or "none"
    acceptance = f"{accepted / (accepted + dismissed):.0%}" if accepted + dismissed else "n/a"
    prs = PullRequest.objects.filter(review_runs__in=completed).distinct().count()
    period = f"{start:%b %d} to {end:%b %d}"
    return Message(
        event=Event.DIGEST,
        title=f"Reviewbot weekly digest ({period})",
        text="Riskiest pull requests this week:" if lines else "No completed reviews this week.",
        url=f"{settings.PUBLIC_URL}/pull-requests",
        fields=[
            (
                "Reviews",
                f"{completed.count()} on {prs} pull requests" + (f" ({failed} failed)" if failed else ""),
            ),
            ("Findings", severity_text),
            ("Triage", f"{accepted} accepted, {dismissed} dismissed (acceptance {acceptance})"),
            ("LLM cost", f"${(cost or Decimal(0)):.2f}"),
        ],
        lines=lines,
        data={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "reviews": completed.count(),
            "failed": failed,
            "pull_requests": prs,
            "findings": by_severity,
            "accepted": accepted,
            "dismissed": dismissed,
            "cost_usd": str(cost or 0),
        },
    )


def send_weekly_digest(now: datetime | None = None) -> int:
    now = now or timezone.now()
    week = now.isocalendar()
    return dispatch(Event.DIGEST, build_digest(now), dedup_key=f"digest:{week.year}-W{week.week:02d}")


def has_channels() -> bool:
    return NotificationChannel.objects.filter(enabled=True).exists()


__all__ = ["dispatch", "on_budget_threshold", "on_review_finished", "send_weekly_digest"]
