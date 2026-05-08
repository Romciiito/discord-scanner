"""Tenacity retry wrapper — honours Retry-After on 429, backs off on 5xx.

Traces to:
- seed-spec.md §2.6 (429 / 5xx handling)
- security-model.md §6 SEC-P0-15 (Retry-After honoured; MAX_429_RETRIES=3
  then channel skip)
- claude-rules.md MUST "Rate-limit + jitter" → 429 Retry-After clause

Exposes:
- `ChannelAbort`: raised after 3 consecutive 429s on the same endpoint; caller
  MUST catch and skip the current channel.
- `RetryableResponseError`: internal signal to tenacity.
- `RequestWithRetry`: convenience wrapper around `httpx.AsyncClient.request`.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Final

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from discord_scanner.logging_conf import get_logger

if TYPE_CHECKING:
    from discord_scanner.session.adaptive import AdaptiveRateLimiter

logger = get_logger(__name__)

MAX_429_RETRIES: Final[int] = 3
RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

# Captcha detection markers (mirrors session/rest.py captcha hard-abort logic).
_CAPTCHA_MARKERS: Final[tuple[str, ...]] = ("captcha_key", "captcha_sitekey", "captcha_service")


class ChannelAbort(RuntimeError):
    """Raised after MAX_429_RETRIES consecutive 429s on the same endpoint.

    The caller (fetch layer) MUST catch this and skip to the next channel
    per SEC-P0-15.
    """


class RetryableResponseError(RuntimeError):
    """Internal exception: a retryable HTTP status was seen.

    Tenacity catches this and schedules the next attempt.
    """

    def __init__(self, status: int, retry_after: float | None, url: str) -> None:
        self.status = status
        self.retry_after = retry_after
        self.url = url
        super().__init__(f"retryable {status} for {url} (retry_after={retry_after})")


async def _sleep_retry_after(resp: httpx.Response) -> None:
    """Honour `Retry-After` header (RFC 7231: seconds-integer OR HTTP-date)."""
    header = resp.headers.get("Retry-After")
    if not header:
        return
    try:
        seconds = float(header)
    except ValueError:
        # HTTP-date form — pragmatic skip; tenacity's own backoff will cover it.
        logger.debug("retry_after_not_numeric", header=header)
        return
    logger.info("honour_retry_after", seconds=seconds, status=resp.status_code)
    await asyncio.sleep(min(seconds, 120.0))  # 2-min cap to stay responsive


def _resolve_adaptive(
    client: httpx.AsyncClient,
    explicit: AdaptiveRateLimiter | None,
) -> AdaptiveRateLimiter | None:
    """Pick the adaptive limiter to record against.

    Precedence: caller-provided `explicit` arg wins; otherwise auto-detect
    by inspecting the client for an `_discord_scanner_limiter` attribute
    (set by `make_client`) — return it only if it's an AdaptiveRateLimiter
    instance with `.record_response`. This keeps every call site in
    `fetch/`, `discovery/`, `dump/` adaptive-aware without per-callsite
    edits."""
    if explicit is not None:
        return explicit
    candidate = getattr(client, "_discord_scanner_limiter", None)
    if candidate is None:
        return None
    # Duck-type detection — `AdaptiveRateLimiter` extends `RateLimiter`.
    # `record_response` is only defined on the adaptive subclass.
    if hasattr(candidate, "record_response") and callable(candidate.record_response):
        return candidate
    return None


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    backoff_initial_sec: float = 2.0,
    backoff_max_sec: float = 60.0,
    attempts: int = 5,
    adaptive: AdaptiveRateLimiter | None = None,
    **request_kwargs: Any,
) -> httpx.Response:
    """Wrap `client.request(...)` with tenacity retries + 429 Retry-After.

    If `adaptive` is provided, every response (success OR retryable) is
    fed to its `record_response()` so the state machine can detect 5xx
    clusters / 429 streaks / captcha and adjust throttle / cooldown.
    Adaptive recording is best-effort: we never let a recording exception
    abort the request flow. When `adaptive` is None or its
    `config.enabled` is False, behaviour is identical to v1.

    Raises:
        ChannelAbort: after MAX_429_RETRIES consecutive 429s.
        httpx.RequestError: for transport errors (tenacity will still retry
            network errors; exhausted retries re-raise the last one via
            RetryError unwrap).
    """
    consecutive_429 = 0
    # Resolve adaptive limiter — explicit arg, else auto-detect on client.
    adaptive_resolved = _resolve_adaptive(client, adaptive)

    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((RetryableResponseError, httpx.TransportError)),
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(
                multiplier=backoff_initial_sec,
                max=backoff_max_sec,
            ),
            reraise=True,
        ):
            with attempt:
                started = time.monotonic()
                resp = await client.request(method, url, **request_kwargs)
                latency_ms = (time.monotonic() - started) * 1000.0
                # Best-effort adaptive recording — never let a recording
                # error mask the actual request outcome.
                if adaptive_resolved is not None:
                    try:
                        was_captcha = _looks_like_captcha(resp)
                        await adaptive_resolved.record_response(
                            status=resp.status_code,
                            latency_ms=latency_ms,
                            was_captcha=was_captcha,
                        )
                    except Exception as e:  # noqa: BLE001 — defensive, then logged
                        logger.debug("adaptive_record_failed", err=str(e))
                if resp.status_code == 429:
                    consecutive_429 += 1
                    if consecutive_429 >= MAX_429_RETRIES:
                        logger.warning(
                            "max_429_retries_exceeded",
                            url=url,
                            attempts=consecutive_429,
                        )
                        raise ChannelAbort(f"{MAX_429_RETRIES} consecutive 429s on {url}")
                    retry_after = resp.headers.get("Retry-After")
                    await _sleep_retry_after(resp)
                    raise RetryableResponseError(
                        status=429,
                        retry_after=(
                            float(retry_after)
                            if retry_after and retry_after.replace(".", "").isdigit()
                            else None
                        ),
                        url=url,
                    )
                if resp.status_code in RETRY_STATUSES:
                    # SEC-P0-15 reads "consecutive 429s on the same endpoint" —
                    # any non-429 retryable status (500/502/503/504) resets the
                    # counter so a transient 5xx does not count toward the
                    # ChannelAbort budget.
                    consecutive_429 = 0
                    raise RetryableResponseError(
                        status=resp.status_code,
                        retry_after=None,
                        url=url,
                    )
                consecutive_429 = 0
                return resp
    except RetryError as e:
        # Exhausted attempts — propagate the underlying cause with explicit `from`.
        if e.last_attempt.failed:
            cause = e.last_attempt.exception()
            if cause is not None:
                raise cause from e
        raise

    # Unreachable — AsyncRetrying either returns or raises.
    raise RuntimeError("request_with_retry: fell through retry loop")  # pragma: no cover


def _looks_like_captcha(resp: httpx.Response) -> bool:
    """Detect a Cloudflare/Discord captcha response. Mirrors rest.py logic.

    Captcha responses arrive with 401 or 403 + a JSON body containing one
    of the marker keys. We sniff the raw bytes — never call resp.json()
    here (it can raise; we want a cheap predicate)."""
    if resp.status_code not in (401, 403):
        return False
    try:
        body = resp.text
    except Exception:  # noqa: BLE001 — body access best-effort
        return False
    return any(marker in body for marker in _CAPTCHA_MARKERS)
