"""Single sign-on views (ADM-07): OpenID Connect start and callback, and linked identities."""

from __future__ import annotations

from typing import cast

from django.conf import settings
from django.contrib.auth import login
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import github_oauth, oidc
from apps.accounts.models import ExternalIdentity, Invite, User
from apps.accounts.permissions import SessionOnly
from apps.accounts.views import _active_admins, _redirect, _safe_next, accept_invite
from apps.audit.services import record
from apps.core.logging import get_logger

logger = get_logger(__name__)

SESSION_KEY = "oidc_login"


class OIDCStartView(APIView):
    """Browser navigation: redirects to the identity provider to sign in, accept an invite, or link."""

    permission_classes = [AllowAny]

    def get(self, request: Request, provider_id: str) -> HttpResponseRedirect:
        provider = oidc.get_provider(provider_id)
        if provider is None:
            return _redirect("/login?error=sso_unavailable")
        link = request.query_params.get("link") == "1"
        if link and not request.user.is_authenticated:
            return _redirect("/login")
        try:
            data, url = oidc.state_payload(
                provider,
                invite=request.query_params.get("invite", "")[:200],
                link=link,
                next=_safe_next(request.query_params.get("next")),
            )
        except (oidc.OIDCError, ValueError) as exc:
            logger.warning("auth.sso_discovery_failed", provider=provider_id, error=str(exc))
            return _redirect("/login?error=sso_failed")
        request.session[SESSION_KEY] = data
        return HttpResponseRedirect(url)


class OIDCCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request: Request, provider_id: str) -> HttpResponseRedirect:
        flow = request.session.pop(SESSION_KEY, None)
        params = request.query_params
        if (
            not flow
            or flow.get("provider") != provider_id
            or not params.get("state")
            or params.get("state") != flow.get("state")
        ):
            return _redirect("/login?error=sso_state")
        if params.get("error"):
            return _redirect("/login?error=sso_denied")
        provider = oidc.get_provider(provider_id)
        if provider is None or not params.get("code"):
            return _redirect("/login?error=sso_unavailable")
        try:
            identity = oidc.fetch_identity(
                provider, code=params["code"], code_verifier=flow["verifier"], nonce=flow["nonce"]
            )
        except (oidc.OIDCError, ValueError) as exc:
            logger.warning("auth.sso_failed", provider=provider_id, error=str(exc))
            return _redirect("/login?error=sso_failed")

        if flow.get("link"):
            return _link_current_user(request, provider, identity)
        user, error = resolve(request, provider, identity, flow.get("invite", ""))
        if user is None:
            record(
                "auth.login_failed",
                request=request,
                method=f"oidc:{provider.id}",
                username=identity.username,
                reason=error,
            )
            return _redirect(f"/login?error={error}")
        login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        record(
            "auth.login",
            request=request,
            actor=user,
            method=f"oidc:{provider.id}",
            username=identity.username,
        )
        return _redirect(flow.get("next") or "/pull-requests")


def _link_current_user(
    request: Request, provider: oidc.OIDCProvider, identity: oidc.OIDCIdentity
) -> HttpResponseRedirect:
    if not request.user.is_authenticated:
        return _redirect("/login")
    user = cast(User, request.user)
    existing = ExternalIdentity.objects.filter(provider=provider.id, subject=identity.subject).first()
    if existing and existing.user_id != user.pk:
        return _redirect("/settings/account?sso=in_use")
    if existing is None:
        try:
            _link(user, identity)
        except IntegrityError:
            return _redirect("/settings/account?sso=already_linked")
    record("auth.sso_linked", request=request, target=user, provider=provider.id, username=identity.username)
    return _redirect("/settings/account?sso=linked")


def _link(user: User, identity: oidc.OIDCIdentity) -> ExternalIdentity:
    with transaction.atomic():
        return ExternalIdentity.objects.create(
            user=user,
            provider=identity.provider,
            subject=identity.subject,
            username=identity.username[:255],
            email=identity.email[:254] if identity.email_verified else "",
            last_login_at=timezone.now(),
        )


