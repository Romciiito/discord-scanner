"""Scan orchestration package — production assembly of the building blocks.

Public surface:
- `run_one_pass(settings, token, guild_filter=None) -> list[RunResult]`
- `scan_one_channel(resources, guild_id, channel, settings, scan_date) -> ChannelResult`
- `_open_shared_resources(settings, token)` — async context manager (internal)
- Dataclasses: `ChannelResult`, `RunResult`, `ScanCounters`

A.0.1 ships forward-only happy path + per-channel error isolation. A.0.2 hardens
the exception matrix. A.0.3 adds the backfill direction. A.0.4 wires the
AdaptiveRateLimiter recording hook. A.0.5 plugs into the daemon.
"""

from __future__ import annotations

from discord_scanner.scan.discover import (
    DiscoveredGuild,
    DiscoverReport,
    apply_discover_report_to_scopes,
    discover_guilds,
    render_discover_report_table,
)
from discord_scanner.scan.orchestrator import (
    ChannelResult,
    FatalScanError,
    RunResult,
    run_one_pass,
    scan_one_channel,
)
from discord_scanner.scan.scopes import (
    ScopeMap,
    ScopeProfile,
    add_guilds_to_scope,
    load_scope_profiles,
)

__all__ = [
    "ChannelResult",
    "DiscoverReport",
    "DiscoveredGuild",
    "FatalScanError",
    "RunResult",
    "ScopeMap",
    "ScopeProfile",
    "add_guilds_to_scope",
    "apply_discover_report_to_scopes",
    "discover_guilds",
    "load_scope_profiles",
    "render_discover_report_table",
    "run_one_pass",
    "scan_one_channel",
]
