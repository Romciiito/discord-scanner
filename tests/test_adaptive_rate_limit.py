"""Tests for `session/adaptive.py` — state-machine-driven adaptive rate limiter.

Traces to:
- workspace plan §"Part A — A.1 / Phase A — Adaptive Rate Limiter"
- requirements.md (Stage 2) implicit: ToS-conservative throttling

Test design uses `monkeypatch` against `time.monotonic` and `random` to make
state transitions deterministic. No external mocks required — these are
pure unit tests of the state machine + signal aggregation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from discord_scanner.session.adaptive import (
    AdaptiveConfig,
    AdaptiveRateLimiter,
    State,
)


# --- Helpers ----------------------------------------------------------------

def _make_limiter(
    *,
    enabled: bool = True,
    rates: dict[str, float] | None = None,
    **cfg_overrides: Any,
) -> AdaptiveRateLimiter:
    """Build an adaptive limiter with sensible test defaults."""
    rates = rates or {"discord.com/api": 1.0, "cdn.discordapp.com": 0.5}
    cfg = AdaptiveConfig(enabled=enabled, **cfg_overrides)
    return AdaptiveRateLimiter(per_host_rate_per_sec=rates, config=cfg)


# --- 1. Disabled = passthrough (parity with v1 RateLimiter) ----------------

@pytest.mark.asyncio
async def test_disabled_means_passthrough() -> None:
    """When `enabled=False`, record_response is a no-op and rates never change."""
    rl = _make_limiter(enabled=False)
    assert rl.state == State.NORMAL

    # Pump every kind of event that would normally trigger transitions.
    for _ in range(10):
        await rl.record_response(status=429, latency_ms=50)
        await rl.record_response(status=403, latency_ms=50)
        await rl.record_response(status=500, latency_ms=10000, was_captcha=True)

    assert rl.state == State.NORMAL
    assert rl.get_host_rate("discord.com/api") == 1.0


# --- 2. Initial state -------------------------------------------------------

@pytest.mark.asyncio
async def test_starts_in_normal_state() -> None:
    rl = _make_limiter()
    assert rl.state == State.NORMAL
    assert rl.cooldown_remaining_sec == 0.0
    assert rl.consecutive_successes == 0


# --- 3. DEGRADED triggers ---------------------------------------------------

@pytest.mark.asyncio
async def test_5xx_cluster_triggers_degraded() -> None:
    rl = _make_limiter(degrade_on_5xx_in_window=3, degrade_window_sec=60.0)
    for _ in range(3):
        await rl.record_response(status=500, latency_ms=200)
    assert rl.state == State.DEGRADED
    # Effective rate should be reduced by factor in [0.30, 0.60].
    new_rate = rl.get_host_rate("discord.com/api")
    assert 0.30 <= new_rate <= 0.60


@pytest.mark.asyncio
async def test_5xx_outside_window_does_not_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two 5xx within window + one >window seconds later should NOT degrade."""
    fake_now = [1000.0]

    def _now() -> float:
        return fake_now[0]

    monkeypatch.setattr("discord_scanner.session.adaptive.time.monotonic", _now)

    rl = _make_limiter(degrade_on_5xx_in_window=3, degrade_window_sec=60.0)
    await rl.record_response(status=500, latency_ms=100)
    fake_now[0] += 30
    await rl.record_response(status=500, latency_ms=100)
    fake_now[0] += 100  # past the 60s window
    await rl.record_response(status=500, latency_ms=100)
    assert rl.state == State.NORMAL


@pytest.mark.asyncio
async def test_latency_p95_triggers_degraded() -> None:
    rl = _make_limiter(degrade_on_latency_p95_ms=4000.0)
    # Push many fast samples then several slow ones — p95 should crest the threshold.
    for _ in range(15):
        await rl.record_response(status=200, latency_ms=100)
    for _ in range(15):
        await rl.record_response(status=200, latency_ms=8000)
    # Final sample tips p95 above 4000.
    assert rl.state == State.DEGRADED


@pytest.mark.asyncio
async def test_low_latency_does_not_degrade() -> None:
    rl = _make_limiter(degrade_on_latency_p95_ms=4000.0)
    for _ in range(50):
        await rl.record_response(status=200, latency_ms=100)
    assert rl.state == State.NORMAL


# --- 4. DEGRADED → NORMAL recovery ------------------------------------------

@pytest.mark.asyncio
async def test_degraded_recovers_after_n_successes() -> None:
    rl = _make_limiter(
        degrade_on_5xx_in_window=3,
        degrade_window_sec=60.0,
        recover_after_successes=5,
    )
    for _ in range(3):
        await rl.record_response(status=500, latency_ms=100)
    assert rl.state == State.DEGRADED

    for _ in range(5):
        await rl.record_response(status=200, latency_ms=100)
    assert rl.state == State.NORMAL
    # Rate restored to baseline.
    assert rl.get_host_rate("discord.com/api") == 1.0


