"""Outbound URL checks for notification webhooks (SSRF protection).

Admins configure webhook URLs, but a URL pointing at the cluster's internal network could still be abused to
reach internal services. Only HTTPS URLs that resolve to public addresses are allowed, unless
``REVIEWBOT_ALLOW_PRIVATE_WEBHOOKS`` is set (for on-premise targets such as an internal chat server).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from django.conf import settings

SLACK_HOSTS = {"hooks.slack.com"}


class UnsafeURL(ValueError):
    pass


def check_url(url: str, *, slack: bool = False) -> None:
    parts = urlsplit(url)
    allow_private = settings.ALLOW_PRIVATE_WEBHOOKS
    if parts.scheme != "https" and not (allow_private and parts.scheme == "http"):
        raise UnsafeURL("The URL must start with https://.")
    host = parts.hostname or ""
    if not host or parts.username or parts.password:
        raise UnsafeURL("The URL must have a host name and no credentials.")
    if slack and host not in SLACK_HOSTS:
        raise UnsafeURL("Use a Slack incoming webhook URL (https://hooks.slack.com/services/...).")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"The host {host} cannot be resolved.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise UnsafeURL(f"The host {host} resolves to a private or reserved address ({address}).")
