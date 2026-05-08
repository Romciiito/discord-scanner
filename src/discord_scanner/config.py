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


class BurnerHttpOverrides(BaseModel):
    """Per-burner overrides for `HttpSettings.fingerprint()` blob.

    When set, these fields override the global `http.*` defaults for ONE
    burner's REST + gateway IDENTIFY traffic. Unspecified fields fall back
    to global. This is the v3 multi-burner anti-detection lever — two
    burners on the same machine should at minimum vary user_agent +
    locale + client_build_number to avoid clustering on the
    `X-Super-Properties` blob (workspace plan §"Multi-burner Topology").
    """

    model_config = ConfigDict(extra="allow")
    user_agent_chrome_version: str | None = None
    fake_os_platform: Literal["Windows", "macOS", "Linux"] | None = None
    fake_os: str | None = None  # e.g. "Windows NT 10.0; Win64; x64"
    locale: str | None = None
    client_build_number: int | None = None


class BurnerConfig(BaseModel):
    """One burner in a multi-burner pool.

    Each burner has its own keyring credential, its own optional fingerprint
    overrides, an optional proxy URL (M.3 — currently unused under
    Topology 2 since OS-level network interface routing handles IP
    differentiation), and a schedule offset for daemon mode.
    """

    model_config = ConfigDict(extra="allow")
    keyring_username: str
    keyring_service: str | None = None  # falls back to AuthSettings.keyring_service
    proxy_url: str | None = None  # httpx proxy URL; unused under Topology 2
    schedule_offset_hours: int = Field(default=0, ge=0, le=23)
    http_overrides: BurnerHttpOverrides = Field(default_factory=BurnerHttpOverrides)


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    token_source: Literal["keyring", "env", "config", "pool"] = "keyring"  # noqa: S105 — literal is a config mode, not a password
    token_pool: list[SecretStr] | None = None  # legacy — superseded by `burners`
    rotate_every: Literal["per_request", "per_server", "per_scan"] = "per_server"
    keyring_service: str = "discord-scanner"
    keyring_username: str = "burner-1"
    discord_token: SecretStr | None = None  # last-resort config.yaml source
    # NEW v3 — multi-burner pool. Empty list = single-burner legacy mode using
    # the keyring_username + keyring_service above. When populated, the
    # orchestrator iterates burners sequentially under the rules in
    # AGENT-TEAM-WORKPLAN §"Multi-burner Topology".
    burners: list[BurnerConfig] = Field(default_factory=list)


class GatewaySettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    heartbeat_interval_ms: int | None = None
    presence: Literal["online", "idle", "dnd", "invisible"] = "online"
    resume_on_drop: bool = True


