"""Monthly budgets per LLM key: spend tracking, threshold alerts, and pausing reviews (KEY-05)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.audit.services import record
from apps.core.logging import get_logger
from apps.credentials.models import BudgetAlert, LLMCredential

logger = get_logger(__name__)

THRESHOLDS = (80, 100)


def month_start(now: datetime | None = None) -> datetime:
    now = now or timezone.now()
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class BudgetStatus:
    budget_usd: Decimal | None
    spent_usd: Decimal
    unpriced_requests: int

    @property
    def percent(self) -> float | None:
        if not self.budget_usd:
            return None
        return float(self.spent_usd / self.budget_usd * 100)

    @property
    def state(self) -> str:
        percent = self.percent
        if percent is None:
            return "none"
        if percent >= 100:
            return "exceeded"
        if percent >= THRESHOLDS[0]:
            return "warning"
        return "ok"

    def as_dict(self) -> dict[str, object]:
        return {
            # Strings, like every other money amount in the API (DRF serializes decimals as strings).
            "budget_usd": str(self.budget_usd) if self.budget_usd is not None else None,
            "spent_usd": str(self.spent_usd.quantize(Decimal("0.01"))),
            "percent": round(self.percent, 1) if self.percent is not None else None,
            "state": self.state,
            "unpriced_requests": self.unpriced_requests,
        }


def status(credential: LLMCredential, now: datetime | None = None) -> BudgetStatus:
    totals = credential.usage.filter(created_at__gte=month_start(now)).aggregate(
        spent=Sum("cost_usd"), unpriced=Count("id", filter=Q(cost_usd__isnull=True))
    )
    return BudgetStatus(
        budget_usd=credential.monthly_budget_usd,
        spent_usd=totals["spent"] or Decimal(0),
        unpriced_requests=totals["unpriced"] or 0,
    )


def is_exceeded(credential: LLMCredential) -> bool:
    return credential.monthly_budget_usd is not None and status(credential).state == "exceeded"


def check_thresholds(credential: LLMCredential) -> list[int]:
    """Records (once per month) each budget threshold the key has crossed and notifies admins."""
    current = status(credential)
    percent = current.percent
    if percent is None or current.budget_usd is None:
        return []
    month: date = month_start().date()
    fired: list[int] = []
    for threshold in THRESHOLDS:
        if percent < threshold:
            continue
        try:
            with transaction.atomic():
                BudgetAlert.objects.create(
                    credential=credential,
                    month=month,
                    threshold=threshold,
                    spent_usd=current.spent_usd,
                    budget_usd=current.budget_usd,
                )
        except IntegrityError:
            continue  # already alerted this month
        fired.append(threshold)
        record(
            "budget.threshold_reached",
            target=credential,
            threshold=threshold,
            spent_usd=str(current.spent_usd.quantize(Decimal("0.01"))),
            budget_usd=str(current.budget_usd),
        )
        logger.warning("budget.threshold_reached", credential_id=credential.pk, threshold=threshold)
        _notify_admins(credential, threshold, current)
    return fired


def _notify_admins(credential: LLMCredential, threshold: int, current: BudgetStatus) -> None:
    if not settings.EMAIL_ENABLED:
        return
    from apps.accounts.models import User

    recipients = list(
        User.objects.filter(role=User.Role.ADMIN, is_active=True).values_list("email", flat=True)
    )
    if not recipients:
        return
    if threshold >= 100:
        consequence = (
            "Automatic reviews using this key are paused until next month or until the budget is raised."
        )
    else:
        consequence = "Automatic reviews pause when it reaches 100%."
    try:
        send_mail(
            subject=f'Reviewbot: LLM key "{credential.name}" reached {threshold}% of its monthly budget',
            message=(
                f'The LLM key "{credential.name}" has used ${current.spent_usd:.2f} of its '
                f"${current.budget_usd:.2f} monthly budget.\n\n{consequence}\n\n"
                f"Usage: {settings.PUBLIC_URL}/settings/usage"
            ),
            from_email=None,
            recipient_list=recipients,
        )
    except Exception as exc:  # never fail a review because of SMTP
        logger.warning("budget.email_failed", error=type(exc).__name__)
