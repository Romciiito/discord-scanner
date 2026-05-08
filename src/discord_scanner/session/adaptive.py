"""Adaptive rate limiter — state-machine-driven throttling on top of the base
token bucket. Mimics human burst behaviour to reduce ToS-detection risk.

Traces to:
- workspace plan §"Part A — A.1"
  (`/Users/trungle/.claude/plans/i-have-used-users-trungle-desktop-projec-dazzling-iverson.md`)
- claude-rules.md MUST "Rate-limit + jitter" (extends; preserves existing
  per-host token bucket behaviour when adaptive is disabled)
- security-model.md §6 SEC-P0-14 (rate-limit guard, extended)

State machine:

    NORMAL (baseline rate)
       │ 5xx cluster (≥3 in 60s) OR latency p95 >4s
       ▼
    DEGRADED (rate × U(0.30, 0.60))    ─► N=30 successes ─► NORMAL
       │ 429 streak ≥5  OR  captcha  OR  403 streak ≥3
       ▼
    COOLDOWN (full pause, U(10, 300)s) ─► timeout elapsed ─► NORMAL

Orthogonal layers (always on when enabled):
- CIRCADIAN — probabilistic sleep inside operator's TZ sleep window.
  P(skip)=0.95 inside window; ramp 1h on either side (twilight).
- SESSION_BREAK — every N=200 requests, pause U(60, 180)s.

Defaults are conservative — opt-in via `http.adaptive.enabled: true`.
When disabled, this class behaves identically to the base RateLimiter
(no calls to record_response, no state transitions, baseline rates stay
constant).

This module never calls `time.sleep` inside an `async def` (CI grep blocker).
"""

from __future__ import annotations

import asyncio
import enum
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Final
from zoneinfo import ZoneInfo

from discord_scanner.logging_conf import get_logger
from discord_scanner.session.rate_limit import (
    DEFAULT_GLOBAL_CONCURRENCY,
    RateLimiter,
    _HostBucket,
)

logger = get_logger(__name__)

# Internal cap on how long we'll wait inside acquire() — defends against a
# pathologically long cooldown setting starving the scan loop.
_MAX_COOLDOWN_SEC: Final[float] = 600.0
# Latency window size — rolling p95 sample.
_LATENCY_WINDOW_SIZE: Final[int] = 50
# 5xx window duration — how long a 5xx counts toward "cluster" detection.
_DEFAULT_5XX_WINDOW_SEC: Final[float] = 60.0


class State(str, enum.Enum):
    """Adaptive limiter operating state."""

    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    COOLDOWN = "COOLDOWN"


@dataclass
class AdaptiveConfig:
    """Tunables for the adaptive limiter — defaults match
    config.live.yaml.example. All ranges are uniform-random sampled."""

    enabled: bool = True

    # DEGRADED state factor: effective rate = baseline × U(*range).
    # Range [0.30, 0.60] = "reduce by 40-70%" per workspace plan §A.1.
    degraded_factor_range: tuple[float, float] = (0.30, 0.60)

    # DEGRADED triggers
    degrade_on_5xx_in_window: int = 3
    degrade_on_latency_p95_ms: float = 4000.0
    degrade_window_sec: float = _DEFAULT_5XX_WINDOW_SEC

    # DEGRADED → NORMAL: N consecutive successes
    recover_after_successes: int = 30

    # COOLDOWN triggers
    cooldown_on_consecutive_429: int = 5
    cooldown_on_captcha: bool = True
    cooldown_on_403_streak: int = 3
    cooldown_duration_sec: tuple[float, float] = (10.0, 300.0)

    # SESSION_BREAK
    session_break_every_requests: int = 200
    session_break_duration_sec: tuple[float, float] = (60.0, 180.0)

    # CIRCADIAN
    circadian_enabled: bool = True
    circadian_timezone: str = "Europe/Prague"
    # Sleep window in operator's TZ. "HH:MM-HH:MM"; wrap-around (e.g.
    # "22:00-06:00") is supported.
    circadian_sleep_window: str = "02:00-08:00"
    # Probability we skip a request when fully inside the sleep window.
    # Outside the window: 0. Inside twilight: linear ramp.
    circadian_sleep_probability: float = 0.95
    circadian_twilight_hours: float = 1.0


