from __future__ import annotations

import secrets
from typing import Any, cast
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, QuerySet
from django.http import HttpResponseRedirect
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.permissions import IsAdmin, IsAdminOrReadOnly, IsReviewerOrReadOnly
from apps.audit.services import diff, record
from apps.core.exceptions import Conflict
from apps.core.logging import get_logger
from apps.git_providers.base import GitProviderError
from apps.git_providers.github import app as github_app
from apps.git_providers.github.client import exchange_manifest_code
from apps.repositories import services
from apps.repositories.models import GitProviderConnection, Repository
from apps.repositories.serializers import (
    REVIEWER_SETTINGS_FIELDS,
    InstallationSerializer,
    RepositorySerializer,
    RepositorySettingsSerializer,
)

logger = get_logger(__name__)

SESSION_STATE_KEY = "github_manifest_state"


def _dashboard_redirect(path: str) -> HttpResponseRedirect:
    return HttpResponseRedirect(f"{settings.PUBLIC_URL}{path}")


def _is_admin(request: Request) -> bool:
    return bool(request.user and request.user.is_authenticated and getattr(request.user, "is_admin", False))


class GitHubIntegrationView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request: Request) -> Response:
        connection = services.github_connection()
        data: dict[str, Any] = {
            "connected": connection is not None,
            "web_url": settings.GITHUB_URL,
            "webhook_url": github_app.webhook_url(),
            "app_name": "",
            "app_slug": "",
            "app_html_url": "",
            "install_url": "",
            "installations": [],
        }
        if connection:
            data.update(
                app_name=connection.app_name,
                app_slug=connection.app_slug,
                app_html_url=connection.app_html_url,
                install_url=connection.install_url,
                installations=InstallationSerializer(
                    connection.installations.filter(removed_at__isnull=True), many=True
                ).data,
            )
        return Response(data)

    @extend_schema(request=None, responses={204: None})
    def delete(self, request: Request) -> Response:
        connection = services.github_connection()
        if connection is None:
            raise NotFound("GitHub is not connected.")
        connection.delete()
        record("integration.github_disconnected", request=request, app_slug=connection.app_slug)
        logger.warning("github.disconnected", user_id=request.user.pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ManualGitHubAppSerializer(serializers.Serializer[Any]):
    app_id = serializers.CharField(max_length=64)
    app_slug = serializers.SlugField(max_length=200)
    private_key = serializers.CharField(max_length=10_000, trim_whitespace=True)
    webhook_secret = serializers.CharField(max_length=500)
    # Optional: enables "Sign in with GitHub" through this App.
    client_id = serializers.CharField(max_length=200, required=False, allow_blank=True)
    client_secret = serializers.CharField(max_length=500, required=False, allow_blank=True)

    def validate_private_key(self, value: str) -> str:
        if "PRIVATE KEY-----" not in value:
            raise serializers.ValidationError(
                "Paste the PEM private key downloaded from the GitHub App settings."
            )
        return value


class GitHubManualConfigView(APIView):
    """Alternative to the manifest flow for Apps created by hand (e.g. older GitHub Enterprise)."""

    permission_classes = [IsAdmin]

    @extend_schema(request=ManualGitHubAppSerializer, responses={201: None})
    def post(self, request: Request) -> Response:
        if services.github_connection():
            raise Conflict("GitHub is already connected. Disconnect it first.")
        serializer = ManualGitHubAppSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        connection = GitProviderConnection(
            provider=GitProviderConnection.Provider.GITHUB,
            web_url=settings.GITHUB_URL,
            api_url=settings.GITHUB_API_URL,
            app_id=data["app_id"],
            app_slug=data["app_slug"],
            app_name=data["app_slug"],
            app_html_url=f"{settings.GITHUB_URL}/apps/{data['app_slug']}",
            client_id=data.get("client_id", ""),
        )
        connection.set_secrets(
            private_key=data["private_key"],
            webhook_secret=data["webhook_secret"],
            client_secret=data.get("client_secret", ""),
        )
        try:
            github_app.app_client(connection).list_installations()
        except GitProviderError as exc:
            raise ValidationError(f"GitHub rejected these credentials: {exc}") from exc
        connection.save()
        record("integration.github_connected", request=request, app_slug=connection.app_slug, method="manual")
        return Response(status=status.HTTP_201_CREATED)


class ManifestRequestSerializer(serializers.Serializer[Any]):
    app_name = serializers.CharField(max_length=34, required=False, allow_blank=True)
    organization = serializers.RegexField(r"^[A-Za-z0-9-]{1,39}$", required=False, allow_blank=True)


class GitHubManifestView(APIView):
    """Step 1 of the manifest flow: returns the form the browser POSTs to GitHub."""

    permission_classes = [IsAdmin]

    @extend_schema(request=ManifestRequestSerializer)
    def post(self, request: Request) -> Response:
        if services.github_connection():
            raise Conflict("GitHub is already connected.")
        serializer = ManifestRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        app_name = serializer.validated_data.get("app_name") or "Reviewbot"
        state = github_app.new_state()
        request.session[SESSION_STATE_KEY] = state
        return Response(
            {
                "post_url": github_app.manifest_post_url(
                    settings.GITHUB_URL, state, serializer.validated_data.get("organization") or None
                ),
                "manifest": github_app.manifest_json(app_name),
                "state": state,
            }
        )


class GitHubCallbackView(APIView):
    """Step 2: GitHub redirects here with a one-time code that we exchange for App credentials."""

    # Browser navigation from github.com: redirect to the login page instead of returning JSON 401.
    permission_classes = [AllowAny]

    def get(self, request: Request) -> HttpResponseRedirect:
        if not _is_admin(request):
            return _dashboard_redirect("/login")
        expected = request.session.pop(SESSION_STATE_KEY, None)
        code = request.query_params.get("code", "")
        if not expected or request.query_params.get("state") != expected or not code:
            return _dashboard_redirect("/settings/integrations?github=invalid_state")
        if services.github_connection():
            return _dashboard_redirect("/settings/integrations?github=already_connected")
        try:
            data = exchange_manifest_code(settings.GITHUB_API_URL, code)
        except GitProviderError as exc:
            logger.warning("github.manifest_exchange_failed", error=str(exc))
            return _dashboard_redirect("/settings/integrations?github=exchange_failed")
        connection = GitProviderConnection(
            provider=GitProviderConnection.Provider.GITHUB,
            web_url=settings.GITHUB_URL,
            api_url=settings.GITHUB_API_URL,
            app_id=str(data["id"]),
            app_slug=data.get("slug", ""),
            app_name=data.get("name", ""),
            app_html_url=data.get("html_url", ""),
            client_id=data.get("client_id", ""),
        )
        connection.set_secrets(
            private_key=data["pem"],
            webhook_secret=data.get("webhook_secret") or "",
            client_secret=data.get("client_secret") or "",
        )
        try:
            connection.save()
        except IntegrityError:
            return _dashboard_redirect("/settings/integrations?github=already_connected")
        record(
            "integration.github_connected", request=request, app_slug=connection.app_slug, method="manifest"
        )
        logger.info("github.app_created", app_id=connection.app_id, slug=connection.app_slug)
        return _dashboard_redirect("/settings/integrations?github=connected")


class GitHubInstalledView(APIView):
    """Step 3: GitHub's post-install ``setup_url``. Syncs the installation, then shows the repos."""

    permission_classes = [AllowAny]

    def get(self, request: Request) -> HttpResponseRedirect:
        if not _is_admin(request):
            return _dashboard_redirect("/login")
        connection = services.github_connection()
        installation_id = request.query_params.get("installation_id", "")
        if connection and installation_id.isdigit():
            try:
                services.sync_single_installation(connection, int(installation_id))
            except GitProviderError as exc:
                logger.warning("github.install_sync_failed", error=str(exc))
                return _dashboard_redirect("/repositories?sync=failed")
        return _dashboard_redirect("/repositories?installed=1")


class GitHubSyncView(APIView):
    permission_classes = [IsAdmin]

    @extend_schema(request=None)
    def post(self, request: Request) -> Response:
        connection = services.github_connection()
        if connection is None:
            raise NotFound("GitHub is not connected.")
        try:
            result = services.sync_github(connection)
        except GitProviderError as exc:
            raise ValidationError(f"Sync failed: {exc}") from exc
        record("integration.github_synced", request=request, result=result)
        return Response(result)


class RepositoryViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet[Repository],
):
    serializer_class = RepositorySerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = None
    http_method_names = ["get", "patch"]

    def get_queryset(self) -> QuerySet[Repository]:
        qs = Repository.objects.select_related(
            "installation__connection", "settings", "settings__credential"
        ).annotate(pull_request_count=Count("pull_requests"))
        params = self.request.query_params
        if params.get("enabled") in ("true", "false"):
            qs = qs.filter(enabled=params["enabled"] == "true")
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        elif self.action == "list":
            qs = qs.filter(status=Repository.Status.ACTIVE)
        if params.get("q"):
            qs = qs.filter(Q(full_name__icontains=params["q"]))
        return qs.order_by("-enabled", "full_name")

    def get_permissions(self) -> Any:
        # Reviewers may edit a repository's review rules; everything else on a repository is admin-only.
        if self.action == "repo_settings":
            return [IsReviewerOrReadOnly()]
        return super().get_permissions()

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        response = super().update(request, *args, **kwargs)
        if warning := getattr(self, "_webhook_warning", ""):
            response.data = {**response.data, "webhook_warning": warning}
        return response

    def perform_update(self, serializer: Any) -> None:
        was_enabled = serializer.instance.enabled
        repository = serializer.save()
        if repository.enabled and repository.installation.connection.provider == "gitlab":
            try:
                services.ensure_gitlab_webhook(repository)
            except services.WebhookSetupError as exc:
                # Reviews still work once someone adds the webhook by hand.
                self._webhook_warning = str(exc)
                logger.warning("gitlab.webhook_setup_failed", repository_id=repository.pk, error=str(exc))
        if repository.enabled != was_enabled:
            action_name = "repository.enabled" if repository.enabled else "repository.disabled"
            record(action_name, request=self.request, target=repository)
        logger.info("repository.updated", repository_id=repository.pk, enabled=repository.enabled)

    @extend_schema(request=RepositorySettingsSerializer, responses={200: RepositorySettingsSerializer})
    @action(detail=True, methods=["get", "patch"], url_path="settings")
    def repo_settings(self, request: Request, pk: str | None = None) -> Response:
        repository = self.get_object()
        repo_settings = services.ensure_settings(repository)
        if request.method == "GET":
            return Response(RepositorySettingsSerializer(repo_settings).data)
        user = cast(User, request.user)
        if not user.is_admin and isinstance(request.data, dict):
            restricted = sorted(set(request.data) - REVIEWER_SETTINGS_FIELDS)
            if restricted:
                raise PermissionDenied(f"Only admins can change: {', '.join(restricted)}.")
        before = repo_settings.snapshot()
        serializer = RepositorySettingsSerializer(repo_settings, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            serializer.save(updated_by=user)
            changes = diff(before, repo_settings.snapshot())
            if changes:
                record("repository.settings_changed", request=request, target=repository, changes=changes)
        return Response(serializer.data)


# --- GitLab (INT-03) ----------------------------------------------------------------------------


class GitLabConnectSerializer(serializers.Serializer[Any]):
    # Not URLField: self-managed instances often use internal host names without a dot (http://gitlab:8080).
    url = serializers.CharField(max_length=500, required=False, default="https://gitlab.com")
    token = serializers.CharField(max_length=500, trim_whitespace=True)

    def validate_url(self, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise serializers.ValidationError("Enter the GitLab address, e.g. https://gitlab.example.com.")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise serializers.ValidationError("Enter only the GitLab address, without credentials or query.")
        return value.strip().rstrip("/")


class GitLabIntegrationView(APIView):
    """Connects a GitLab instance (gitlab.com or self-managed) with a bot user's access token."""

    permission_classes = [IsAdmin]

    def get(self, request: Request) -> Response:
        connection = services.gitlab_connection()
        data: dict[str, Any] = {
            "connected": connection is not None,
            "webhook_url": services.gitlab_webhook_url(),
        }
        if connection:
            installation = connection.installations.filter(removed_at__isnull=True).first()
            data.update(
                web_url=connection.web_url,
                username=installation.account_login if installation else "",
                webhook_secret=connection.webhook_secret,  # admins need it to add webhooks by hand
                repositories=Repository.objects.filter(
                    installation__connection=connection, status=Repository.Status.ACTIVE
                ).count(),
            )
        return Response(data)

    @extend_schema(request=GitLabConnectSerializer, responses={201: None})
    def post(self, request: Request) -> Response:
        from apps.git_providers.gitlab.client import GitLabClient

        if services.gitlab_connection():
            raise Conflict("GitLab is already connected. Disconnect it first.")
        serializer = GitLabConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        web_url = serializer.validated_data["url"].rstrip("/")
        token = serializer.validated_data["token"]
        api_url = f"{web_url}/api/v4"
        client = GitLabClient(api_url, token)
        try:
            user = client.current_user()
            scopes = client.token_scopes()
        except GitProviderError as exc:
            raise ValidationError({"token": [f"GitLab rejected this token: {exc}"]}) from exc
        if scopes is not None and "api" not in scopes:
            raise ValidationError({"token": ["The token needs the 'api' scope."]})
        connection = GitProviderConnection(
            provider=GitProviderConnection.Provider.GITLAB,
            web_url=web_url,
            api_url=api_url,
            app_name=user.get("username", ""),
            app_html_url=user.get("web_url", ""),
        )
        connection.set_secrets(webhook_secret=secrets.token_urlsafe(32))
        connection.set_access_token(token)
        try:
            connection.save()
        except IntegrityError as exc:
            raise Conflict("GitLab is already connected.") from exc
        record(
            "integration.gitlab_connected",
            request=request,
            web_url=web_url,
            username=user.get("username", ""),
        )
        try:
            result = services.sync_gitlab(connection)
        except GitProviderError as exc:
            logger.warning("gitlab.initial_sync_failed", error=str(exc))
            result = {"installations": 0, "repositories": 0}
        return Response(result, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={204: None})
    def delete(self, request: Request) -> Response:
        connection = services.gitlab_connection()
        if connection is None:
            raise NotFound("GitLab is not connected.")
        connection.delete()
        record("integration.gitlab_disconnected", request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class GitLabSyncView(APIView):
    permission_classes = [IsAdmin]

    @extend_schema(request=None)
    def post(self, request: Request) -> Response:
        connection = services.gitlab_connection()
        if connection is None:
            raise NotFound("GitLab is not connected.")
        try:
            result = services.sync_gitlab(connection)
        except GitProviderError as exc:
            raise ValidationError(f"Sync failed: {exc}") from exc
        record("integration.gitlab_synced", request=request, result=result)
        return Response(result)
