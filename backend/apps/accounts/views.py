from __future__ import annotations

import secrets
from typing import Any, cast

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.core.mail import send_mail
from django.db import connection, transaction
from django.db.models import QuerySet
from django.http import Http404, HttpResponseRedirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import github_oauth
from apps.accounts.models import ApiToken, Invite, User
from apps.accounts.permissions import IsAdmin, SessionOnly
from apps.accounts.serializers import (
    ApiTokenSerializer,
    InviteAcceptSerializer,
    InviteSerializer,
    LoginSerializer,
    MeUpdateSerializer,
    PasswordChangeSerializer,
    SetupSerializer,
    UserSerializer,
    UserUpdateSerializer,
)
from apps.accounts.throttles import InviteThrottle, LoginThrottle, SetupThrottle
from apps.audit.services import diff, record
from apps.core.logging import get_logger

logger = get_logger(__name__)

# Arbitrary constant used as a Postgres advisory-lock id so concurrent setup requests serialize.
_SETUP_LOCK_ID = 7_301_001


@method_decorator(ensure_csrf_cookie, name="get")
class CsrfView(APIView):
    """Sets the ``csrftoken`` cookie. The dashboard calls this once before any unsafe request."""

    permission_classes = [AllowAny]

    @extend_schema(responses={204: None})
    def get(self, request: Request) -> Response:
        return Response(status=status.HTTP_204_NO_CONTENT)


class SetupStatusView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        responses={
            200: {
                "type": "object",
                "properties": {"needs_setup": {"type": "boolean"}, "github_login": {"type": "boolean"}},
            }
        }
    )
    def get(self, request: Request) -> Response:
        return Response(
            {
                "needs_setup": not User.objects.exists(),
                "github_login": github_oauth.oauth_client() is not None,
            }
        )


