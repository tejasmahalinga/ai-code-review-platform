"""Returns the :class:`GitProvider` for a repository. Tests swap the factory for a fake."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from apps.git_providers.base import GitProvider

if TYPE_CHECKING:
    from apps.repositories.models import Repository


def _default_factory(repository: Repository) -> GitProvider:
    from apps.git_providers.github.app import installation_client

    connection = repository.installation.connection
    provider = connection.provider
    if provider == "github":
        return installation_client(repository.installation)
    if provider == "gitlab":
        from apps.git_providers.gitlab.client import client_for_connection

        return client_for_connection(connection)
    raise NotImplementedError(f"Git provider '{provider}' is not supported yet")


_factory: Callable[[Repository], GitProvider] = _default_factory


def provider_for(repository: Repository) -> GitProvider:
    return _factory(repository)


def set_factory(factory: Callable[[Repository], GitProvider] | None) -> None:
    global _factory
    _factory = factory or _default_factory
