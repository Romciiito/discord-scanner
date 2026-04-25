"""Tests for DaemonLoop — iteration count, shutdown on signal, error survival.

Traces to: workplan.md Phase 9.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from discord_scanner.config import Settings, load_config
from discord_scanner.daemon import DaemonLoop, DaemonStats


@pytest.mark.asyncio
async def test_run_forever_respects_max_iterations(
    tmp_config_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_iterations=2 → exactly 2 scan_fn calls, then return."""
    cfg = load_config(tmp_config_yaml)
    # tighten sleep to near-zero
    cfg.daemon.interval_hours = 0
    cfg.daemon.jitter_hours = (0, 0)

    calls: list[int] = []

    async def _scan(_settings: Settings) -> None:
        calls.append(1)

    loop = DaemonLoop(cfg, _scan)
    await asyncio.wait_for(loop.run_forever(max_iterations=2), timeout=5.0)
    assert len(calls) == 2
    assert loop.stats.iterations == 2


@pytest.mark.asyncio
async def test_run_forever_survives_scan_error(
    tmp_config_yaml: Path,
) -> None:
    """A scan that raises does not crash the loop; error is captured."""
    cfg = load_config(tmp_config_yaml)
    cfg.daemon.interval_hours = 0
    cfg.daemon.jitter_hours = (0, 0)

    async def _bad_scan(_settings: Settings) -> None:
        raise RuntimeError("scan boom")

    loop = DaemonLoop(cfg, _bad_scan)
    await asyncio.wait_for(loop.run_forever(max_iterations=2), timeout=5.0)
    assert loop.stats.iterations == 2
    assert "scan boom" in (loop.stats.last_scan_error or "")


@pytest.mark.asyncio
async def test_shutdown_event_exits_loop(tmp_config_yaml: Path) -> None:
    """request_shutdown() from outside the scan function exits cleanly."""
    cfg = load_config(tmp_config_yaml)
    cfg.daemon.interval_hours = 1
    cfg.daemon.jitter_hours = (0, 0)

    async def _scan(_settings: Settings) -> None:
        pass

    loop = DaemonLoop(cfg, _scan)

    async def _trigger() -> None:
        await asyncio.sleep(0.05)
        loop.request_shutdown()

    await asyncio.wait_for(asyncio.gather(loop.run_forever(), _trigger()), timeout=5.0)
    assert loop.stats.iterations >= 1


@pytest.mark.asyncio
async def test_retention_runs_before_each_scan(
    tmp_config_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every iteration prunes the output root."""
    cfg = load_config(tmp_config_yaml)
    cfg.daemon.interval_hours = 0
    cfg.daemon.jitter_hours = (0, 0)

    prune_calls: list[dict[str, Any]] = []

    def _fake_prune(output_root: Path, *, keep_days: int) -> dict[str, int]:
        prune_calls.append({"root": output_root, "keep": keep_days})
        return {
            "scanned": 0,
            "deleted": 0,
            "skipped_symlink": 0,
            "skipped_non_date": 0,
            "refused_escape": 0,
        }

    import discord_scanner.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "prune_output", _fake_prune)

    async def _scan(_settings: Settings) -> None:
        pass

    loop = DaemonLoop(cfg, _scan, retention_keep_days=7)
    await asyncio.wait_for(loop.run_forever(max_iterations=3), timeout=5.0)
    assert len(prune_calls) == 3
    assert prune_calls[0]["keep"] == 7


def test_stats_returns_copy(tmp_config_yaml: Path) -> None:
    """`DaemonLoop.stats` MUST return a fresh `DaemonStats` each call so tests
    cannot accidentally mutate the loop's internal counter."""
    cfg = load_config(tmp_config_yaml)

    async def _noop_scan(_s: Settings) -> None:
        return None

    loop = DaemonLoop(cfg, _noop_scan)
    snapshot = loop.stats
    snapshot.iterations = 999
    # Mutating the snapshot must not affect the loop's internal state.
    assert loop.stats.iterations == 0
    # Sanity: `DaemonStats` is still constructible directly with field values.
    direct = DaemonStats(iterations=5, last_scan_error="x", sleep_seconds_last=1.0)
    assert direct.iterations == 5


@pytest.mark.asyncio
async def test_max_iterations_zero_is_no_op(tmp_config_yaml: Path) -> None:
    """max_iterations=0 → no scan calls."""
    cfg = load_config(tmp_config_yaml)
    cfg.daemon.interval_hours = 0
    cfg.daemon.jitter_hours = (0, 0)

    calls: list[int] = []

    async def _scan(_settings: Settings) -> None:
        calls.append(1)

    loop = DaemonLoop(cfg, _scan)
    await asyncio.wait_for(loop.run_forever(max_iterations=0), timeout=5.0)
    assert calls == []
