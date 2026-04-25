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
) -> httpx.AsyncClient:
    """Build the single shared AsyncClient for a scan.

    The caller owns the client lifecycle (`async with ...` or explicit
    `await client.aclose()`). Persist the cookie jar via `persist_cookies`
    on close.
    """
    if state_root is None:
        state_root = settings.run.state_root

    cookies = load_jar(state_root, settings.auth.keyring_username)

    # SEC-P0-14: per-host token bucket on every outbound request, not just
    # message paginate. One limiter per scan (one client = one limiter).
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
