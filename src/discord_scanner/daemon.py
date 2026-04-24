"""Long-running scan loop with interval jitter + SIGINT/SIGTERM clean shutdown.

Traces to:
- seed-spec.md §3 (daemon: interval_hours, jitter_hours, scan_start_window)
- workplan.md Phase 9 done definition
- claude-rules.md MUST "Rate-limit + jitter"

This module provides the skeleton. The actual per-guild scan function
(`ScanFn`) is injected — in Phase 10 smoke / CLI wiring it's the closure
that orchestrates: retention prune → gateway connect → resolve invites →
list guilds → per-channel fetch → dump. Keeping the loop decoupled from
the scan implementation makes tests straightforward.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from discord_scanner.config import Settings
from discord_scanner.fetch.jitter import daemon_next_sleep_seconds
from discord_scanner.logging_conf import get_logger
from discord_scanner.retention import prune_output

logger = get_logger(__name__)

ScanFn = Callable[[Settings], Awaitable[None]]


@dataclass
class DaemonStats:
    """Observable daemon-loop state for tests + `status` command."""

    iterations: int = 0
    last_scan_error: str | None = None
    sleep_seconds_last: float = 0.0


class DaemonLoop:
    """Run `scan_fn(settings)` repeatedly with configured jitter between runs.

    Registers SIGINT / SIGTERM handlers (POSIX only — on Windows SIGINT via
    Ctrl-C still works but SIGTERM cannot be installed). A flag-based loop
    checks `self._shutdown` on each wake-up.
    """

    def __init__(
        self,
        settings: Settings,
        scan_fn: ScanFn,
        *,
        retention_keep_days: int | None = None,
    ) -> None:
        self._settings = settings
        self._scan_fn = scan_fn
        self._shutdown = asyncio.Event()
        self._stats = DaemonStats()
        # Default to config value; caller can override for tests.
        self._retention_keep_days = (
            retention_keep_days
            if retention_keep_days is not None
            else settings.retention.raw_dump_keep_days
        )

    @property
    def stats(self) -> DaemonStats:
        """Return a copy of current stats (tests should not mutate internal state)."""
        return DaemonStats(
            iterations=self._stats.iterations,
            last_scan_error=self._stats.last_scan_error,
            sleep_seconds_last=self._stats.sleep_seconds_last,
        )

    def request_shutdown(self) -> None:
        """Signal the loop to exit on next wake-up (tests + signal handlers)."""
        self._shutdown.set()

    async def run_forever(self, *, max_iterations: int | None = None) -> None:
        """Loop: retention prune → scan → sleep(jitter). Exits on shutdown.

        `max_iterations` exists for tests; production runs pass None.
        """
        self._install_signal_handlers()
        try:
            while not self._shutdown.is_set():
                if max_iterations is not None and self._stats.iterations >= max_iterations:
                    return
                self._run_retention(self._settings.run.output_root)
                try:
                    await self._scan_fn(self._settings)
                    self._stats.last_scan_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — daemon must survive
                    logger.error("scan_iteration_failed", err=str(e))
                    self._stats.last_scan_error = str(e)
                finally:
                    self._stats.iterations += 1

                if self._shutdown.is_set():
                    return
                sleep_sec = daemon_next_sleep_seconds(self._settings)
                self._stats.sleep_seconds_last = sleep_sec
                logger.info(
                    "daemon_sleep",
                    seconds=sleep_sec,
                    iteration=self._stats.iterations,
                )
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_sec)
                    return  # shutdown was set during sleep
                except TimeoutError:
                    continue
        finally:
            self._uninstall_signal_handlers()

    def _run_retention(self, output_root: Path) -> None:
        try:
            counters = prune_output(output_root, keep_days=self._retention_keep_days)
            logger.info("retention_counters", **counters)
        except Exception as e:  # noqa: BLE001 — retention failure must not kill daemon
            logger.error("retention_failed", err=str(e))

    # ------------------------------------------------------------------
    # Signal handling

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except (NotImplementedError, RuntimeError) as e:
                # Windows ProactorEventLoop doesn't support add_signal_handler;
                # Ctrl-C still raises KeyboardInterrupt into asyncio.run().
                logger.debug("signal_handler_not_installed", sig=sig_name, err=str(e))

    def _uninstall_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)
