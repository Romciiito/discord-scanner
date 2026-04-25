"""Per-channel cursor state — sqlite-backed, single-writer, fsync-safe.

Traces to:
- seed-spec.md §2.7 (state / cursor)
- workplan.md Phase 5 done definition + REQ-F-023/024
- security-model.md §6 SEC-P0-22 (chmod 0o600 on state files)
- claude-rules.md MUST "SQLite parameterisation", "Cross-platform locking"

Schema: `cursor(guild_id TEXT, channel_id TEXT, last_message_id TEXT,
updated_at TEXT, PRIMARY KEY(guild_id, channel_id))`.

Single-writer model: `CursorLock` (see `cursor/lock.py`) uses
`filelock.FileLock(state/cursor.lock, timeout=0)`. A second concurrent scan
with the same state-root will refuse to start (matches gateway FSM's
SEC-P0-13 model). Under that guarantee, SELECT + UPDATE + COMMIT + fsync
are effectively atomic.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Final

from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS cursor (
    guild_id          TEXT NOT NULL,
    channel_id        TEXT NOT NULL,
    last_message_id   TEXT,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
CREATE INDEX IF NOT EXISTS ix_cursor_updated ON cursor(updated_at);
"""


class CursorStore:
    """Typed wrapper over the `cursor` sqlite table.

    The caller owns the filelock (see `cursor/lock.py`). This class assumes
    it has exclusive write access.
    """

    def __init__(self, state_root: Path) -> None:
        from discord_scanner._paths import secure_mkdir

        secure_mkdir(state_root)
        self._path = state_root / "cursor.sqlite"
        fresh = not self._path.exists()
        self._conn = sqlite3.connect(self._path)
        # WAL gives us durable single-writer + concurrent readers (status cmd
        # can read while a scan writes). isolation_level=None lets us control
        # transactions explicitly via BEGIN/COMMIT below.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        if fresh:
            try:
                os.chmod(self._path, 0o600)
            except OSError as e:
                logger.debug("chmod_best_effort_failed", path=str(self._path), err=str(e))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CursorStore:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def get(self, guild_id: str, channel_id: str) -> str | None:
        """Return the last-persisted `last_message_id` for a channel, or None."""
        row = self._conn.execute(
            "SELECT last_message_id FROM cursor WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        return row[0] if row is not None else None

    def advance(
        self,
        guild_id: str,
        channel_id: str,
        last_message_id: str,
    ) -> None:
        """Atomically advance the cursor AFTER a successful channel dump.

        Call this only after the JSONL file has been fsync'd to disk — otherwise
        a crash between cursor-advance and file-flush would lose messages on
        the next run (seed-spec §2.7: "Updated AFTER successful message dump").
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO cursor "
            "(guild_id, channel_id, last_message_id, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, channel_id, last_message_id, _now_iso()),
        )
        self._conn.commit()

    def all_rows(self) -> list[tuple[str, str, str | None, str]]:
        """Return `(guild_id, channel_id, last_message_id, updated_at)` rows
        sorted by `(guild_id, channel_id)` for deterministic output.

        Read-only — used by the `status` command.
        """
        return list(
            self._conn.execute(
                "SELECT guild_id, channel_id, last_message_id, updated_at "
                "FROM cursor "
                "ORDER BY guild_id ASC, channel_id ASC"
            )
        )


def _now_iso() -> str:
    """UTC ISO-8601 timestamp, second precision — deterministic enough for ops."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
