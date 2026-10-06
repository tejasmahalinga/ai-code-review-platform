from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from django.db.models import Count, Exists, OuterRef, Q, QuerySet, Subquery, Sum
from django.db.models.functions import TruncDay
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.permissions import IsAdmin, IsReviewerOrReadOnly
from apps.audit.services import record
from apps.core.exceptions import Conflict
from apps.repositories.models import SEVERITY_RANK
from apps.reviews.engine.profiles import PROFILES
from apps.reviews.models import Finding, FindingFeedback, LLMUsage, PullRequest, ReviewRun
from apps.reviews.serializers import (
    REPORTED_STATUSES,
    SEVERITIES,
    FeedbackVoteSerializer,
    FindingSerializer,
    FindingStateSerializer,
    PullRequestSerializer,
    ReviewRunSerializer,
    ReviewRunSummarySerializer,
)
from apps.reviews.services import ActiveRunExists, request_manual_review


def with_counts(qs: QuerySet[ReviewRun]) -> QuerySet[ReviewRun]:
    reported = Q(findings__post_status__in=REPORTED_STATUSES)
    annotations: dict[str, Any] = {
        f"count_{s}": Count("findings", filter=reported & Q(findings__severity=s)) for s in SEVERITIES
    }
    annotations["count_posted"] = Count("findings", filter=Q(findings__post_status=Finding.PostStatus.POSTED))
    return qs.annotate(**annotations)


class PullRequestPagination(CursorPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
    ordering = ("-updated_at", "-id")


class PullRequestViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet[PullRequest]
):
    serializer_class = PullRequestSerializer
    permission_classes = [IsReviewerOrReadOnly]
    pagination_class = PullRequestPagination

    def get_queryset(self) -> QuerySet[PullRequest]:
        latest = ReviewRun.objects.filter(pull_request=OuterRef("pk")).order_by("-created_at", "-id")
        qs = PullRequest.objects.select_related("repository").annotate(
            latest_status=Subquery(latest.values("status")[:1]),
            latest_run_id=Subquery(latest.values("id")[:1]),
            latest_risk=Subquery(latest.values("risk_score")[:1]),
        )
        params = self.request.query_params
        if repo := params.get("repository"):
            if not repo.isdigit():
                raise ValidationError({"repository": "Must be a repository id."})
            qs = qs.filter(repository_id=int(repo))
        if state := params.get("state"):
            qs = qs.filter(state__in=state.split(","))
        if review_status := params.get("review_status"):
            statuses = review_status.split(",")
            if "none" in statuses:
                qs = qs.filter(Q(latest_status__in=statuses) | Q(latest_status__isnull=True))
            else:
                qs = qs.filter(latest_status__in=statuses)
        if min_severity := params.get("min_severity"):
            if min_severity not in SEVERITY_RANK:
                raise ValidationError({"min_severity": f"Choose from: {', '.join(SEVERITIES)}"})
            severe = [s for s, rank in SEVERITY_RANK.items() if rank >= SEVERITY_RANK[min_severity]]
            qs = qs.filter(
                Exists(
                    Finding.objects.filter(
                        review_run_id=OuterRef("latest_run_id"),
                        severity__in=severe,
                        post_status__in=REPORTED_STATUSES,
                    )
                )
            )
        if risk := params.get("risk"):
            buckets = {"low": (0, 29), "medium": (30, 59), "high": (60, 100)}
            if risk not in buckets:
                raise ValidationError({"risk": "Choose from: low, medium, high"})
            qs = qs.filter(latest_risk__range=buckets[risk])
        if q := params.get("q"):
            matching_findings = Finding.objects.filter(
                review_run__pull_request=OuterRef("pk"), title__icontains=q
            )
            qs = qs.filter(Q(title__icontains=q) | Q(author_login__iexact=q) | Exists(matching_findings))
        return qs

    def _latest_runs(self, prs: list[PullRequest]) -> dict[int, ReviewRun]:
        ids = [pr.pk for pr in prs]
        latest_ids = Subquery(
            ReviewRun.objects.filter(pull_request=OuterRef("pull_request"))
            .order_by("-created_at", "-id")
            .values("id")[:1]
        )
        runs = with_counts(ReviewRun.objects.filter(pull_request_id__in=ids, id=latest_ids))
        return {run.pull_request_id: run for run in runs}

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        assert page is not None
        serializer = self.get_serializer(
            page, many=True, context={**self.get_serializer_context(), "latest_runs": self._latest_runs(page)}
        )
        return self.get_paginated_response(serializer.data)

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        pr = self.get_object()
        context = {**self.get_serializer_context(), "latest_runs": self._latest_runs([pr])}
        return Response(self.get_serializer(pr, context=context).data)

    @extend_schema(
        request=None, responses={200: ReviewRunSummarySerializer(many=True), 201: ReviewRunSummarySerializer}
    )
    @action(detail=True, methods=["get", "post"])
    def reviews(self, request: Request, pk: str | None = None) -> Response:
        pr = self.get_object()
        if request.method == "GET":
            runs = with_counts(pr.review_runs.all()).order_by("-created_at", "-id")[:100]
            return Response(ReviewRunSummarySerializer(runs, many=True).data)
        repository = pr.repository
        if not repository.is_reviewable:
            raise ValidationError(
                "Reviews are disabled for this repository. Enable it on the Repositories page."
            )
        settings = getattr(repository, "settings", None)
        if not (settings and settings.credential and settings.credential.is_usable):
            raise ValidationError("This repository has no valid LLM key configured.")
        try:
            run = request_manual_review(pr, request.user)
        except ActiveRunExists as exc:
            raise Conflict("A review for this pull request is already queued or running.") from exc
        record("review.requested", request=request, target=pr, review_id=run.pk)
        return Response(
            ReviewRunSummarySerializer(with_counts(ReviewRun.objects.filter(pk=run.pk)).get()).data,
            status=status.HTTP_201_CREATED,
        )


class ReviewRunViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet[ReviewRun]):
    serializer_class = ReviewRunSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self) -> QuerySet[ReviewRun]:
        return ReviewRun.objects.select_related(
            "pull_request__repository", "credential", "created_by"
        ).prefetch_related("findings__feedback")

    @action(detail=True, methods=["get"])
    def compare(self, request: Request, pk: str | None = None) -> Response:
        """Findings added and resolved between this run and an earlier run of the same PR (by fingerprint)."""
        run = self.get_object()
        other_id = request.query_params.get("with", "")
        if not other_id.isdigit():
            raise ValidationError({"with": "Pass the id of another review of the same pull request."})
        other = ReviewRun.objects.filter(pk=int(other_id), pull_request_id=run.pull_request_id).first()
        if other is None:
            raise ValidationError({"with": "Review not found for this pull request."})

        def reported(r: ReviewRun) -> dict[str, Finding]:
            # Dismissed findings still exist in the code, so they count as present (not "resolved").
            statuses = [*REPORTED_STATUSES, Finding.PostStatus.DISMISSED_EARLIER]
            qs = r.findings.filter(post_status__in=statuses).prefetch_related("feedback")
            return {f.fingerprint: f for f in qs}

        current, previous = reported(run), reported(other)
        context = self.get_serializer_context()
        return Response(
            {
                "run": run.pk,
                "with": other.pk,
                "added": FindingSerializer(
                    [f for fp, f in current.items() if fp not in previous], many=True, context=context
                ).data,
                "resolved": FindingSerializer(
                    [f for fp, f in previous.items() if fp not in current], many=True, context=context
                ).data,
                "unchanged": len(current.keys() & previous.keys()),
            }
        )


class FindingViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet[Finding]):
    """Accept/dismiss findings and vote on them (RE-16)."""

    serializer_class = FindingSerializer
    permission_classes = [IsReviewerOrReadOnly]
    http_method_names = ["get", "patch", "put", "delete"]

    def get_queryset(self) -> QuerySet[Finding]:
        return Finding.objects.select_related("review_run").prefetch_related("feedback")

    @extend_schema(request=FindingStateSerializer, responses={200: FindingSerializer})
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        finding = self.get_object()
        serializer = FindingStateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        previous = finding.state
        finding.state = serializer.validated_data["state"]
        finding.dismiss_reason = serializer.validated_data["dismiss_reason"]
        finding.state_changed_by = cast(User, request.user)
        finding.state_changed_at = timezone.now()
        finding.save(update_fields=["state", "dismiss_reason", "state_changed_by", "state_changed_at"])
        if previous != finding.state:
            record(
                "finding.state_changed",
                request=request,
                target=finding,
                previous=previous,
                state=finding.state,
                reason=finding.dismiss_reason,
            )
        return Response(self.get_serializer(finding).data)

    @extend_schema(request=FeedbackVoteSerializer, responses={200: FindingSerializer})
    @action(detail=True, methods=["put", "delete"])
    def feedback(self, request: Request, pk: str | None = None) -> Response:
        finding = self.get_object()
        user = cast(User, request.user)
        if request.method == "DELETE":
            FindingFeedback.objects.filter(finding=finding, user=user).delete()
        else:
            serializer = FeedbackVoteSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            FindingFeedback.objects.update_or_create(
                finding=finding, user=user, defaults={"vote": serializer.validated_data["vote"]}
            )
        finding = self.get_queryset().get(pk=finding.pk)
        return Response(self.get_serializer(finding).data)


