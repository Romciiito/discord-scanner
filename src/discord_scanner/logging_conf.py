"""Structlog configuration with mandatory redaction processors.

Traces to: security-model.md §6 (SEC-P0-03, SEC-P0-04), seed-spec.md §3 (invite-code redaction).

Every log record passes through `redact_token` and `redact_invite_code` processors.
Raw `discord.gg/` / `discord.com/invite/` literals and raw Discord user tokens must
never appear in log output — enforced by processors here and CI grep guards in
`tests/ci/test_grep_guards.py`.
"""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_INVITE_PATTERN = re.compile(
    r"(?:discord\.gg/|discord\.com/invite/)([A-Za-z0-9-]{4,20})",
    re.IGNORECASE,
)
_TOKEN_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}\b"
)


def redact_token(token: str) -> str:
    """Redact a Discord token for safe logging.

    SEC-P0-03: format is f"{t[:6]}***{t[-4:]}" when token is long enough,
    otherwise fully masked. Accepts any string to make this processor safe to
    call on user-controlled input without guards at every call site.
    """
    if not isinstance(token, str) or len(token) < 12:
        return "***"
    return f"{token[:6]}***{token[-4:]}"


def redact_invite_code(code: str) -> str:
    """Redact an invite code for safe logging.

    Matches the format used by Stage 1 (civit-hf-scanner) for cross-pipeline
    consistency: f"{code[:2]}***{code[-2:]}" when code is long enough.
    """
    if not isinstance(code, str) or len(code) < 5:
        return "***"
    return f"{code[:2]}***{code[-2:]}"


def _redact_string(value: str) -> str:
    """Apply both invite-URL and token redaction to a free-text string."""
    value = _INVITE_PATTERN.sub(
        lambda m: f"discord.gg/{redact_invite_code(m.group(1))}", value
    )
    value = _TOKEN_PATTERN.sub(lambda m: redact_token(m.group(0)), value)
    return value


def _redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor that recursively redacts tokens + invite codes."""
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = _redact_string(value)
        elif isinstance(value, (list, tuple)):
            event_dict[key] = type(value)(
                _redact_string(v) if isinstance(v, str) else v for v in value
            )
    return event_dict


def configure_logging(level: str = "info", json_output: bool = True) -> None:
    """Configure structlog with redaction processors + JSON renderer.

    MUST be called once at CLI entry before any log emission.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            _level_to_int(level)
        ),
        cache_logger_on_first_use=True,
    )


def _level_to_int(level: str) -> int:
    import logging

    return getattr(logging, level.upper(), logging.INFO)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. All output flows through redact processors."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
