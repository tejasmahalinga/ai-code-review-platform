from __future__ import annotations

from typing import Any, cast

from django.db.models import QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.permissions import IsAdmin
from apps.audit.services import diff, record
from apps.core.logging import get_logger
from apps.credentials.models import LLMCredential
from apps.credentials.serializers import LLMCredentialSerializer
from apps.credentials.services import check_credential, revalidate
from apps.llm import registry
from apps.llm.base import LLMError

logger = get_logger(__name__)

AUDITED_FIELDS = ("name", "default_model", "base_url", "monthly_budget_usd")


class RotateSerializer(serializers.Serializer[Any]):
    api_key = serializers.CharField(max_length=500, trim_whitespace=True)


def _audited(credential: LLMCredential) -> dict[str, Any]:
    return {
        f: str(getattr(credential, f)) if getattr(credential, f) is not None else None for f in AUDITED_FIELDS
    }


class LLMCredentialViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet[LLMCredential],
):
    serializer_class = LLMCredentialSerializer
    permission_classes = [IsAdmin]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete"]

    def get_queryset(self) -> QuerySet[LLMCredential]:
        qs = LLMCredential.objects.all()
        if self.request.query_params.get("include_revoked") != "true":
            qs = qs.exclude(status=LLMCredential.Status.REVOKED)
        return qs

    def perform_create(self, serializer: Any) -> None:
        data = serializer.validated_data
        api_key: str = data.pop("api_key", "") or ""
        try:
            check_credential(data["provider"], api_key, data["default_model"], data.get("base_url") or None)
        except LLMError as exc:
            raise ValidationError({"api_key": [f"Validation failed: {exc}"]}) from exc
        credential = LLMCredential(
            **data, created_by=cast(User, self.request.user), last_validated_at=timezone.now()
        )
        credential.set_secret(api_key)
        credential.save()
        serializer.instance = credential
        record("credential.created", request=self.request, target=credential, model=credential.default_model)
        logger.info("credential.created", credential_id=credential.pk, provider=credential.provider)

    def perform_update(self, serializer: Any) -> None:
        instance: LLMCredential = serializer.instance
        if instance.status == LLMCredential.Status.REVOKED:
            raise ValidationError("Revoked credentials cannot be edited.")
        changed_target = any(
            k in serializer.validated_data and serializer.validated_data[k] != getattr(instance, k)
            for k in ("default_model", "base_url")
        )
        before = _audited(instance)
        credential = serializer.save()
        if changed_target:
            revalidate(credential)
        if changes := diff(before, _audited(credential)):
            record("credential.updated", request=self.request, target=credential, changes=changes)

    def perform_destroy(self, instance: LLMCredential) -> None:
        instance.revoke()
        record("credential.revoked", request=self.request, target=instance)
        logger.info("credential.revoked", credential_id=instance.pk)

    @extend_schema(request=None, responses={200: LLMCredentialSerializer})
    @action(detail=True, methods=["post"])
    def validate(self, request: Request, pk: str | None = None) -> Response:
        credential = self.get_object()
        if credential.status == LLMCredential.Status.REVOKED:
            raise ValidationError("Revoked credentials cannot be validated.")
        revalidate(credential)
        return Response(self.get_serializer(credential).data, status=status.HTTP_200_OK)

    @extend_schema(request=RotateSerializer, responses={200: LLMCredentialSerializer})
    @action(detail=True, methods=["post"])
    def rotate(self, request: Request, pk: str | None = None) -> Response:
        """Replaces the stored API key after validating the new one; repositories keep this credential."""
        credential = self.get_object()
        if credential.status == LLMCredential.Status.REVOKED:
            raise ValidationError("Revoked credentials cannot be rotated.")
        body = RotateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        api_key = body.validated_data["api_key"]
        try:
            check_credential(
                credential.provider, api_key, credential.default_model, credential.base_url or None
            )
        except LLMError as exc:
            raise ValidationError({"api_key": [f"Validation failed: {exc}"]}) from exc
        credential.set_secret(api_key)
        credential.status = LLMCredential.Status.VALID
        credential.status_message = ""
        credential.last_validated_at = timezone.now()
        credential.save(
            update_fields=["encrypted_secret", "last4", "status", "status_message", "last_validated_at"]
        )
        record("credential.rotated", request=request, target=credential)
        logger.info("credential.rotated", credential_id=credential.pk)
        return Response(self.get_serializer(credential).data)


class LLMProvidersView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(
            [
                {
                    "id": p.id,
                    "label": p.label,
                    "requires_api_key": p.requires_api_key,
                    "requires_base_url": p.requires_base_url,
                    "suggested_models": list(p.suggested_models),
                    "default_base_url": p.default_base_url,
                }
                for p in registry.enabled_providers()
            ]
        )
