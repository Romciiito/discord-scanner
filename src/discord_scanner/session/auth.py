"""Burner-token loader with priority keyring → env → config.

Traces to:
- security-model.md §6 SEC-P0-01 (three-source priority), SEC-P0-05 (never logged),
  SEC-P0-06 (plaintext-fallback refusal)
- seed-spec.md §3 (token storage)
- claude-rules.md MUST list "Token storage"
"""

from __future__ import annotations

import os
from enum import StrEnum

from pydantic import SecretStr

from discord_scanner.config import Settings
from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)


class TokenSource(StrEnum):
    """Which of the three priority tiers produced the loaded token."""

    KEYRING = "keyring"
    ENV = "env"
    CONFIG = "config"


class PlaintextKeyringRefused(RuntimeError):
    """Raised when keyring.get_keyring() returns a plaintext backend.

    SEC-P0-06: we refuse to touch a plaintext keyring even to read an existing
    entry, so a compromised disk cannot escalate via `keyrings.alt`.
    """


class TokenNotFound(RuntimeError):
    """All three sources yielded no token. The caller should exit 1."""


_PLAINTEXT_MODULE_PREFIX = "keyrings.alt."


def is_plaintext_keyring_backend(backend: object) -> bool:
    """Single source of truth for SEC-P0-06 plaintext-backend detection.

    Returns True if `backend` is a `keyrings.alt.*` module class OR has
    `Plaintext` anywhere in its class name. Used by both `_check_backend_not_plaintext`
    here and the `store-token` CLI command — keeping one predicate avoids
    drift between read- and write-side checks.
    """
    cls = type(backend)
    name = cls.__name__
    module = cls.__module__
    return "Plaintext" in name or module.startswith(_PLAINTEXT_MODULE_PREFIX)


def _check_backend_not_plaintext() -> None:
    """Inspect the active keyring backend; raise if it is a plaintext variant."""
    try:
        import keyring as _kr
    except ImportError as e:  # pragma: no cover — dev dep
        raise TokenNotFound("keyring not importable") from e

    backend = _kr.get_keyring()
    if is_plaintext_keyring_backend(backend):
        cls = type(backend)
        raise PlaintextKeyringRefused(
            f"refusing plaintext keyring backend {cls.__module__}.{cls.__name__}. "
            "Install a platform-native backend "
            "(Windows Credential Manager / macOS Keychain / Linux Secret Service)."
        )


def load_token(settings: Settings) -> tuple[SecretStr, TokenSource]:
    """Resolve the burner token using the three-tier priority.

    Order (highest priority first):
      1. keyring (service=settings.auth.keyring_service, username=settings.auth.keyring_username)
      2. env var `DISCORD_TOKEN`
      3. config.yaml `auth.discord_token` (last resort, logs a warning)

    Raises:
        PlaintextKeyringRefused: backend is plaintext (SEC-P0-06).
        TokenNotFound: all three sources yielded nothing.

    The returned token is wrapped in `SecretStr`; logs log only `TokenSource`,
    never the token value (SEC-P0-05).
    """
    _check_backend_not_plaintext()

    # Tier 1 — keyring
    try:
        import keyring as _kr

        kr_token = _kr.get_password(
            settings.auth.keyring_service,
            settings.auth.keyring_username,
        )
    except Exception as e:  # pragma: no cover — keyring backend-specific
        logger.warning("keyring_read_failed", err=str(e))
        kr_token = None

    if kr_token:
        logger.info("token_loaded", source=TokenSource.KEYRING.value)
        return SecretStr(kr_token), TokenSource.KEYRING

    # Tier 2 — env
    env_token = os.environ.get("DISCORD_TOKEN")
    if env_token:
        logger.info("token_loaded", source=TokenSource.ENV.value)
        return SecretStr(env_token), TokenSource.ENV

    # Tier 3 — config (last resort)
    if settings.auth.discord_token is not None:
        logger.warning(
            "token_loaded_from_config",
            source=TokenSource.CONFIG.value,
            advisory="prefer keyring or DISCORD_TOKEN env",
        )
        return settings.auth.discord_token, TokenSource.CONFIG

    raise TokenNotFound(
        "No burner token found. Run `discord-scanner store-token` or set DISCORD_TOKEN."
    )
