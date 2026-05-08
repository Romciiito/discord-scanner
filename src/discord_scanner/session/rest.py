"""Single shared AsyncClient factory — HTTP/2, verify=True, URL allowlist, full headers.

Traces to:
- seed-spec.md §4.1 (header set), §4.2 (HTTP/2), §4.4 (cookies)
- security-model.md §6 SEC-P0-07 (header completeness), SEC-P0-11 (HTTP/2),
  SEC-P0-17 (URL allowlist), SEC-P0-18 (verify=True)
- claude-rules.md MUST "TLS", "HTTP/2", "URL allowlist"

Design rules:
- One `AsyncClient` per scan — shared across adapters. Never construct a second.
- TLS verification is always enabled (httpx default). Disabling it is a merge-blocker.
- `http2=True` ALWAYS. HTTP/1.1 is TLS-fingerprintable.
- `event_hooks["request"]` raises `SSRFViolation` on any netloc outside the
  four-host allowlist. The hook runs BEFORE the request leaves the client,
  so no out-of-allowlist packet is ever emitted.
- `event_hooks["response"]` scans 401/403 for captcha markers (SEC-P0-16).
- Redirects are followed only if the redirect target is also inside the
  allowlist (httpx enforces `event_hooks` on each hop).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from discord_scanner.config import Settings
from discord_scanner.logging_conf import get_logger
from discord_scanner.session.captcha import check_response
from discord_scanner.session.cookies import load_jar, save_jar
from discord_scanner.session.headers import build_rest_headers
from discord_scanner.session.rate_limit import RateLimiter

logger = get_logger(__name__)

ALLOWED_NETLOCS: Final[frozenset[str]] = frozenset(
    {
        "discord.com",
        "cdn.discordapp.com",
        "gateway.discord.gg",
        "media.discordapp.net",
    }
)

# CDN / media hosts must NEVER receive the Authorization header — claude-rules
# "never send Authorization header to cdn.discordapp.com or media.discordapp.net".
# The request hook `_strip_auth_on_cdn` enforces this on every outbound request.
_NO_AUTH_NETLOCS: Final[frozenset[str]] = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


class SSRFViolation(RuntimeError):
    """Raised when a request is about to leave the allowlisted hosts.

    SEC-P0-17 merge-blocker: no request ever escapes the allowlist.
    """

    def __init__(self, url: str, netloc: str) -> None:
        self.url = url
        self.netloc = netloc
        super().__init__(
            f"SSRFViolation: refusing request to {netloc!r} (not in allowlist). url={url}"
        )


async def _enforce_allowlist(request: httpx.Request) -> None:
    """httpx request hook — rejects any URL not in `ALLOWED_NETLOCS`.

    Also rejects `http://` (only https + wss allowed in this project; claude-rules
    "TLS always").
    """
    url = str(request.url)
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "wss"):
        raise SSRFViolation(url=url, netloc=parsed.netloc or parsed.scheme)
    if parsed.netloc not in ALLOWED_NETLOCS:
        raise SSRFViolation(url=url, netloc=parsed.netloc)


async def _strip_auth_on_cdn(request: httpx.Request) -> None:
    """httpx request hook — strip `Authorization` on CDN / media hosts.

    claude-rules MUST "never send Authorization header to cdn.discordapp.com
    or media.discordapp.net": the CDN URLs are pre-signed and leaking a user
    token there is a cross-domain credential exposure.
    """
    host = request.url.host
    if host in _NO_AUTH_NETLOCS and "Authorization" in request.headers:
        del request.headers["Authorization"]
        logger.debug("stripped_authorization_on_cdn", host=host)


async def _check_captcha_on_response(response: httpx.Response) -> None:
    """httpx response hook — scans 401/403 bodies for captcha markers."""
    # We must `await response.aread()` before inspecting JSON because httpx
    # streams bodies by default.
    if response.status_code in (401, 403):
        try:
            await response.aread()
        except httpx.HTTPError as e:
            logger.debug("aread_failed_in_captcha_hook", err=str(e))
            return
        check_response(response)


def _make_rate_limit_hook(limiter: RateLimiter) -> Any:
    """Return an httpx request hook that awaits the per-host token bucket
    before the request leaves the client.

    Wires SEC-P0-14 / claude-rules "Rate-limit + jitter (per-host token
    bucket)" into every outbound HTTP, not just the message-fetch loop.
    The host key is the URL host with `/api` suffix when applicable so the
    `discord.com/api: 2 req/s` configured rate is matched.
    """

    async def _hook(request: httpx.Request) -> None:
        host = request.url.host
        path = request.url.path or ""
        # Match the configured key shape: `discord.com/api` (not just `discord.com`).
        key = "discord.com/api" if host == "discord.com" and path.startswith("/api") else host
        await limiter._bucket_for(key).acquire()  # noqa: SLF001 — single-package usage

    return _hook


def make_client(
    settings: Settings,
    token: SecretStr,
    state_root: Path | None = None,
    *,
    http_overrides: Any | None = None,
) -> httpx.AsyncClient:
    """Build the single shared AsyncClient for a scan.

    The caller owns the client lifecycle (`async with ...` or explicit
    `await client.aclose()`). Persist the cookie jar via `persist_cookies`
    on close.

    M.3 multi-burner (`http_overrides` is `BurnerHttpOverrides`): when
    set, the named `http.*` fields are shallow-merged into a per-burner
    settings snapshot before constructing the client. The same snapshot
    must be passed to `DormantGateway` so the IDENTIFY `properties` blob
    matches `X-Super-Properties` byte-for-byte (SEC-P0-25).
    """
    if http_overrides is not None:
        settings = _apply_http_overrides(settings, http_overrides)

    if state_root is None:
        state_root = settings.run.state_root

    cookies = load_jar(state_root, settings.auth.keyring_username)

    # SEC-P0-14: per-host token bucket on every outbound request, not just
    # message paginate. One limiter per scan (one client = one limiter).
    # When `http.adaptive.enabled=True`, build an `AdaptiveRateLimiter` —
    # which IS-A `RateLimiter` so the request hook works unchanged, but
    # ALSO records response signals via `record_response()` from inside
    # `request_with_retry` (the retry layer auto-detects an AdaptiveRL on
    # the client and threads it through).
    limiter: RateLimiter
    if settings.http.adaptive.enabled:
        from discord_scanner.session.adaptive import (
            AdaptiveConfig,
            AdaptiveRateLimiter,
        )

        a = settings.http.adaptive
        adaptive_config = AdaptiveConfig(
            enabled=True,
            degraded_factor_range=a.degraded_factor_range,
            degrade_on_5xx_in_window=a.degrade_on_5xx_in_window,
            degrade_on_latency_p95_ms=a.degrade_on_latency_p95_ms,
            degrade_window_sec=a.degrade_window_sec,
            recover_after_successes=a.recover_after_successes,
            cooldown_on_consecutive_429=a.cooldown_on_consecutive_429,
            cooldown_on_captcha=a.cooldown_on_captcha,
            cooldown_on_403_streak=a.cooldown_on_403_streak,
            cooldown_duration_sec=a.cooldown_duration_sec,
            session_break_every_requests=a.session_break_every_requests,
            session_break_duration_sec=a.session_break_duration_sec,
            circadian_enabled=a.circadian.enabled,
            circadian_timezone=a.circadian.timezone,
            circadian_sleep_window=a.circadian.sleep_window,
            circadian_sleep_probability=a.circadian.sleep_probability,
            circadian_twilight_hours=a.circadian.twilight_hours,
        )
        limiter = AdaptiveRateLimiter(
            per_host_rate_per_sec=dict(settings.http.per_host_rate_per_sec),
            config=adaptive_config,
        )
    else:
        limiter = RateLimiter(per_host_rate_per_sec=dict(settings.http.per_host_rate_per_sec))
    rate_limit_hook = _make_rate_limit_hook(limiter)

    client = httpx.AsyncClient(
        http2=True,
        verify=True,
        timeout=settings.http.timeout_sec,
        headers=build_rest_headers(settings, token),
        cookies=cookies,
        follow_redirects=True,
        event_hooks={
            "request": [_enforce_allowlist, _strip_auth_on_cdn, rate_limit_hook],
            "response": [_check_captcha_on_response],
        },
    )
    # Stash the limiter on the client for fetch-layer access (burst pause +
    # explicit `async with limiter.acquire(host)` if needed). Using a private
    # attribute name to avoid colliding with httpx fields.
    client._discord_scanner_limiter = limiter  # type: ignore[attr-defined]  # noqa: SLF001
    logger.info(
        "rest_client_created",
        http2=True,
        verify=True,
        allowed_hosts=sorted(ALLOWED_NETLOCS),
        per_host_rate_per_sec=dict(settings.http.per_host_rate_per_sec),
    )
    return client


def persist_cookies(
    client: httpx.AsyncClient,
    settings: Settings,
    state_root: Path | None = None,
) -> None:
    """Persist the client's cookie jar. Call after `await client.aclose()`."""
    if state_root is None:
        state_root = settings.run.state_root
    save_jar(client.cookies, state_root, settings.auth.keyring_username)


