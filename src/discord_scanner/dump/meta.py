"""meta.json builder — per-guild, per-date scan metadata.

Traces to:
- seed-spec.md §2.8 / §7.2 (meta.json schema)

One record per guild-date. Contains run-level counters consumed by Stage 3
to skip unchanged scans + pipeline dashboards.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from discord_scanner.dump.jsonl_writer import write_json


@dataclass
class ScanCounters:
    """Integer counters populated during a scan."""

    channels_scanned: int = 0
    messages_fetched: int = 0
    pinned_fetched: int = 0
    threads_scanned: int = 0
    thread_messages_fetched: int = 0
    attachments_downloaded: int = 0
    attachments_skipped_oversize: int = 0
    attachments_skipped_mime: int = 0
    attachments_skipped_ext: int = 0
    http_429_seen: int = 0
    channel_aborts: int = 0


@dataclass
class ScanMeta:
    """Top-level shape for `meta.json`."""

    schema_version: int = 1
    guild_id: str = ""
    guild_name: str = ""
    scan_date: str = ""
    scan_started_at: str = ""
    scan_finished_at: str = ""
    duration_sec: float = 0.0
    counters: ScanCounters = field(default_factory=ScanCounters)
    scanner_version: str = "0.1.0"


def write_meta(path: Path, meta: ScanMeta) -> None:
    """Write `meta` to `path` as deterministic JSON."""
    write_json(path, asdict(meta))


def now_iso() -> str:
    """UTC ISO-8601 timestamp — second precision for deterministic tests via
    `freezegun` (not yet a dep; use monkeypatch of `time.time()` for now)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
