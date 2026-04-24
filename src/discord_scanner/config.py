"""Settings loader — YAML-first, env-overridable, security-validated.

Traces to:
- workplan.md Phase 1 task "Implement src/discord_scanner/config.py"
- seed-spec.md §5 (Config surface)
- security-model.md §6 SEC-P0-17 (URL allowlist), SEC-P0-23 (path-traversal),
  SEC-P0-08 (Chrome UA staleness), SEC-P0-25 (fingerprint single source),
  SEC-P0-26 (client_build_number plausible)

A single `load_config(path)` entry point is the only supported way to build
a `Settings` instance. It runs YAML parse → pydantic coerce → security
validations → path resolve. Any failure raises `ConfigError` (distinct from
pydantic.ValidationError so that the CLI can map to exit-code 1 cleanly).

Environment overrides (pydantic-settings):
- `DISCORD_TOKEN` — the burner token (plain env name, per SEC-P0-01 priority 2)
- `APP_LOG_LEVEL` — override `run.log_level`
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MIN_CLIENT_BUILD_NUMBER = 300_000
# SEC-P0-08: hard-fail if Chrome major < this. Bumped quarterly; last bump 2026-04.
# Today-aware staleness check (comparing user_agent_chrome_version against a live
# Chrome-stable probe) is tracked as TODO-P1-01 (deferred to Phase 1 monthly
# CI probe — see workplan.md).
_MIN_PLAUSIBLE_CHROME_MAJOR = 130


class ConfigError(RuntimeError):
    """Raised when config load / validation fails. Maps to CLI exit 1."""


class RunSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    log_level: str = "info"
    output_root: Path
    state_root: Path


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    token_source: Literal["keyring", "env", "config", "pool"] = "keyring"  # noqa: S105 — literal is a config mode, not a password
    token_pool: list[SecretStr] | None = None
    rotate_every: Literal["per_request", "per_server", "per_scan"] = "per_server"
    keyring_service: str = "discord-scanner"
    keyring_username: str = "burner-1"
    discord_token: SecretStr | None = None  # last-resort config.yaml source


class GatewaySettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    heartbeat_interval_ms: int | None = None
    presence: Literal["online", "idle", "dnd", "invisible"] = "online"
    resume_on_drop: bool = True


class HttpSettings(BaseModel):
    """Single source of truth for anti-detection fingerprint (SEC-P0-25)."""

    model_config = ConfigDict(extra="allow")
    http2: bool = True
    timeout_sec: int = 30
    per_host_rate_per_sec: dict[str, float] = Field(
        default_factory=lambda: {"discord.com/api": 2.0, "cdn.discordapp.com": 1.0}
    )
    per_channel_delay_sec: tuple[float, float] = (1.5, 4.0)
    burst_pause_sec: tuple[float, float] = (30.0, 90.0)
    max_messages_per_scan: int = 10_000
    user_agent_chrome_version: str = "134.0.0.0"
    fake_os: str = "Windows NT 10.0; Win64; x64"
    fake_os_platform: str = "Windows"
    locale: str = "en-US"
    timezone: str = "Europe/Prague"
    client_build_number: int = 350_000

    @field_validator("client_build_number")
    @classmethod
    def _build_plausible(cls, v: int) -> int:
        if v < _MIN_CLIENT_BUILD_NUMBER:
            raise ValueError(
                f"client_build_number {v} is implausible; must be >= {_MIN_CLIENT_BUILD_NUMBER}"
            )
        return v

    def fingerprint(self) -> dict[str, Any]:
        """Return the fingerprint blob used by BOTH REST X-Super-Properties
        and gateway OPCODE 2 IDENTIFY properties. Byte-for-byte parity is
        enforced by the caller; this helper guarantees both sides read from
        one config object (SEC-P0-25)."""
        return {
            "os": self.fake_os_platform,
            "browser": "Chrome",
            "browser_version": self.user_agent_chrome_version,
            "os_version": self.fake_os.split(";", 1)[0].replace("Windows NT ", ""),
            "device": "",
            "browser_user_agent": (
                f"Mozilla/5.0 ({self.fake_os}) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{self.user_agent_chrome_version} "
                "Safari/537.36"
            ),
            "system_locale": self.locale,
            "client_build_number": int(self.client_build_number),
            "release_channel": "stable",
        }


class RetrySettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    attempts: int = 5
    backoff_initial_sec: float = 2.0
    backoff_max_sec: float = 60.0
    retry_on_status: list[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])
    captcha_action: Literal["abort", "notify_and_wait"] = "abort"


class DiscoveryFilter(BaseModel):
    model_config = ConfigDict(extra="allow")
    min_score_pct: int = 70
    intent_allowlist: list[str] = Field(
        default_factory=lambda: ["prompt_sharing", "tutorials", "workflows", "collab"]
    )
    exclude_nsfw: bool = False


class DiscoverySettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    invites_input: Path
    filter: DiscoveryFilter = Field(default_factory=DiscoveryFilter)
    manual_invites: list[str] = Field(default_factory=list)


class AttachmentSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    download_images: bool = True
    image_extensions: list[str] = Field(
        default_factory=lambda: [".png", ".jpg", ".jpeg", ".webp", ".gif"]
    )
    max_size_mb: int = 20
    download_cdn_ua_matches_rest: bool = True


class DaemonSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    interval_hours: int = 168
    jitter_hours: tuple[int, int] = (-1, 2)
    scan_start_window: str = "02:00-06:00 UTC"


class RetentionSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    raw_dump_keep_days: int = 30
    attachment_keep_days: int = 14


class Settings(BaseSettings):
    """Top-level settings merged from YAML + env overrides.

    `repr()` MUST NOT leak the burner token (SEC-P0-05) — `SecretStr` masking
    handles this and we assert via test_token_never_in_repr.
    """

    model_config = SettingsConfigDict(
        env_file=None,  # YAML is primary; .env is only for local dev overrides
        env_file_encoding="utf-8",
        extra="allow",
    )

    run: RunSettings
    auth: AuthSettings = Field(default_factory=lambda: AuthSettings())
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    discovery: DiscoverySettings
    attachments: AttachmentSettings = Field(default_factory=AttachmentSettings)
    daemon: DaemonSettings = Field(default_factory=DaemonSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

    # Token from env var DISCORD_TOKEN — SEC-P0-01 priority 2.
    discord_token: SecretStr | None = Field(default=None)

    def __repr__(self) -> str:
        return (
            f"Settings(run={self.run!r}, auth=AuthSettings(..., token=***), "
            f"gateway={self.gateway!r}, http={self.http!r})"
        )


def _resolve_within(candidate: Path, project_root: Path) -> Path:
    """Resolve `candidate` and reject if it escapes `project_root` OR sits
    in a reserved system location. SEC-P0-23 requires `..` rejection AND
    we additionally block common OS-reserved roots on Windows + POSIX."""
    resolved = Path(candidate).expanduser().resolve()
    # Never allow reserved OS dirs as output or state roots.
    reserved_prefixes: list[Path] = []
    if os.name == "nt":
        for env in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)"):
            val = os.environ.get(env)
            if val:
                reserved_prefixes.append(Path(val).resolve())
        reserved_prefixes.append(Path("C:/Windows"))
    else:
        reserved_prefixes.extend(
            [
                Path("/etc"),
                Path("/bin"),
                Path("/sbin"),
                Path("/usr/bin"),
                Path("/usr/sbin"),
                Path("/boot"),
                Path("/sys"),
                Path("/proc"),
            ]
        )
    for reserved in reserved_prefixes:
        try:
            resolved.relative_to(reserved)
        except ValueError:
            continue
        raise ConfigError(
            f"path {resolved} is inside reserved OS location {reserved}; "
            "refusing to write there (SSRF / path-traversal guard)"
        )
    # `..` already collapsed by resolve(); reject if still present in str form
    if ".." in str(candidate):
        raise ConfigError(
            f"path {candidate} contains '..' traversal; refusing (SSRF / path-traversal guard)"
        )
    return resolved


def _validate_chrome_ua(version: str) -> None:
    """SEC-P0-08: hard-fail if the configured Chrome version is implausibly old.

    Phase 1 enforces only a compiled-in floor (MIN_PLAUSIBLE_CHROME_MAJOR).
    The dynamic horizon ("reject UA more than N days behind live Chrome-stable")
    is TODO-P1-01: it requires a monthly CI probe against the Chrome release
    channel and is tracked in workplan.md.
    """
    try:
        major = int(version.split(".", 1)[0])
    except (ValueError, AttributeError) as e:
        raise ConfigError(f"user_agent_chrome_version {version!r} unparseable") from e
    if major < _MIN_PLAUSIBLE_CHROME_MAJOR:
        raise ConfigError(
            f"stale Chrome UA: version {version} major={major} is older than the "
            f"minimum plausible {_MIN_PLAUSIBLE_CHROME_MAJOR}. Bump "
            "http.user_agent_chrome_version in config.yaml. (SEC-P0-08)"
        )


def load_config(path: Path | str) -> Settings:
    """Load and validate a config.yaml.

    Raises:
        ConfigError: on missing file, YAML parse error, path-traversal,
            stale UA, implausible build number, or any validation failure.
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")

    # Early path-traversal check BEFORE pydantic coerces (so our error message wins).
    run_block = raw.get("run", {})
    if not isinstance(run_block, dict):
        raise ConfigError("run: must be a mapping")
    project_root = p.parent.resolve()
    for key in ("output_root", "state_root"):
        raw_val = run_block.get(key)
        if raw_val is None:
            continue
        if ".." in str(raw_val):
            raise ConfigError(
                f"run.{key}={raw_val!r} contains '..' traversal; refusing (SEC-P0-23)"
            )
        # resolve relative to config file dir for relative paths
        candidate = Path(raw_val)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        _resolve_within(candidate, project_root)

    # Chrome UA staleness (SEC-P0-08) — checked before pydantic coercion for clearer errors.
    http_block = raw.get("http", {})
    if isinstance(http_block, dict):
        ua_ver = http_block.get("user_agent_chrome_version")
        if ua_ver:
            _validate_chrome_ua(str(ua_ver))

    try:
        settings = Settings(**raw)
    except Exception as e:  # pydantic.ValidationError or TypeError
        raise ConfigError(f"config validation failed: {e}") from e

    return settings
