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


def load_token_for_burner(
    settings: Settings,
    *,
    keyring_username: str,
    keyring_service: str | None = None,
) -> tuple[SecretStr, TokenSource]:
    """Resolve a SPECIFIC burner's token by overriding the keyring lookup
    keys. Used by the multi-burner orchestrator to load per-burner
    credentials without mutating the global Settings object.

    Falls back to env-var (`DISCORD_TOKEN_<USERNAME_UPPER>` first, then
    plain `DISCORD_TOKEN`) and finally to `auth.discord_token` config
    field — same three-tier semantics as `load_token`, but Tier-1 keyring
    parameters are caller-controlled.

    Per-burner env-var convention: `DISCORD_TOKEN_BURNER_1` resolves
    burner_id "burner-1" / "burner_1" / "BURNER-1" — case-insensitive
    after upper-casing and converting `-` → `_`.
    """
    _check_backend_not_plaintext()
    service = keyring_service or settings.auth.keyring_service

    # Tier 1 — keyring (per-burner)
    try:
        import keyring as _kr

        kr_token = _kr.get_password(service, keyring_username)
    except Exception as e:  # pragma: no cover
        logger.warning(
            "keyring_read_failed",
            keyring_username=keyring_username,
            err=str(e),
        )
        kr_token = None

    if kr_token:
        logger.info(
            "token_loaded",
            source=TokenSource.KEYRING.value,
            keyring_username=keyring_username,
        )
        return SecretStr(kr_token), TokenSource.KEYRING

    # Tier 2 — per-burner env first, then plain DISCORD_TOKEN
    per_burner_env = "DISCORD_TOKEN_" + keyring_username.upper().replace("-", "_")
    env_token = os.environ.get(per_burner_env) or os.environ.get("DISCORD_TOKEN")
    if env_token:
        logger.info(
            "token_loaded",
            source=TokenSource.ENV.value,
            keyring_username=keyring_username,
            env_var=per_burner_env if os.environ.get(per_burner_env) else "DISCORD_TOKEN",
        )
        return SecretStr(env_token), TokenSource.ENV

    # Tier 3 — config field (single shared)
    if settings.auth.discord_token is not None:
        logger.warning(
            "token_loaded_from_config",
            source=TokenSource.CONFIG.value,
            keyring_username=keyring_username,
        )
        return settings.auth.discord_token, TokenSource.CONFIG

    raise TokenNotFound(
        f"No burner token found for {keyring_username!r}. "
        f"Run `discord-scanner store-token --burner {keyring_username}` or set "
        f"{per_burner_env} / DISCORD_TOKEN env."
    )