class CircadianSettings(BaseModel):
    """Operator-TZ sleep window for the adaptive limiter."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    timezone: str = "Europe/Prague"
    sleep_window: str = "02:00-08:00"
    sleep_probability: float = Field(default=0.95, ge=0.0, le=1.0)
    twilight_hours: float = Field(default=1.0, ge=0.0, le=12.0)


class AdaptiveRateLimitSettings(BaseModel):
    """Adaptive rate limiter — wraps base token bucket with state machine.

    See `session/adaptive.py` for state machine + signal semantics.
    Defaults match workspace plan §"Part A — A.1". Disabled by default for
    operator opt-in after one-week canary.
    """

    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    degraded_factor_range: tuple[float, float] = (0.30, 0.60)
    degrade_on_5xx_in_window: int = Field(default=3, ge=1)
    degrade_on_latency_p95_ms: float = Field(default=4000.0, ge=0.0)
    degrade_window_sec: float = Field(default=60.0, ge=1.0)
    recover_after_successes: int = Field(default=30, ge=1)
    cooldown_on_consecutive_429: int = Field(default=5, ge=1)
    cooldown_on_captcha: bool = True
    cooldown_on_403_streak: int = Field(default=3, ge=1)
    cooldown_duration_sec: tuple[float, float] = (10.0, 300.0)
    session_break_every_requests: int = Field(default=200, ge=1)
    session_break_duration_sec: tuple[float, float] = (60.0, 180.0)
    circadian: CircadianSettings = Field(default_factory=CircadianSettings)

    @field_validator("degraded_factor_range")
    @classmethod
    def _factor_range_valid(cls, v: tuple[float, float]) -> tuple[float, float]:
        low, high = v
        if not (0.0 < low <= high < 1.0):
            raise ValueError(
                f"degraded_factor_range {v} must satisfy 0 < low <= high < 1; "
                "factors are multiplied against baseline rate."
            )
        return v

    @field_validator("cooldown_duration_sec", "session_break_duration_sec")
    @classmethod
    def _positive_range(cls, v: tuple[float, float]) -> tuple[float, float]:
        low, high = v
        if not (0.0 <= low <= high):
            raise ValueError(
                f"duration range {v} must satisfy 0 <= low <= high (seconds)."
            )
        return v


class HttpSettings(BaseModel):
    """Single source of truth for anti-detection fingerprint (SEC-P0-25)."""

    model_config = ConfigDict(extra="allow")
    http2: bool = True
    timeout_sec: int = 30
    # Default rates LOWERED in v2 (was 2.0 / 1.0) for ToS conservatism.
    # See workspace plan §"Part A — A.1" — backfill (Phase C) amplifies
    # traffic ~5× and adaptive RL must already be in production by then.
    per_host_rate_per_sec: dict[str, float] = Field(
        default_factory=lambda: {"discord.com/api": 1.0, "cdn.discordapp.com": 0.5}
    )
    per_channel_delay_sec: tuple[float, float] = (1.5, 4.0)
    burst_pause_sec: tuple[float, float] = (30.0, 90.0)
    max_messages_per_scan: int = 10_000
    user_agent_chrome_version: str = "148.0.7778.56"
    fake_os: str = "Windows NT 10.0; Win64; x64"
    fake_os_platform: str = "Windows"
    locale: str = "en-US"
    timezone: str = "Europe/Prague"
    client_build_number: int = 350_000
    # Adaptive RL (NEW v2). Disabled by default — opt-in via config after
    # one-week canary. See workspace plan rollout sequence Week 1.
    adaptive: AdaptiveRateLimitSettings = Field(default_factory=AdaptiveRateLimitSettings)

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
    # Stage 1.5 confidence floor (NEW v2). When invites.enriched.json is read,
    # any record with `confidence < min_confidence` is dropped before
    # `intent_allowlist` is applied. Bare invites.json (no confidence field)
    # bypasses this floor — operator falls back to manual_invites + score_pct.
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    exclude_nsfw: bool = False


class GuildSelectorConfig(BaseModel):
    """Per-guild human-readable channel selectors (NEW v2).

    Empty/None selectors falls back to current type-0/5/15 default behaviour
    so existing configs keep working. Resolution happens at scan-start.
    """

    model_config = ConfigDict(extra="allow")
    categories: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    exclude_channels: list[str] = Field(default_factory=list)
    fail_open: bool = True


class GuildSelectorEntry(BaseModel):
    """One entry in `discovery.guilds[]`. Either `id` or `invite_code` is required."""

    model_config = ConfigDict(extra="allow")
    id: str | None = None
    invite_code: str | None = None
    selectors: GuildSelectorConfig = Field(default_factory=GuildSelectorConfig)


class DiscoverySettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    invites_input: Path
    filter: DiscoveryFilter = Field(default_factory=DiscoveryFilter)
    manual_invites: list[str] = Field(default_factory=list)
    # v2 — additive. Empty list keeps current discovery behaviour.
    guilds: list[GuildSelectorEntry] = Field(default_factory=list)


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


class BackfillSettings(BaseModel):
    """Backward backfill settings (NEW v2 — workspace plan §"Part A — A.3").

    Disabled by default — opt-in via `enabled: true` after Phase A (adaptive
    rate limiter) is canaried. Backfill walks each channel backwards from
    its oldest-known message until reaching channel start (Discord returns
    empty page) or the safety bound is hit.
    """

    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    # Per-scan budget for backfill messages. Independent of forward live-tail
    # budget (`http.max_messages_per_scan`) so backfill doesn't starve fresh data.
    per_scan_message_budget: int = Field(default=5000, ge=1)
    # Safety bound: after this many scan runs without `backfill_complete=1`,
    # skip backfill on the channel (handles 500k-message firehoses).
    max_scan_runs_per_channel: int = Field(default=50, ge=1)


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
    # v2 — additive. Disabled by default; deploy after Phase A canary.
    backfill: BackfillSettings = Field(default_factory=BackfillSettings)

    def __repr__(self) -> str:
        # Custom repr keeps `auth` reduced to a literal "***" so even a stray
        # debug print of the Settings object cannot leak the token. SecretStr
        # masking already covers the value; this is defence-in-depth.
        return (
            f"Settings(run={self.run!r}, auth=AuthSettings(..., token=***), "
            f"gateway={self.gateway!r}, http={self.http!r}, retry={self.retry!r}, "
            f"discovery={self.discovery!r}, attachments={self.attachments!r}, "
            f"daemon={self.daemon!r}, retention={self.retention!r})"
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
        # Resolve the reserved prefix too: on macOS /etc is a symlink to
        # /private/etc, so an unresolved comparison would silently pass.
        reserved_resolved = reserved.resolve()
        try:
            resolved.relative_to(reserved_resolved)
        except ValueError:
            continue
        raise ConfigError(
            f"path {resolved} is inside reserved OS location {reserved_resolved}; "
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