class SetupView(APIView):
    """Creates the first admin. Only available while the instance has zero users."""

    permission_classes = [AllowAny]
    throttle_classes = [SetupThrottle]

    @extend_schema(request=SetupSerializer, responses={201: UserSerializer})
    def post(self, request: Request) -> Response:
        if User.objects.exists():
            raise Http404
        serializer = SetupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data: dict[str, Any] = serializer.validated_data
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_SETUP_LOCK_ID])
            if User.objects.exists():
                raise Http404
            user = User.objects.create_superuser(
                email=data["email"], password=data["password"], name=data.get("name", "")
            )
        login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        record("setup.completed", request=request, actor=user, target=user)
        logger.info("setup.admin_created", user_id=user.pk)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    @extend_schema(request=LoginSerializer, responses={200: UserSerializer})
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request._request,
            username=serializer.validated_data["email"].lower(),
            password=serializer.validated_data["password"],
        )
        if user is None:
            LoginThrottle().record_failure(request, self)
            record("auth.login_failed", request=request, email=serializer.validated_data["email"].lower())
            logger.info("auth.login_failed")
            return Response(
                {
                    "error": {
                        "code": "invalid_credentials",
                        "message": "Invalid email or password.",
                        "details": None,
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        login(request._request, user)
        record("auth.login", request=request, actor=user, method="password")
        logger.info("auth.login", user_id=user.pk)
        return Response(UserSerializer(user).data)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        record("auth.logout", request=request)
        logout(request._request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    @extend_schema(responses={200: UserSerializer})
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(cast(User, request.user)).data)

    @extend_schema(request=MeUpdateSerializer, responses={200: UserSerializer})
    def patch(self, request: Request) -> Response:
        user = cast(User, request.user)
        serializer = MeUpdateSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(user).data)


class PasswordChangeView(APIView):
    """Changes (or, for accounts created through GitHub, sets) the signed-in user's password."""

    permission_classes = [SessionOnly]

    @extend_schema(request=PasswordChangeSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        user = cast(User, request.user)
        serializer = PasswordChangeSerializer(data=request.data, context={"user": user})
        serializer.is_valid(raise_exception=True)
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        update_session_auth_hash(request._request, user)  # keep this session; other sessions are logged out
        record("auth.password_changed", request=request, target=user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class GitHubUnlinkView(APIView):
    permission_classes = [SessionOnly]

    @extend_schema(request=None, responses={200: UserSerializer})
    def delete(self, request: Request) -> Response:
        user = cast(User, request.user)
        if not user.github_id:
            raise ValidationError("No GitHub account is linked.")
        if not user.has_usable_password():
            raise ValidationError("Set a password first, or you would not be able to sign in.")
        login_name = user.github_login
        user.github_id = None
        user.github_login = ""
        user.save(update_fields=["github_id", "github_login"])
        record("auth.github_unlinked", request=request, target=user, github_login=login_name)
        return Response(UserSerializer(user).data)


# --- Users & invites (ADM-02) -------------------------------------------------------------------


def _active_admins() -> QuerySet[User]:
    return User.objects.filter(role=User.Role.ADMIN, is_active=True)


class UserViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet[User]
):
    serializer_class = UserSerializer
    permission_classes = [IsAdmin]
    pagination_class = None
    http_method_names = ["get", "patch"]

    def get_queryset(self) -> QuerySet[User]:
        return User.objects.order_by("-is_active", "email")

    @extend_schema(request=UserUpdateSerializer, responses={200: UserSerializer})
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        target = self.get_object()
        serializer = UserUpdateSerializer(target, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        me = cast(User, request.user)
        if target.pk == me.pk and data.get("is_active") is False:
            raise ValidationError({"is_active": "You cannot deactivate your own account."})
        loses_admin = (
            target.is_admin
            and target.is_active
            and (data.get("role", target.role) != User.Role.ADMIN or data.get("is_active", True) is False)
        )
        with transaction.atomic():
            # Lock the admins so two concurrent demotions cannot remove the last one.
            admins = list(_active_admins().select_for_update().values_list("pk", flat=True))
            if loses_admin and admins == [target.pk]:
                raise ValidationError("At least one active admin is required.")
            before = {"name": target.name, "role": target.role, "is_active": target.is_active}
            serializer.save()
            changes = diff(before, {"name": target.name, "role": target.role, "is_active": target.is_active})
            if "role" in changes:
                record("user.role_changed", request=request, target=target, **changes["role"])
            if "is_active" in changes:
                action = "user.reactivated" if target.is_active else "user.deactivated"
                record(action, request=request, target=target)
        return Response(UserSerializer(target).data)


def invite_url(token: str) -> str:
    return f"{settings.PUBLIC_URL}/invite/{token}"


def _send_invite_email(invite: Invite, url: str, inviter: User) -> bool:
    if not settings.EMAIL_ENABLED:
        return False
    try:
        send_mail(
            subject="You're invited to Reviewbot",
            message=(
                f"{inviter.name or inviter.email} invited you to Reviewbot "
                f"as {invite.get_role_display()}.\n\n"
                f"Accept the invitation: {url}\n\n"
                f"This link expires on {invite.expires_at:%Y-%m-%d %H:%M} UTC."
            ),
            from_email=None,
            recipient_list=[invite.email],
        )
    except Exception as exc:  # SMTP problems must not fail the invite; the admin can copy the link.
        logger.warning("invite.email_failed", invite_id=invite.pk, error=type(exc).__name__)
        return False
    return True


class InviteViewSet(
    mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet[Invite]
):
    serializer_class = InviteSerializer
    permission_classes = [IsAdmin]
    pagination_class = None

    def get_queryset(self) -> QuerySet[Invite]:
        qs = Invite.objects.select_related("created_by")
        if self.request.query_params.get("status") == "pending":
            qs = qs.pending()
        return qs[:200] if self.action == "list" else qs

    @extend_schema(request=InviteSerializer, responses={201: InviteSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        me = cast(User, request.user)
        # A new invite for the same address replaces the pending one.
        Invite.objects.pending().filter(email=serializer.validated_data["email"]).update(
            revoked_at=timezone.now()
        )
        invite, token = Invite.issue(
            email=serializer.validated_data["email"], role=serializer.validated_data["role"], created_by=me
        )
        url = invite_url(token)
        email_sent = _send_invite_email(invite, url, me)
        record("user.invited", request=request, target=invite, role=invite.role, email_sent=email_sent)
        # The link is returned exactly once; only its hash is stored.
        return Response(
            {**InviteSerializer(invite).data, "url": url, "email_sent": email_sent},
            status=status.HTTP_201_CREATED,
        )

    def perform_destroy(self, instance: Invite) -> None:
        if instance.status != "pending":
            raise ValidationError("Only pending invites can be revoked.")
        instance.revoked_at = timezone.now()
        instance.save(update_fields=["revoked_at"])
        record("invite.revoked", request=self.request, target=instance)


class InvitePublicView(APIView):
    """Lets the invitee see the invite and accept it with a password."""

    permission_classes = [AllowAny]
    throttle_classes = [InviteThrottle]

    def get(self, request: Request, token: str) -> Response:
        invite = Invite.find_pending(token)
        if invite is None:
            raise Http404
        return Response(
            {
                "email": invite.email,
                "role": invite.role,
                "expires_at": invite.expires_at,
                "github_login": github_oauth.oauth_client() is not None,
            }
        )

    @extend_schema(request=InviteAcceptSerializer, responses={201: UserSerializer})
    def post(self, request: Request, token: str) -> Response:
        invite = Invite.find_pending(token)
        if invite is None:
            raise Http404
        serializer = InviteAcceptSerializer(data=request.data, context={"invite": invite})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            user = accept_invite(invite.pk, name=serializer.validated_data.get("name", ""))
            if user is None:
                raise Http404
            user.set_password(serializer.validated_data["password"])
            user.save(update_fields=["password"])
        login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        record("auth.login", request=request, actor=user, method="invite")
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


def accept_invite(
    invite_id: int, *, name: str, github: github_oauth.GitHubIdentity | None = None
) -> User | None:
    """Creates the invited account. Must run inside a transaction; returns None if the invite was used."""
    invite = Invite.objects.pending().select_for_update().filter(pk=invite_id).first()
    if invite is None or User.objects.filter(email__iexact=invite.email).exists():
        return None
    user = User.objects.create_user(email=invite.email, password=None, name=name, role=invite.role)
    if github is not None:
        user.github_id = github.id
        user.github_login = github.login
        user.save(update_fields=["github_id", "github_login"])
    invite.accepted_at = timezone.now()
    invite.accepted_by = user
    invite.save(update_fields=["accepted_at", "accepted_by"])
    record(
        "invite.accepted", actor=user, target=invite, role=invite.role, via="github" if github else "password"
    )
    return user


# --- Sign in with GitHub (ADM-03) ---------------------------------------------------------------

OAUTH_SESSION_KEY = "github_oauth"


def _redirect(path: str) -> HttpResponseRedirect:
    return HttpResponseRedirect(f"{settings.PUBLIC_URL}{path}")


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/pull-requests"


class GitHubLoginStartView(APIView):
    """Browser navigation: starts the GitHub OAuth flow (sign in, accept an invite, or link an account)."""

    permission_classes = [AllowAny]

    def get(self, request: Request) -> HttpResponseRedirect:
        client = github_oauth.oauth_client()
        if client is None:
            return _redirect("/login?error=github_unavailable")
        link = request.query_params.get("link") == "1"
        if link and not request.user.is_authenticated:
            return _redirect("/login")
        state = secrets.token_urlsafe(24)
        request.session[OAUTH_SESSION_KEY] = {
            "state": state,
            "invite": request.query_params.get("invite", "")[:200],
            "link": link,
            "next": _safe_next(request.query_params.get("next")),
        }
        return HttpResponseRedirect(github_oauth.authorize_url(client, state))


class GitHubLoginCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request: Request) -> HttpResponseRedirect:
        flow = request.session.pop(OAUTH_SESSION_KEY, None)
        params = request.query_params
        if not flow or not params.get("state") or params.get("state") != flow.get("state"):
            return _redirect("/login?error=github_state")
        if params.get("error"):
            return _redirect("/login?error=github_denied")
        client = github_oauth.oauth_client()
        if client is None or not params.get("code"):
            return _redirect("/login?error=github_unavailable")
        try:
            identity = github_oauth.fetch_identity(client, params["code"])
        except github_oauth.OAuthError as exc:
            logger.warning("auth.github_failed", error=str(exc))
            return _redirect("/login?error=github_failed")

        if flow.get("link"):
            return self._link(request, identity)
        user, error = self._resolve(request, identity, flow.get("invite", ""))
        if user is None:
            record(
                "auth.login_failed",
                request=request,
                method="github",
                github_login=identity.login,
                reason=error,
            )
            return _redirect(f"/login?error={error}")
        login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        record("auth.login", request=request, actor=user, method="github", github_login=identity.login)
        return _redirect(flow.get("next") or "/pull-requests")

    def _link(self, request: Request, identity: github_oauth.GitHubIdentity) -> HttpResponseRedirect:
        if not request.user.is_authenticated:
            return _redirect("/login")
        user = cast(User, request.user)
        if User.objects.filter(github_id=identity.id).exclude(pk=user.pk).exists():
            return _redirect("/settings/account?github=in_use")
        user.github_id = identity.id
        user.github_login = identity.login
        user.save(update_fields=["github_id", "github_login"])
        record("auth.github_linked", request=request, target=user, github_login=identity.login)
        return _redirect("/settings/account?github=linked")

    def _resolve(
        self, request: Request, identity: github_oauth.GitHubIdentity, invite_token: str
    ) -> tuple[User | None, str]:
        user = User.objects.filter(github_id=identity.id).first()
        if user is not None:
            return (user, "") if user.is_active else (None, "account_disabled")

        invite = Invite.find_pending(invite_token)
        if invite is not None:
            with transaction.atomic():
                created = accept_invite(invite.pk, name=identity.name or identity.login, github=identity)
            return (created, "") if created else (None, "invite_invalid")

        # Link an existing account by verified email, but never one already tied to another GitHub user.
        for email in identity.verified_emails:
            match = User.objects.filter(email__iexact=email).first()
            if match is None:
                continue
            if match.github_id is not None:
                return None, "github_mismatch"
            if not match.is_active:
                return None, "account_disabled"
            match.github_id = identity.id
            match.github_login = identity.login
            match.save(update_fields=["github_id", "github_login"])
            record(
                "auth.github_linked", request=request, actor=match, target=match, github_login=identity.login
            )
            return match, ""

        if github_oauth.signup_allowed(identity.primary_email):
            role = settings.SIGNUP_ROLE if settings.SIGNUP_ROLE in User.Role.values else User.Role.VIEWER
            if role == User.Role.ADMIN:
                role = User.Role.VIEWER  # self-signup never grants admin
            user = User.objects.create_user(
                email=identity.primary_email, password=None, name=identity.name or identity.login, role=role
            )
            user.github_id = identity.id
            user.github_login = identity.login
            user.save(update_fields=["github_id", "github_login"])
            record("user.signed_up", request=request, actor=user, target=user, role=role, method="github")
            return user, ""
        return None, "no_account"


# --- Personal API tokens (ADM-06) ---------------------------------------------------------------


class ApiTokenViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet[ApiToken],
):
    """The signed-in user's API tokens. Tokens act with their owner's role."""

    serializer_class = ApiTokenSerializer
    permission_classes = [SessionOnly]
    pagination_class = None

    def get_queryset(self) -> QuerySet[ApiToken]:
        return ApiToken.objects.filter(user=cast(User, self.request.user), revoked_at__isnull=True)

    @extend_schema(request=ApiTokenSerializer, responses={201: ApiTokenSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = cast(User, request.user)
        if ApiToken.objects.filter(user=user, revoked_at__isnull=True).count() >= 20:
            raise ValidationError("You already have 20 active tokens. Revoke one first.")
        token, plaintext = ApiToken.issue(
            user=user,
            name=serializer.validated_data["name"],
            expires_in_days=serializer.validated_data.get("expires_in_days"),
        )
        record("token.created", request=request, target=token, expires_at=str(token.expires_at or "never"))
        # The token is returned exactly once.
        return Response(
            {**ApiTokenSerializer(token).data, "token": plaintext}, status=status.HTTP_201_CREATED
        )

    def perform_destroy(self, instance: ApiToken) -> None:
        instance.revoked_at = timezone.now()
        instance.save(update_fields=["revoked_at"])
        record("token.revoked", request=self.request, target=instance)