# --- 5. COOLDOWN triggers ---------------------------------------------------

@pytest.mark.asyncio
async def test_consecutive_429_triggers_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force deterministic cooldown duration to keep tests fast.
    monkeypatch.setattr(
        "discord_scanner.session.adaptive.random.uniform",
        lambda a, b: a,  # always return lower bound — predictable
    )
    rl = _make_limiter(
        cooldown_on_consecutive_429=3,
        cooldown_duration_sec=(10.0, 300.0),
    )
    for _ in range(3):
        await rl.record_response(status=429, latency_ms=100)
    assert rl.state == State.COOLDOWN
    assert rl.cooldown_remaining_sec > 0
    # cooldown duration should be in the configured range.
    assert 9.5 <= rl.cooldown_remaining_sec <= 10.5  # using lower-bound lambda


@pytest.mark.asyncio
async def test_captcha_triggers_cooldown() -> None:
    rl = _make_limiter(cooldown_on_captcha=True)
    await rl.record_response(status=403, latency_ms=100, was_captcha=True)
    assert rl.state == State.COOLDOWN


@pytest.mark.asyncio
async def test_403_streak_triggers_cooldown() -> None:
    rl = _make_limiter(cooldown_on_403_streak=3)
    for _ in range(3):
        await rl.record_response(status=403, latency_ms=100)
    assert rl.state == State.COOLDOWN


@pytest.mark.asyncio
async def test_cooldown_duration_in_configured_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Track random.uniform calls to confirm the cooldown range was used.
    captured: list[tuple[float, float]] = []

    def fake_uniform(a: float, b: float) -> float:
        captured.append((a, b))
        return (a + b) / 2.0

    monkeypatch.setattr("discord_scanner.session.adaptive.random.uniform", fake_uniform)
    rl = _make_limiter(
        cooldown_on_consecutive_429=2,
        cooldown_duration_sec=(20.0, 100.0),
    )
    for _ in range(2):
        await rl.record_response(status=429, latency_ms=100)
    # The cooldown_duration_sec range must have been queried.
    assert (20.0, 100.0) in captured
    # Cooldown remaining ~ midpoint of (20,100) = 60.
    assert 59.0 <= rl.cooldown_remaining_sec <= 61.0


# --- 6. COOLDOWN → NORMAL transition ----------------------------------------

