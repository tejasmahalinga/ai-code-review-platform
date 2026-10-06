"""Sends a message to one channel. Raises ``TransientError`` when a retry may help."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx2
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from apps.notifications.messages import Message, to_email, to_slack, to_webhook
from apps.notifications.models import NotificationChannel
from apps.notifications.urlguard import check_url

TIMEOUT_SECONDS = 10


class DeliveryError(Exception):
    pass


class TransientError(DeliveryError):
    pass


def http_client() -> httpx2.Client:
    # Redirects are not followed: a redirect could point at an address the URL check would reject.
    return httpx2.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)


def send(channel: NotificationChannel, message: Message) -> None:
    if channel.kind == NotificationChannel.Kind.EMAIL:
        _send_email(channel, message)
    elif channel.kind == NotificationChannel.Kind.SLACK:
        _post(channel, json.dumps(to_slack(message)).encode(), {}, slack=True)
    else:
        body = json.dumps(to_webhook(message, timezone.now().isoformat())).encode()
        headers = {"X-Reviewbot-Event": message.event}
        if secret := channel.secret:
            digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Reviewbot-Signature-256"] = f"sha256={digest}"
        _post(channel, body, headers, slack=False)


def _send_email(channel: NotificationChannel, message: Message) -> None:
    if not settings.EMAIL_ENABLED:
        raise DeliveryError("Email is not configured (set REVIEWBOT_EMAIL_HOST).")
    if not channel.recipients:
        raise DeliveryError("The channel has no recipients.")
    subject, body = to_email(message)
    try:
        send_mail(subject=subject, message=body, from_email=None, recipient_list=list(channel.recipients))
    except OSError as exc:
        raise TransientError(f"SMTP error: {type(exc).__name__}") from exc


def _post(channel: NotificationChannel, body: bytes, headers: dict[str, str], *, slack: bool) -> None:
    url = channel.url
    try:
        check_url(url, slack=slack)
    except ValueError as exc:
        raise DeliveryError(str(exc)) from exc
    try:
        with http_client() as client:
            response = client.post(
                url,
                content=body,
                headers={"Content-Type": "application/json", "User-Agent": "Reviewbot", **headers},
            )
    except httpx2.HTTPError as exc:
        raise TransientError(f"Request failed: {type(exc).__name__}") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(f"HTTP {response.status_code}")
    if response.status_code >= 300:
        raise DeliveryError(f"HTTP {response.status_code}: {response.text[:200]}")