def _sync_role(request: Request, user: User, provider: oidc.OIDCProvider, mapped: str | None) -> None:
    """Keeps the role in line with IdP groups. Never demotes the last active admin."""
    if not (provider.maps_roles and provider.sync_roles and mapped) or user.role == mapped:
        return
    if (
        user.is_admin
        and mapped != User.Role.ADMIN
        and list(_active_admins().values_list("pk", flat=True)) == [user.pk]
    ):
        logger.warning("auth.sso_role_sync_skipped_last_admin", user_id=user.pk)
        return
    previous = user.role
    user.role = mapped
    user.save(update_fields=["role"])
    record(
        "user.role_synced",
        request=request,
        actor=user,
        target=user,
        provider=provider.id,
        **{"from": previous, "to": mapped},
    )


def resolve(
    request: Request, provider: oidc.OIDCProvider, identity: oidc.OIDCIdentity, invite_token: str
) -> tuple[User | None, str]:
    mapped = provider.role_for(identity.groups) if provider.maps_roles else None
    if provider.maps_roles and mapped is None:
        return None, "not_in_allowed_group"

    link = (
        ExternalIdentity.objects.select_related("user")
        .filter(provider=provider.id, subject=identity.subject)
        .first()
    )
    if link is not None:
        if not link.user.is_active:
            return None, "account_disabled"
        ExternalIdentity.objects.filter(pk=link.pk).update(
            last_login_at=timezone.now(), username=identity.username[:255]
        )
        _sync_role(request, link.user, provider, mapped)
        return link.user, ""

    invite = Invite.find_pending(invite_token)
    if invite is not None:
        with transaction.atomic():
            user = accept_invite(
                invite.pk, name=identity.name or identity.username, via=f"oidc:{provider.id}"
            )
            if user is None:
                return None, "invite_invalid"
            _link(user, identity)
        return user, ""

    if identity.email_verified:
        match = User.objects.filter(email__iexact=identity.email).first()
        if match is not None:
            if match.identities.filter(provider=provider.id).exists():
                return None, "identity_mismatch"  # already linked to a different account at this IdP
            if not match.is_active:
                return None, "account_disabled"
            _link(match, identity)
            record("auth.sso_linked", request=request, actor=match, target=match, provider=provider.id)
            _sync_role(request, match, provider, mapped)
            return match, ""

        # Group mapping is the IdP admin's statement of who may use Reviewbot, so it provisions accounts.
        role = mapped
        if role is None and github_oauth.signup_allowed(identity.email):
            role = settings.SIGNUP_ROLE if settings.SIGNUP_ROLE in ("viewer", "reviewer") else "viewer"
        if role is not None:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=identity.email, password=None, name=identity.name or identity.username, role=role
                )
                _link(user, identity)
            record(
                "user.signed_up",
                request=request,
                actor=user,
                target=user,
                role=role,
                method=f"oidc:{provider.id}",
            )
            return user, ""
    return None, "no_account"


# --- linked identities --------------------------------------------------------------------------


class IdentitySerializer(serializers.ModelSerializer[ExternalIdentity]):
    provider_name = serializers.SerializerMethodField()

    class Meta:
        model = ExternalIdentity
        fields = ["id", "provider", "provider_name", "username", "email", "created_at", "last_login_at"]
        read_only_fields = fields

    def get_provider_name(self, obj: ExternalIdentity) -> str:
        provider = oidc.get_provider(obj.provider)
        return provider.name if provider else obj.provider


class IdentityListView(APIView):
    @extend_schema(responses={200: IdentitySerializer(many=True)})
    def get(self, request: Request) -> Response:
        rows = ExternalIdentity.objects.filter(user=cast(User, request.user))
        return Response(IdentitySerializer(rows, many=True).data)


class IdentityDetailView(APIView):
    permission_classes = [SessionOnly]

    @extend_schema(request=None, responses={204: None})
    def delete(self, request: Request, identity_id: int) -> Response:
        user = cast(User, request.user)
        identity = ExternalIdentity.objects.filter(user=user, pk=identity_id).first()
        if identity is None:
            raise NotFound()
        others = user.identities.exclude(pk=identity.pk).exists() or bool(user.github_id)
        if not (others or user.has_usable_password()):
            raise ValidationError(
                "Set a password or link another sign-in method first, or you could not sign in."
            )
        identity.delete()
        record("auth.sso_unlinked", request=request, target=user, provider=identity.provider)
        return Response(status=status.HTTP_204_NO_CONTENT)