@pytest.mark.asyncio
async def test_cooldown_returns_to_normal_via_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cooldown elapses, next acquire() drops the state back to NORMAL."""
    fake_now = [1000.0]

    def _now() -> float:
        return fake_now[0]

    monkeypatch.setattr("discord_scanner.session.adaptive.time.monotonic", _now)
    monkeypatch.setattr(
        "discord_scanner.session.adaptive.random.uniform",
        lambda a, b: 1.0,  # 1-sec cooldown for fast test
    )

    rl = _make_limiter(cooldown_on_consecutive_429=1)
    await rl.record_response(status=429, latency_ms=100)
    assert rl.state == State.COOLDOWN

    # Advance time past cooldown; acquire should drop the state.
    fake_now[0] += 60.0
    async with rl.acquire("discord.com/api"):
        pass
    assert rl.state == State.NORMAL
    # Streak counters reset on cooldown entry; should still be 0.
    assert rl.consecutive_successes == 0


# --- 7. SESSION_BREAK -------------------------------------------------------

@pytest.mark.asyncio
async def test_session_break_after_n_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("discord_scanner.session.adaptive.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "discord_scanner.session.adaptive.random.uniform",
        lambda a, b: 75.0,  # deterministic session-break duration
    )

    rl = _make_limiter(
        session_break_every_requests=3,
        session_break_duration_sec=(60.0, 180.0),
        circadian_enabled=False,  # isolate session-break behaviour
    )
    for _ in range(3):
        async with rl.acquire("discord.com/api"):
            pass
    # On the 3rd acquire, a session break should have been triggered.
    assert 75.0 in sleeps


@pytest.mark.asyncio
async def test_session_break_resets_after_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("discord_scanner.session.adaptive.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "discord_scanner.session.adaptive.random.uniform",
        lambda a, b: 60.0,
    )

    rl = _make_limiter(
        session_break_every_requests=2,
        circadian_enabled=False,
    )
    for _ in range(4):
        async with rl.acquire("discord.com/api"):
            pass
    # Should have triggered exactly twice — at request 2 and request 4.
    assert sleeps.count(60.0) == 2


# --- 8. CIRCADIAN -----------------------------------------------------------

def test_circadian_window_parses_simple() -> None:
    rl = _make_limiter(
        circadian_enabled=True,
        circadian_sleep_window="02:00-08:00",
    )
    # Inside window
    p = rl._circadian_skip_probability(datetime(2026, 4, 26, 4, 0))
    assert p == pytest.approx(0.95)


def test_circadian_outside_window_returns_zero() -> None:
    rl = _make_limiter(
        circadian_enabled=True,
        circadian_sleep_window="02:00-08:00",
        circadian_twilight_hours=0.0,
    )
    p = rl._circadian_skip_probability(datetime(2026, 4, 26, 14, 0))
    assert p == 0.0


def test_circadian_twilight_ramps() -> None:
    rl = _make_limiter(
        circadian_enabled=True,
        circadian_sleep_window="02:00-08:00",
        circadian_twilight_hours=1.0,
        circadian_sleep_probability=1.0,
    )
    # 30 min before window start: should be at ~50% probability (linear ramp).
    p = rl._circadian_skip_probability(datetime(2026, 4, 26, 1, 30))
    assert 0.4 <= p <= 0.6
    # Right at window start: full probability.
    p_full = rl._circadian_skip_probability(datetime(2026, 4, 26, 2, 1))
    assert p_full >= 0.95


def test_circadian_window_wraps_midnight() -> None:
    """22:00-06:00 should treat 23:30 as inside, 06:30 as outside."""
    rl = _make_limiter(
        circadian_enabled=True,
        circadian_sleep_window="22:00-06:00",
        circadian_twilight_hours=0.0,
    )
    p_inside = rl._circadian_skip_probability(datetime(2026, 4, 26, 23, 30))
    assert p_inside > 0.0
    p_outside = rl._circadian_skip_probability(datetime(2026, 4, 26, 14, 0))
    assert p_outside == 0.0


def test_circadian_disabled_returns_zero() -> None:
    rl = _make_limiter(circadian_enabled=False)
    p = rl._circadian_skip_probability(datetime(2026, 4, 26, 4, 0))
    assert p == 0.0


# --- 9. Bucket rate management ---------------------------------------------

@pytest.mark.asyncio
async def test_degraded_scales_all_known_hosts() -> None:
    rl = _make_limiter(
        rates={"discord.com/api": 2.0, "cdn.discordapp.com": 1.0},
        degrade_on_5xx_in_window=2,
    )
    for _ in range(2):
        await rl.record_response(status=500, latency_ms=100)

    api_rate = rl.get_host_rate("discord.com/api")
    cdn_rate = rl.get_host_rate("cdn.discordapp.com")
    assert api_rate < 2.0
    assert cdn_rate < 1.0
    assert api_rate / 2.0 == pytest.approx(cdn_rate / 1.0, rel=1e-6)


# --- 10. Acquire integration -----------------------------------------------

@pytest.mark.asyncio
async def test_acquire_passes_through_when_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In NORMAL with no session-break trigger, acquire should not insert delays."""

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        # delegate to a real zero-sleep so loops don't hang
        await asyncio.sleep(0)

    monkeypatch.setattr("discord_scanner.session.adaptive.asyncio.sleep", fake_sleep)

    rl = _make_limiter(
        rates={"discord.com/api": 100.0},  # fast rate so bucket doesn't add delay
        circadian_enabled=False,
        session_break_every_requests=10_000,
    )
    async with rl.acquire("discord.com/api"):
        pass
    # No session-break sleep should have happened.
    assert all(d != 60.0 for d in sleeps)


# --- 11. State exposure / introspection ------------------------------------

@pytest.mark.asyncio
async def test_state_property_exposes_current_state() -> None:
    rl = _make_limiter(degrade_on_5xx_in_window=1)
    assert rl.state == State.NORMAL
    await rl.record_response(status=500, latency_ms=100)
    assert rl.state == State.DEGRADED


@pytest.mark.asyncio
async def test_get_host_rate_returns_default_for_unknown_host() -> None:
    rl = _make_limiter(rates={"discord.com/api": 1.0})
    # Unknown host falls back to default rate (most conservative known).
    rate = rl.get_host_rate("unknown.example.com")
    assert rate > 0.0


# --- 12. Determinism / no leaks --------------------------------------------

@pytest.mark.asyncio
async def test_recover_resets_consecutive_429() -> None:
    """After a streak of 429s and a recovery, the 429 counter must be 0."""
    rl = _make_limiter(
        cooldown_on_consecutive_429=10,  # high — won't cooldown
        degrade_on_5xx_in_window=10_000,  # never degrade from 5xx
        recover_after_successes=2,
    )
    for _ in range(3):
        await rl.record_response(status=429, latency_ms=100)
    # Each 429 raises consecutive_429 BUT also resets consecutive_successes.
    # A success should reset 429 streak.
    await rl.record_response(status=200, latency_ms=100)
    # Internal counter — exposed via test-only property if needed.
    # Indirectly verified: another 429 should NOT cooldown if we go up to 10
    # again, but a fresh streak from 1 takes 10 to reach the threshold.
    for _ in range(9):
        await rl.record_response(status=429, latency_ms=100)
    assert rl.state == State.NORMAL  # 9 < 10 threshold
