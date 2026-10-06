"""Channel-independent notification messages and their Slack, email and webhook renderings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Message:
    event: str
    title: str
    text: str = ""
    url: str = ""
    level: str = "info"  # info | warning | danger
    fields: list[tuple[str, str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # bullet list, e.g. top PRs in the digest
    data: dict[str, Any] = field(default_factory=dict)  # machine-readable payload for webhooks

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Message:
        raw = dict(raw)
        raw["fields"] = [tuple(f) for f in raw.get("fields", [])]
        return cls(**raw)


SLACK_COLORS = {"info": "#0ea5e9", "warning": "#f59e0b", "danger": "#dc2626"}


def slack_escape(text: str) -> str:
    """Escapes Slack control characters so PR titles cannot inject mentions (<!channel>) or links."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_slack(message: Message) -> dict[str, Any]:
    title = slack_escape(message.title)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
    ]
    if message.text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": slack_escape(message.text)}})
    if message.fields:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{slack_escape(label)}*\n{slack_escape(value)}"}
                    for label, value in message.fields[:10]
                ],
            }
        )
    if message.lines:
        bullets = "\n".join(f"• {slack_escape(line)}" for line in message.lines[:10])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": bullets}})
    if message.url:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{message.url}|Open in Reviewbot>"}],
            }
        )
    return {
        "text": message.title,  # notification fallback
        "attachments": [{"color": SLACK_COLORS.get(message.level, SLACK_COLORS["info"]), "blocks": blocks}],
    }


def to_email(message: Message) -> tuple[str, str]:
    body = [message.title, ""]
    if message.text:
        body += [message.text, ""]
    body += [f"{label}: {value}" for label, value in message.fields]
    if message.lines:
        body += ["", *[f"- {line}" for line in message.lines]]
    if message.url:
        body += ["", message.url]
    return f"[Reviewbot] {message.title}", "\n".join(body)


def to_webhook(message: Message, sent_at: str) -> dict[str, Any]:
    return {
        "event": message.event,
        "title": message.title,
        "text": message.text,
        "url": message.url,
        "level": message.level,
        "data": message.data,
        "sent_at": sent_at,
    }