def _apply_http_overrides(settings: Settings, http_overrides: Any) -> Settings:
    """Build a per-burner settings snapshot with `http_overrides` merged.

    Shared by `make_client` and `DormantGateway` so the IDENTIFY blob and
    `X-Super-Properties` are guaranteed to read from the same source
    (SEC-P0-25). Pure function — does not mutate the input.

    `http_overrides` is duck-typed as `BurnerHttpOverrides` (importing it
    here would create a config<->session circular dep): we just probe for
    the named optional fields. Unset (`None`) fields fall back to global.
    """
    update: dict[str, Any] = {}
    if getattr(http_overrides, "user_agent_chrome_version", None) is not None:
        update["user_agent_chrome_version"] = http_overrides.user_agent_chrome_version
    if getattr(http_overrides, "fake_os_platform", None) is not None:
        update["fake_os_platform"] = http_overrides.fake_os_platform
    if getattr(http_overrides, "fake_os", None) is not None:
        update["fake_os"] = http_overrides.fake_os
    if getattr(http_overrides, "locale", None) is not None:
        update["locale"] = http_overrides.locale
    if getattr(http_overrides, "client_build_number", None) is not None:
        update["client_build_number"] = http_overrides.client_build_number
    if not update:
        return settings
    new_http = settings.http.model_copy(update=update)
    logger.info(
        "http_overrides_applied",
        keyring_username=settings.auth.keyring_username,
        fields=sorted(update.keys()),
    )
    return settings.model_copy(update={"http": new_http})
