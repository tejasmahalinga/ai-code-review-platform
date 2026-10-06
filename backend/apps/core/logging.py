"""Structured logging with secret redaction.

Every log line is passed through :func:`redact_secrets`, which masks values of sensitive keys and
anything that looks like a known credential format, so an accidental ``logger.info(..., key=...)``
does not leak a secret into log storage.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|authorization|private[_-]?key|cookie)", re.I
)
SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
]
REDACTED = "[REDACTED]"
# Keys whose names match SENSITIVE_KEY_RE but carry no secret (counts, ids, ...).
SAFE_KEYS = {"input_tokens", "output_tokens", "max_tokens", "token_count", "last4", "max_input_tokens"}


def redact_text(value: str) -> str:
    for pattern in SECRET_VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None and key not in SAFE_KEYS and SENSITIVE_KEY_RE.search(key):
        return REDACTED if value not in (None, "") else value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact(v) for v in value)
    return value


def redact(value: Any) -> Any:
    """Redacts secret-looking values and values under secret-looking keys (for logs and audit metadata)."""
    return _redact(value)


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key in {"timestamp", "level", "logger"}:
            continue
        event_dict[key] = _redact(event_dict[key], None if key == "event" else key)
    return event_dict


SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[*SHARED_PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=SHARED_PROCESSORS,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, redact_secrets, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("django.db.backends", "httpx", "httpx2", "httpcore", "httpcore2", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLevelName(level)))


def get_logger(name: str | None = None) -> Any:
    return structlog.stdlib.get_logger(name)
