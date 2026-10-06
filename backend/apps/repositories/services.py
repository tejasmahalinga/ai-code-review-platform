from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.logging import get_logger
from apps.credentials.models import LLMCredential
from apps.repositories.models import GitProviderConnection, Installation, Repository, RepositorySettings

logger = get_logger(__name__)


def github_connection() -> GitProviderConnection | None:
    return GitProviderConnection.objects.filter(provider=GitProviderConnection.Provider.GITHUB).first()


def ensure_settings(repository: Repository) -> RepositorySettings:
    try:
        return repository.settings
    except RepositorySettings.DoesNotExist:
        default_credential = (
            LLMCredential.objects.filter(status=LLMCredential.Status.VALID).order_by("id").first()
        )
        return RepositorySettings.objects.create(repository=repository, credential=default_credential)


def upsert_installation(connection: GitProviderConnection, data: dict[str, Any]) -> Installation:
    account = data.get("account") or {}
    installation, _ = Installation.objects.update_or_create(
        connection=connection,
        external_id=int(data["id"]),
        defaults={
            "account_login": account.get("login", ""),
            "account_type": account.get("type", ""),
            "suspended_at": timezone.now() if data.get("suspended_at") else None,
            "removed_at": None,
        },
    )
    return installation


def upsert_repository(installation: Installation, data: dict[str, Any]) -> Repository:
    full_name = data.get("full_name") or f"{installation.account_login}/{data['name']}"
    defaults: dict[str, Any] = {
        "full_name": full_name,
        "private": bool(data.get("private", True)),
        "status": Repository.Status.ACTIVE,
    }
    if data.get("default_branch"):
        defaults["default_branch"] = data["default_branch"]
    defaults["html_url"] = data.get("html_url") or f"{installation.connection.web_url}/{full_name}"
    repository, _ = Repository.objects.update_or_create(
        installation=installation, external_id=int(data["id"]), defaults=defaults
    )
    ensure_settings(repository)
    return repository


def mark_repositories_removed(installation: Installation, external_ids: list[int]) -> int:
    return Repository.objects.filter(installation=installation, external_id__in=external_ids).update(
        status=Repository.Status.REMOVED, enabled=False
    )


def remove_installation(installation: Installation) -> None:
    installation.removed_at = timezone.now()
    installation.save(update_fields=["removed_at"])
    installation.repositories.update(status=Repository.Status.REMOVED, enabled=False)


@transaction.atomic
def sync_installation_repositories(installation: Installation, repos: list[dict[str, Any]]) -> None:
    seen = {int(r["id"]) for r in repos}
    for repo in repos:
        upsert_repository(installation, repo)
    stale = installation.repositories.exclude(external_id__in=seen).values_list("external_id", flat=True)
    mark_repositories_removed(installation, list(stale))


def sync_github(connection: GitProviderConnection, http_client: Any = None) -> dict[str, int]:
    """Pulls installations and their repositories from GitHub and reconciles local state."""
    from apps.git_providers.github.app import app_client

    client = app_client(connection, http_client)
    remote = client.list_installations()
    remote_ids = set()
    repo_count = 0
    for data in remote:
        installation = upsert_installation(connection, data)
        remote_ids.add(installation.external_id)
        if installation.suspended_at:
            continue
        repos = client.for_installation(installation.external_id).list_repositories()
        sync_installation_repositories(installation, repos)
        repo_count += len(repos)
    for installation in connection.installations.filter(removed_at__isnull=True).exclude(
        external_id__in=remote_ids
    ):
        remove_installation(installation)
    logger.info("github.synced", installations=len(remote_ids), repositories=repo_count)
    return {"installations": len(remote_ids), "repositories": repo_count}


def sync_single_installation(
    connection: GitProviderConnection, installation_id: int, http_client: Any = None
) -> Installation:
    from apps.git_providers.github.app import app_client

    client = app_client(connection, http_client)
    installation = upsert_installation(connection, client.get_installation(installation_id))
    if not installation.suspended_at:
        repos = client.for_installation(installation.external_id).list_repositories()
        sync_installation_repositories(installation, repos)
    return installation


# --- GitLab (INT-03) ----------------------------------------------------------------------------


def gitlab_connection() -> GitProviderConnection | None:
    return GitProviderConnection.objects.filter(provider=GitProviderConnection.Provider.GITLAB).first()


def gitlab_webhook_url() -> str:
    from django.conf import settings

    return f"{settings.PUBLIC_URL}/webhooks/gitlab"


def sync_gitlab(connection: GitProviderConnection, http_client: Any = None) -> dict[str, int]:
    """Mirrors the projects the bot user can review. GitLab has no installations, so one pseudo-installation
    (the bot user) holds every project."""
    from apps.git_providers.gitlab.client import client_for_connection

    client = client_for_connection(connection, http_client)
    user = client.current_user()
    installation, _ = Installation.objects.update_or_create(
        connection=connection,
        external_id=int(user["id"]),
        defaults={"account_login": user.get("username", ""), "account_type": "User", "removed_at": None},
    )
    connection.installations.exclude(pk=installation.pk).filter(removed_at__isnull=True).update(
        removed_at=timezone.now()
    )
    repos = [
        {
            "id": p["id"],
            "full_name": p["path_with_namespace"],
            "name": p.get("path", ""),
            "private": p.get("visibility", "private") != "public",
            "default_branch": p.get("default_branch") or "",
            "html_url": p.get("web_url", ""),
        }
        for p in client.list_projects()
    ]
    sync_installation_repositories(installation, repos)
    logger.info("gitlab.synced", repositories=len(repos))
    return {"installations": 1, "repositories": len(repos)}


class WebhookSetupError(Exception):
    pass


def ensure_gitlab_webhook(repository: Repository, http_client: Any = None) -> str:
    """Creates the project webhook if it does not exist yet. Needs the Maintainer role on the project."""
    from apps.git_providers.base import GitProviderError
    from apps.git_providers.gitlab.client import client_for_connection

    connection = repository.installation.connection
    client = client_for_connection(connection, http_client)
    try:
        if repository.webhook_id and client.hook_exists(repository.full_name, repository.webhook_id):
            return repository.webhook_id
        hook_id = client.create_project_hook(
            repository.full_name, gitlab_webhook_url(), connection.webhook_secret
        )
    except GitProviderError as exc:
        if exc.status in (401, 403):
            raise WebhookSetupError(
                "The GitLab bot user needs the Maintainer role on this project to add the webhook. Grant it, "
                "or add the webhook by hand (Settings → Webhooks, URL and secret token from the Integrations "
                "page, events: merge requests and comments)."
            ) from exc
        raise WebhookSetupError(f"GitLab refused to create the webhook: {exc}") from exc
    repository.webhook_id = hook_id
    repository.save(update_fields=["webhook_id"])
    logger.info("gitlab.webhook_created", repository_id=repository.pk, hook_id=hook_id)
    return hook_id
