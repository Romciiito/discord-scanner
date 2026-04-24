"""Per-host token bucket + global concurrency cap + per-request jitter.

Traces to:
- seed-spec.md §2.6, §4.5 (rate-limit + jitter + burst pause)
- security-model.md §6 SEC-P0-14
- claude-rules.md MUST "Rate-limit + jitter"

Defaults (from `config.http.per_host_rate_per_sec`):
- `discord.com/api: 2 req/s`
- `cdn.discordapp.com: 1 req/s`

Per-request jitter `random.uniform(*config.http.per_channel_delay_sec)` (default
1.5-4.0 s) + inter-channel burst pause `random.uniform(*burst_pause_sec)` (default
30-90 s) are applied by the orchestrating layer via `sleep_with_jitter` /
`burst_pause`. This module never calls `time.sleep` inside an `async def`
(that would block the event loop — CI grep-blocker).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

# Default global concurrency cap across all hosts (seed-spec §2.6).
DEFAULT_GLOBAL_CONCURRENCY = 8


@dataclass
class _HostBucket:
    """Per-host token bucket: one permit emitted every `1/rate_per_sec` seconds."""

    rate_per_sec: float
    _next_allowed_monotonic: float = field(default=0.0)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self) -> None:
        """Wait until a permit is available, then reserve the next slot.

        Uses `asyncio.sleep` (not `time.sleep`) — safe inside async def.
        """
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_monotonic - now
            if wait > 0:
                await asyncio.sleep(wait)
            # reserve next slot
            interval = 1.0 / self.rate_per_sec if self.rate_per_sec > 0 else 0.0
            self._next_allowed_monotonic = max(now, self._next_allowed_monotonic) + interval


class RateLimiter:
    """Composite limiter: per-host token buckets + global asyncio semaphore.

    One instance per scan; registered with the `httpx.AsyncClient` via an event
    hook wrapper in `rest.py` or via explicit `async with limiter.acquire(host)`
    call sites in the fetch layer.
    """

    def __init__(
        self,
        per_host_rate_per_sec: dict[str, float],
        global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
    ) -> None:
        self._buckets: dict[str, _HostBucket] = {
            host: _HostBucket(rate_per_sec=rate) for host, rate in per_host_rate_per_sec.items()
        }
        self._global_sem = asyncio.Semaphore(global_concurrency)
        self._default_rate = min(per_host_rate_per_sec.values(), default=1.0)

    def _bucket_for(self, host: str) -> _HostBucket:
        if host in self._buckets:
            return self._buckets[host]
        # Unknown host → create a bucket at the most conservative known rate.
        bucket = _HostBucket(rate_per_sec=self._default_rate)
        self._buckets[host] = bucket
        return bucket

    @asynccontextmanager
    async def acquire(self, host: str) -> AsyncIterator[None]:
        """Acquire a permit for `host`. Blocks on per-host bucket + global sem."""
        await self._global_sem.acquire()
        try:
            bucket = self._bucket_for(host)
            await bucket.acquire()
            yield
        finally:
            self._global_sem.release()


async def sleep_with_jitter(bounds: tuple[float, float]) -> None:
    """Sleep `random.uniform(*bounds)` seconds — MUST NOT use `time.sleep` (CI guard)."""
    low, high = bounds
    await asyncio.sleep(random.uniform(low, high))  # noqa: S311 — jitter, not crypto


async def burst_pause(bounds: tuple[float, float]) -> None:
    """Inter-channel burst pause (seed-spec §4.5)."""
    await sleep_with_jitter(bounds)
