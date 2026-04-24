"""Per-channel + per-scan jitter helpers.

Traces to:
- seed-spec.md §4.5 (jitter, burst pauses, daemon jitter)
- claude-rules.md MUST "Rate-limit + jitter" + MUST-NOT "No time.sleep in async"

All sleep calls go through `session/rate_limit.sleep_with_jitter` /
`burst_pause` so the grep guard remains happy. This module adds the
**daemon mode** jitter helper used by Phase 9.
"""

from __future__ import annotations

import random

from discord_scanner.config import Settings
from discord_scanner.session.rate_limit import burst_pause, sleep_with_jitter

# Re-exports kept for API ergonomics in P6/P9.
__all__ = [
    "burst_pause",
    "daemon_next_sleep_seconds",
    "sleep_with_jitter",
]


def daemon_next_sleep_seconds(settings: Settings) -> float:
    """Compute the next daemon sleep duration with seed-spec §4.5 jitter.

    Returns seconds; caller uses `await asyncio.sleep(...)` (P9 scan loop).
    """
    base_sec = settings.daemon.interval_hours * 3600.0
    low, high = settings.daemon.jitter_hours
    jitter_sec = random.uniform(float(low), float(high)) * 3600.0  # noqa: S311
    return max(0.0, base_sec + jitter_sec)
