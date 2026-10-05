from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db.models import Count, OuterRef, Q, QuerySet, Subquery, Sum
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

from apps.accounts.permissions import IsAdmin, IsAdminOrReadOnly
from apps.core.exceptions import Conflict
from apps.reviews.models import Finding, LLMUsage, PullRequest, ReviewRun
from apps.reviews.serializers import (
    REPORTED_STATUSES,
    SEVERITIES,
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
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = PullRequestPagination

    def get_queryset(self) -> QuerySet[PullRequest]:
        latest = ReviewRun.objects.filter(pull_request=OuterRef("pk")).order_by("-created_at", "-id")
        qs = PullRequest.objects.select_related("repository").annotate(
            latest_status=Subquery(latest.values("status")[:1])
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
        if q := params.get("q"):
            qs = qs.filter(Q(title__icontains=q) | Q(author_login__iexact=q))
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
        ).prefetch_related("findings")


def _parse_date(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid date: {value}") from exc
    return parsed if parsed.tzinfo else timezone.make_aware(parsed)


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
