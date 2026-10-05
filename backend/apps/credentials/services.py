from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.credentials.models import LLMCredential
from apps.llm.base import AuthenticationFailed, LLMError, LLMProvider
from apps.llm.registry import build_provider


def provider_for(credential: LLMCredential, model: str | None = None, http_client: Any = None) -> LLMProvider:
    return build_provider(
        credential.provider,
        api_key=credential.get_secret(),
        model=model or credential.default_model,
        base_url=credential.base_url or None,
        http_client=http_client,
    )


def check_credential(provider: str, api_key: str, model: str, base_url: str | None) -> None:
    """Validates a not-yet-saved credential. Raises LLMError on failure."""
    build_provider(provider, api_key=api_key, model=model, base_url=base_url).validate()


def revalidate(credential: LLMCredential) -> LLMCredential:
    """Re-checks a stored credential and records the outcome."""
    try:
        provider_for(credential).validate()
    except LLMError as exc:
        credential.status = LLMCredential.Status.INVALID
        credential.status_message = str(exc)[:500]
    else:
        credential.status = LLMCredential.Status.VALID
        credential.status_message = ""
    credential.last_validated_at = timezone.now()
    credential.save(update_fields=["status", "status_message", "last_validated_at"])
    return credential


def mark_invalid(credential: LLMCredential, error: AuthenticationFailed) -> None:
    LLMCredential.objects.filter(pk=credential.pk, status=LLMCredential.Status.VALID).update(
        status=LLMCredential.Status.INVALID, status_message=str(error)[:500]
    )