class AdaptiveRateLimiter(RateLimiter):
    """RateLimiter that adapts host bucket rates and inserts session
    breaks / cooldowns / circadian dampening.

    When `config.enabled` is False, behaves identically to the base
    RateLimiter (record_response is a no-op; bucket rates never adjusted).
    """

    def __init__(
        self,
        per_host_rate_per_sec: dict[str, float],
        global_concurrency: int = DEFAULT_GLOBAL_CONCURRENCY,
        config: AdaptiveConfig | None = None,
    ) -> None:
        super().__init__(per_host_rate_per_sec, global_concurrency)
        self._adaptive_config = config or AdaptiveConfig()
        # Snapshot baseline rates so DEGRADED can revert to them on recovery.
        self._baseline_rates: dict[str, float] = dict(per_host_rate_per_sec)
        # State + signal tracking
        self._state: State = State.NORMAL
        self._cooldown_until_monotonic: float = 0.0
        self._5xx_timestamps: list[float] = []
        self._latencies_ms: list[float] = []
        self._consecutive_429: int = 0
        self._consecutive_403: int = 0
        self._consecutive_successes: int = 0
        self._request_count_since_break: int = 0
        self._state_lock: asyncio.Lock = asyncio.Lock()
        # Pre-parse circadian window bounds (cached, never re-parsed at runtime)
        self._circadian_window: tuple[dtime, dtime] | None = None
        if self._adaptive_config.circadian_enabled:
            self._circadian_window = self._parse_window(
                self._adaptive_config.circadian_sleep_window
            )
        try:
            self._circadian_tz = ZoneInfo(self._adaptive_config.circadian_timezone)
        except Exception:
            logger.warning(
                "circadian_timezone_unknown",
                timezone=self._adaptive_config.circadian_timezone,
                fallback="UTC",
            )
            self._circadian_tz = timezone.utc

    # ------------------------------------------------------------------
    # State machine — record_response is the single entry point.
    # ------------------------------------------------------------------

    async def record_response(
        self,
        status: int,
        latency_ms: float,
        was_captcha: bool = False,
    ) -> None:
        """Update signal windows + transition state if thresholds crossed.

        Safe to call from an unlocked context — internal lock guards
        state mutations.
        """
        if not self._adaptive_config.enabled:
            return
        async with self._state_lock:
            now = time.monotonic()
            cfg = self._adaptive_config

            # Track latency
            self._latencies_ms.append(latency_ms)
            if len(self._latencies_ms) > _LATENCY_WINDOW_SIZE:
                self._latencies_ms.pop(0)

            # Track 5xx in rolling window
            if 500 <= status < 600:
                self._5xx_timestamps.append(now)
            self._5xx_timestamps = [
                t for t in self._5xx_timestamps if (now - t) <= cfg.degrade_window_sec
            ]

            # Update streak counters
            if status == 429:
                self._consecutive_429 += 1
                self._consecutive_successes = 0
            elif status == 403:
                self._consecutive_403 += 1
                self._consecutive_successes = 0
            elif 200 <= status < 300:
                self._consecutive_429 = 0
                self._consecutive_403 = 0
                self._consecutive_successes += 1
            # 5xx and other 4xx don't reset 429 streak per retry.py contract;
            # we mirror that behaviour here (5xx counts toward DEGRADED
            # signals only, not COOLDOWN).

            # COOLDOWN triggers (highest priority — pre-empts DEGRADED)
            cooldown_reason: str | None = None
            if was_captcha and cfg.cooldown_on_captcha:
                cooldown_reason = "captcha"
            elif self._consecutive_429 >= cfg.cooldown_on_consecutive_429:
                cooldown_reason = f"consecutive_429={self._consecutive_429}"
            elif self._consecutive_403 >= cfg.cooldown_on_403_streak:
                cooldown_reason = f"consecutive_403={self._consecutive_403}"

            if cooldown_reason is not None:
                self._enter_cooldown(reason=cooldown_reason, now=now)
                return

            # DEGRADED triggers
            if self._state == State.NORMAL:
                degrade_reason: str | None = None
                if len(self._5xx_timestamps) >= cfg.degrade_on_5xx_in_window:
                    degrade_reason = (
                        f"5xx_cluster={len(self._5xx_timestamps)}"
                        f"_in_{cfg.degrade_window_sec}s"
                    )
                else:
                    p95 = self._compute_p95_latency()
                    if p95 is not None and p95 > cfg.degrade_on_latency_p95_ms:
                        degrade_reason = f"latency_p95_ms={p95:.0f}"
                if degrade_reason is not None:
                    self._enter_degraded(reason=degrade_reason)

            # DEGRADED → NORMAL recovery
            if (
                self._state == State.DEGRADED
                and self._consecutive_successes >= cfg.recover_after_successes
            ):
                self._enter_normal(reason=f"recovered_after_{self._consecutive_successes}_successes")

    # ------------------------------------------------------------------
    # Acquire — applies circadian / session-break / cooldown dampening.
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(self, host: str) -> AsyncIterator[None]:
        """Acquire a permit, applying all adaptive layers before yielding."""
        if not self._adaptive_config.enabled:
            # Fast path: identical behaviour to base RateLimiter.
            async with super().acquire(host):
                yield
            return

        # 1. COOLDOWN — wait for it to expire.
        await self._wait_for_cooldown_clear()

        # 2. CIRCADIAN — probabilistic dampening.
        await self._maybe_circadian_sleep()

        # 3. SESSION_BREAK — periodic forced pause.
        await self._maybe_session_break()

        # 4. Hand off to the base limiter (per-host token bucket + global sem).
        async with super().acquire(host):
            yield

    # ------------------------------------------------------------------
    # Public introspection (used by tests + status command).
    # ------------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def cooldown_remaining_sec(self) -> float:
        if self._state != State.COOLDOWN:
            return 0.0
        return max(0.0, self._cooldown_until_monotonic - time.monotonic())

    @property
    def consecutive_successes(self) -> int:
        return self._consecutive_successes

    def get_host_rate(self, host: str) -> float:
        """Return current effective rate for a host (post-DEGRADED scaling)."""
        bucket = self._buckets.get(host)
        if bucket is None:
            return self._default_rate
        return bucket.rate_per_sec

    # ------------------------------------------------------------------
    # State transitions (must be called holding _state_lock).
    # ------------------------------------------------------------------

    def _enter_degraded(self, *, reason: str) -> None:
        prev = self._state
        factor = random.uniform(*self._adaptive_config.degraded_factor_range)  # noqa: S311 — jitter, not crypto
        self._state = State.DEGRADED
        self._consecutive_successes = 0
        # Scale every host bucket's rate. Idempotent — called from NORMAL only.
        for host, baseline in self._baseline_rates.items():
            self._set_bucket_rate(host, baseline * factor)
        # Default rate (used for unknown hosts) also scales.
        self._default_rate = min(
            (b.rate_per_sec for b in self._buckets.values()),
            default=self._default_rate * factor,
        )
        logger.warning(
            "rate_limit_state_change",
            previous=prev.value,
            current=State.DEGRADED.value,
            reason=reason,
            factor=round(factor, 3),
        )

    def _enter_cooldown(self, *, reason: str, now: float) -> None:
        prev = self._state
        duration = random.uniform(*self._adaptive_config.cooldown_duration_sec)  # noqa: S311
        duration = min(duration, _MAX_COOLDOWN_SEC)
        self._state = State.COOLDOWN
        self._cooldown_until_monotonic = now + duration
        # Reset streaks so we re-evaluate cleanly when cooldown clears.
        self._consecutive_429 = 0
        self._consecutive_403 = 0
        self._consecutive_successes = 0
        logger.warning(
            "rate_limit_state_change",
            previous=prev.value,
            current=State.COOLDOWN.value,
            reason=reason,
            cooldown_sec=round(duration, 1),
        )

    def _enter_normal(self, *, reason: str) -> None:
        prev = self._state
        self._state = State.NORMAL
        self._cooldown_until_monotonic = 0.0
        # Restore baseline rates.
        for host, baseline in self._baseline_rates.items():
            self._set_bucket_rate(host, baseline)
        self._default_rate = min(self._baseline_rates.values(), default=1.0)
        logger.info(
            "rate_limit_state_change",
            previous=prev.value,
            current=State.NORMAL.value,
            reason=reason,
        )

    def _set_bucket_rate(self, host: str, rate: float) -> None:
        bucket = self._buckets.get(host)
        if bucket is None:
            self._buckets[host] = _HostBucket(rate_per_sec=rate)
            return
        bucket.rate_per_sec = rate

    # ------------------------------------------------------------------
    # Acquire-time dampening helpers.
    # ------------------------------------------------------------------

    async def _wait_for_cooldown_clear(self) -> None:
        # Compare-without-lock: cooldown_until_monotonic is monotonic and
        # only ever moves forward inside _enter_cooldown / clears in
        # _enter_normal — racing reads return safe stale values.
        while True:
            remaining = self.cooldown_remaining_sec
            if remaining <= 0:
                # Drop the COOLDOWN state if we slept through its expiry.
                if self._state == State.COOLDOWN:
                    async with self._state_lock:
                        if (
                            self._state == State.COOLDOWN
                            and self.cooldown_remaining_sec <= 0
                        ):
                            self._enter_normal(reason="cooldown_elapsed")
                return
            await asyncio.sleep(min(remaining, 5.0))

    async def _maybe_circadian_sleep(self) -> None:
        if not self._adaptive_config.circadian_enabled:
            return
        if self._circadian_window is None:
            return
        prob = self._circadian_skip_probability(datetime.now(self._circadian_tz))
        if prob <= 0.0:
            return
        roll = random.random()  # noqa: S311 — circadian dampening, not crypto
        if roll < prob:
            # Sleep ~1 minute and re-check; over a long sleep window this
            # accumulates without holding state.
            await asyncio.sleep(60.0)

    def _circadian_skip_probability(self, now_local: datetime) -> float:
        """Return probability of skipping this request given local time.

        - Inside the sleep window: full `circadian_sleep_probability`.
        - Inside the twilight band on either side: linear ramp 0..p.
        - Outside both: 0.
        """
        if self._circadian_window is None:
            return 0.0
        start, end = self._circadian_window
        cur = now_local.timetz().replace(tzinfo=None)
        cfg = self._adaptive_config
        twilight = cfg.circadian_twilight_hours

        # Convert to seconds-since-midnight for arithmetic.
        def to_sec(t: dtime) -> float:
            return t.hour * 3600.0 + t.minute * 60.0 + t.second

        cur_s = to_sec(cur)
        start_s = to_sec(start)
        end_s = to_sec(end)
        twilight_s = twilight * 3600.0

        # Handle wrap-around (e.g., 22:00-06:00 spans midnight).
        if end_s <= start_s:
            inside = cur_s >= start_s or cur_s < end_s
            # Twilight bands at start - twilight  and  end + twilight,
            # carefully wrapped.
            pre_start = (start_s - twilight_s) % 86400.0
            post_end = (end_s + twilight_s) % 86400.0
            in_pre_twilight = (
                cur_s >= pre_start and cur_s < start_s
                if pre_start < start_s
                else (cur_s >= pre_start or cur_s < start_s)
            )
            in_post_twilight = (
                cur_s >= end_s and cur_s < post_end
                if end_s < post_end
                else (cur_s >= end_s or cur_s < post_end)
            )
        else:
            inside = start_s <= cur_s < end_s
            in_pre_twilight = max(0.0, start_s - twilight_s) <= cur_s < start_s
            in_post_twilight = end_s <= cur_s < min(86400.0, end_s + twilight_s)

        if inside:
            return cfg.circadian_sleep_probability
        if twilight_s <= 0:
            # No twilight band configured → outside-window means zero probability.
            return 0.0
        if in_pre_twilight:
            # Linear ramp 0 → p over the band leading into start.
            distance = self._wrap_distance(cur_s, start_s, twilight_s)
            return cfg.circadian_sleep_probability * (1.0 - distance / twilight_s)
        if in_post_twilight:
            distance = self._wrap_distance(end_s, cur_s, twilight_s)
            return cfg.circadian_sleep_probability * (1.0 - distance / twilight_s)
        return 0.0

    @staticmethod
    def _wrap_distance(from_sec: float, to_sec: float, max_band_sec: float) -> float:
        """Distance from `from_sec` to `to_sec` in seconds, wrapping at midnight,
        clamped to [0, max_band_sec]."""
        diff = (to_sec - from_sec) % 86400.0
        return min(diff, max_band_sec)

    async def _maybe_session_break(self) -> None:
        cfg = self._adaptive_config
        async with self._state_lock:
            self._request_count_since_break += 1
            if self._request_count_since_break < cfg.session_break_every_requests:
                return
            self._request_count_since_break = 0
            duration = random.uniform(*cfg.session_break_duration_sec)  # noqa: S311
        logger.info(
            "rate_limit_session_break",
            duration_sec=round(duration, 1),
        )
        await asyncio.sleep(duration)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_p95_latency(self) -> float | None:
        if len(self._latencies_ms) < 10:
            return None
        ordered = sorted(self._latencies_ms)
        idx = max(0, int(len(ordered) * 0.95) - 1)
        return ordered[idx]

    @staticmethod
    def _parse_window(spec: str) -> tuple[dtime, dtime]:
        """Parse `HH:MM-HH:MM` into a pair of datetime.time. Wrap-around OK."""
        try:
            left, right = spec.split("-", 1)
            sh, sm = (int(p) for p in left.strip().split(":", 1))
            eh, em = (int(p) for p in right.strip().split(":", 1))
            return dtime(sh, sm), dtime(eh, em)
        except (ValueError, AttributeError) as e:
            raise ValueError(
                f"adaptive.circadian.sleep_window={spec!r} is malformed; "
                "expected 'HH:MM-HH:MM' (24-hour)"
            ) from e