class FeedbackStatsView(APIView):
    """Acceptance and helpfulness of findings by category (RE-16 analytics)."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        qs = Finding.objects.filter(post_status__in=REPORTED_STATUSES)
        if repo := request.query_params.get("repository"):
            if not repo.isdigit():
                raise ValidationError({"repository": "Must be a repository id."})
            qs = qs.filter(review_run__pull_request__repository_id=int(repo))
        rows = (
            qs.values("category")
            .annotate(
                reported=Count("id", distinct=True),
                accepted=Count("id", filter=Q(state=Finding.State.ACCEPTED), distinct=True),
                dismissed=Count("id", filter=Q(state=Finding.State.DISMISSED), distinct=True),
                false_positive=Count(
                    "id", filter=Q(dismiss_reason=Finding.DismissReason.FALSE_POSITIVE), distinct=True
                ),
                up=Count("feedback", filter=Q(feedback__vote=FindingFeedback.Vote.UP)),
                down=Count("feedback", filter=Q(feedback__vote=FindingFeedback.Vote.DOWN)),
            )
            .order_by("category")
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            decided = row["accepted"] + row["dismissed"]
            rate = round(row["accepted"] / decided, 3) if decided else None
            result.append({**row, "acceptance_rate": rate})
        return Response(result)


def _parse_date(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid date: {value}") from exc
    return parsed if parsed.tzinfo else timezone.make_aware(parsed)


class ReviewProfilesView(APIView):
    """Built-in review profiles and the threshold presets the dashboard applies when one is chosen."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(
            [
                {
                    "id": p.id,
                    "label": p.label,
                    "description": p.description,
                    "categories": sorted(p.allowed_categories) if p.allowed_categories is not None else None,
                    "defaults": p.defaults,
                }
                for p in PROFILES.values()
            ]
        )


class UsageView(APIView):
    """Token usage aggregated by day, repository, credential, or model (raw data for KEY-03/ADM-05)."""

    permission_classes = [IsAdmin]
    GROUPS = {"day", "repository", "credential", "model"}

    def get(self, request: Request) -> Response:
        now = timezone.now()
        start = _parse_date(request.query_params.get("from"), now - timedelta(days=30))
        end = _parse_date(request.query_params.get("to"), now)
        group_by = [g for g in request.query_params.get("group_by", "day").split(",") if g]
        if not group_by or not set(group_by) <= self.GROUPS:
            raise ValidationError({"group_by": f"Choose from: {', '.join(sorted(self.GROUPS))}"})
        qs = LLMUsage.objects.filter(created_at__gte=start, created_at__lt=end)
        fields: list[str] = []
        if "day" in group_by:
            qs = qs.annotate(day=TruncDay("created_at"))
            fields.append("day")
        if "repository" in group_by:
            fields += ["repository_id", "repository__full_name"]
        if "credential" in group_by:
            fields += ["credential_id", "credential__name"]
        if "model" in group_by:
            fields.append("model")
        rows = (
            qs.values(*fields)
            .annotate(
                requests=Count("id"),
                errors=Count("id", filter=Q(status=LLMUsage.Status.ERROR)),
                input_tokens=Sum("input_tokens"),
                output_tokens=Sum("output_tokens"),
            )
            .order_by(*fields)
        )
        totals = qs.aggregate(
            input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"), requests=Count("id")
        )
        return Response(
            {
                "from": start,
                "to": end,
                "group_by": group_by,
                "rows": list(rows),
                "totals": {k: v or 0 for k, v in totals.items()},
            }
        )
