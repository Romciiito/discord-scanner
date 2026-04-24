"""Tests for `session/rate_limit.py` — token bucket timing + global semaphore.

Traces to: SEC-P0-14.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from discord_scanner.session.rate_limit import (
    DEFAULT_GLOBAL_CONCURRENCY,
    RateLimiter,
    sleep_with_jitter,
)


@pytest.mark.asyncio
async def test_inter_request_gap_respects_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-P0-14: two back-to-back acquires on the same host are spaced
    >= 1 / rate_per_sec seconds apart."""
    rl = RateLimiter(per_host_rate_per_sec={"discord.com/api": 10.0})  # 100 ms gap
    t0 = time.monotonic()
    async with rl.acquire("discord.com/api"):
        pass
    async with rl.acquire("discord.com/api"):
        pass
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.09, f"expected >= 90 ms gap, got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_different_hosts_dont_block_each_other() -> None:
    rl = RateLimiter(per_host_rate_per_sec={"discord.com/api": 1.0, "cdn.discordapp.com": 1.0})
    t0 = time.monotonic()
    async with rl.acquire("discord.com/api"):
        pass
    async with rl.acquire("cdn.discordapp.com"):
        pass
    elapsed = time.monotonic() - t0
    # both first-acquires are free → should complete quickly
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_global_concurrency_cap() -> None:
    """Global semaphore caps concurrent acquires at DEFAULT_GLOBAL_CONCURRENCY."""
    rl = RateLimiter(
        per_host_rate_per_sec={"discord.com/api": 100.0},
        global_concurrency=2,
    )

    in_flight = 0
    peak = 0

    async def one() -> None:
        nonlocal in_flight, peak
        async with rl.acquire("discord.com/api"):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1

    await asyncio.gather(*[one() for _ in range(6)])
    assert peak <= 2, f"global cap breached — peak in-flight = {peak}"


def test_default_global_concurrency_constant() -> None:
    assert DEFAULT_GLOBAL_CONCURRENCY == 8


@pytest.mark.asyncio
async def test_sleep_with_jitter_within_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sleep_with_jitter((0.01, 0.03))` sleeps between 10-30 ms."""
    t0 = time.monotonic()
    await sleep_with_jitter((0.01, 0.03))
    elapsed = time.monotonic() - t0
    assert 0.005 <= elapsed <= 0.15
