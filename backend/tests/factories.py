"""Small helpers to build test data without network access."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from apps.git_providers.base import ChangedFile, PullRequestInfo
from apps.llm.base import InvalidResponse, LLMError, LLMResponse, TokenUsage
from apps.repositories.models import GitProviderConnection, Installation, Repository
from apps.repositories.services import ensure_settings
from apps.reviews.models import PullRequest

_ids = itertools.count(1000)
WEBHOOK_SECRET = "whsec-test-secret"


def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


_PEM_CACHE: list[str] = []


def cached_pem() -> str:
    if not _PEM_CACHE:
        _PEM_CACHE.append(rsa_private_key_pem())
    return _PEM_CACHE[0]


def make_connection() -> GitProviderConnection:
    connection = GitProviderConnection(
        provider="github",
        web_url="https://github.com",
        api_url="https://api.github.com",
        app_id="12345",
        app_slug="reviewbot-test",
        app_name="Reviewbot Test",
    )
    connection.set_secrets(private_key=cached_pem(), webhook_secret=WEBHOOK_SECRET)
    connection.save()
    return connection


def make_installation(connection: GitProviderConnection | None = None, external_id: int = 77) -> Installation:
    connection = connection or GitProviderConnection.objects.first() or make_connection()
    return Installation.objects.create(connection=connection, external_id=external_id, account_login="acme")


def make_repository(
    *,
    credential: Any = None,
    enabled: bool = True,
    installation: Installation | None = None,
    name: str | None = None,
    **settings_overrides: Any,
) -> Repository:
    installation = installation or Installation.objects.first() or make_installation()
    external_id = next(_ids)
    repo = Repository.objects.create(
        installation=installation,
        external_id=external_id,
        full_name=f"acme/{name or f'repo{external_id}'}",
        enabled=enabled,
    )
    settings = ensure_settings(repo)
    settings.credential = credential
    for key, value in settings_overrides.items():
        setattr(settings, key, value)
    settings.save()
    repo.refresh_from_db()
    return repo


def make_pull_request(repository: Repository, number: int = 1, **kwargs: Any) -> PullRequest:
    defaults = {"title": "Add feature", "author_login": "octocat", "head_sha": "a" * 40, "state": "open"}
    defaults.update(kwargs)
    return PullRequest.objects.create(repository=repository, number=number, **defaults)


def pr_info(number: int = 1, head_sha: str = "a" * 40, **kwargs: Any) -> PullRequestInfo:
    defaults: dict[str, Any] = {
        "number": number,
        "title": "Add feature",
        "author_login": "octocat",
        "state": "open",
        "is_draft": False,
        "base_ref": "main",
        "head_ref": "feature",
        "base_sha": "b" * 40,
        "head_sha": head_sha,
        "html_url": f"https://github.com/acme/repo/pull/{number}",
    }
    defaults.update(kwargs)
    return PullRequestInfo(**defaults)


def added_file(path: str, lines: list[str], start: int = 1, status: str = "added") -> ChangedFile:
    patch = f"@@ -0,0 +{start},{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
    return ChangedFile(path=path, status=status, additions=len(lines), deletions=0, patch=patch)


def finding(
    path: str,
    line: int,
    *,
    severity: str = "high",
    category: str = "bug",
    title: str = "Problem",
    confidence: float = 0.9,
    line_start: int | None = None,
    suggestion: str | None = None,
) -> dict[str, Any]:
    return {
        "path": path,
        "line_start": line_start if line_start is not None else line,
        "line_end": line,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "body": f"Explanation for {title}.",
        "suggestion": suggestion,
    }


class ScriptedLLM:
    """LLMProvider stand-in. ``script`` returns the JSON dict (or raises) for each call."""

    name = "scripted"

    def __init__(self, script: Callable[[str, str], dict[str, Any]] | list[Any], model: str = "test-model"):
        self.model = model
        self.calls: list[dict[str, str]] = []
        self._script = script
        self._lock = __import__("threading").Lock()

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResponse:
        with self._lock:
            self.calls.append({"system": system, "user": user})
            if callable(self._script):
                item: Any = self._script(system, user)
            else:
                item = self._script.pop(0)
        if isinstance(item, LLMError):
            raise item
        if isinstance(item, str):
            raise InvalidResponse("not json", usage=TokenUsage(10, 5), raw_text=item)
        return LLMResponse(data=item, raw_text=str(item), usage=TokenUsage(100, 20), model=self.model)

    def validate(self) -> None:
        return None


class EnqueueRecorder(list):
    """Stand-in for ``apps.reviews.services.enqueue``: records run ids (and countdowns separately)."""

    def __init__(self) -> None:
        super().__init__()
        self.countdowns: list[float] = []

    def __call__(self, run_id: int, countdown: float = 0) -> None:
        self.append(run_id)
        self.countdowns.append(countdown)
