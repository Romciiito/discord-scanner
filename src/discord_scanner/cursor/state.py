"""Per-channel cursor state — sqlite-backed, single-writer, fsync-safe.

Traces to:
- seed-spec.md §2.7 (state / cursor)
- workplan.md Phase 5 done definition + REQ-F-023/024
- security-model.md §6 SEC-P0-22 (chmod 0o600 on state files)
- claude-rules.md MUST "SQLite parameterisation", "Cross-platform locking"
- workspace plan §"Part A — A.3 Phase C: Backward Backfill"

Schema (v2 — additive migration on existing v1 DBs):
    cursor(
      guild_id TEXT NOT NULL,
      channel_id TEXT NOT NULL,
      last_message_id TEXT,                  -- v1: forward cursor (newest seen)
      updated_at TEXT NOT NULL,
      -- v2 additive columns:
      oldest_seen_message_id TEXT,           -- backward cursor; None until first backfill
      newest_seen_message_id TEXT,           -- alias for last_message_id (kept in sync)
      oldest_seen_at TEXT,                   -- timestamp from oldest message we've seen
      backfill_complete INTEGER NOT NULL DEFAULT 0,
      backfill_started_at TEXT,
      backfill_runs INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (guild_id, channel_id)
    )

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
from dataclasses import dataclass
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

# v2 + v3 additive migrations. Each is a (column-name, ALTER-TABLE-statement)
# pair — applied only if the column is missing per PRAGMA table_info
# introspection. Pure SQL string literals (no f-strings or % formatting) —
# passes the `tests/test_cursor.py:test_no_f_string_or_percent_sql_in_sqlite_modules`
# regex test that scans this module.
_MIGRATIONS: Final[tuple[tuple[str, str], ...]] = (
    ("oldest_seen_message_id",
     "ALTER TABLE cursor ADD COLUMN oldest_seen_message_id TEXT"),
    ("newest_seen_message_id",
     "ALTER TABLE cursor ADD COLUMN newest_seen_message_id TEXT"),
    ("oldest_seen_at",
     "ALTER TABLE cursor ADD COLUMN oldest_seen_at TEXT"),
    ("backfill_complete",
     "ALTER TABLE cursor ADD COLUMN backfill_complete INTEGER NOT NULL DEFAULT 0"),
    ("backfill_started_at",
     "ALTER TABLE cursor ADD COLUMN backfill_started_at TEXT"),
    ("backfill_runs",
     "ALTER TABLE cursor ADD COLUMN backfill_runs INTEGER NOT NULL DEFAULT 0"),
    # v3 — multi-burner ownership tracking (M.1). Default '' for legacy
    # rows; backfilled to the operator's `auth.keyring_username` on first
    # multi-burner init. PK stays at (guild_id, channel_id) — application
    # layer guarantees one burner per channel via scope→burner ownership.
    # Re-assignment of a channel to a new burner uses INSERT OR REPLACE
    # which updates the burner_id column atomically.
    ("burner_id",
     "ALTER TABLE cursor ADD COLUMN burner_id TEXT NOT NULL DEFAULT ''"),
)

# Lookup index for (burner_id, guild_id) — accelerates the per-burner
# scan loop's "what channels does this burner own?" query. Non-unique;
# the (guild_id, channel_id) PK provides uniqueness.
_BURNER_INDEX_DDL: Final[str] = (
    "CREATE INDEX IF NOT EXISTS ix_cursor_burner "
    "ON cursor(burner_id, guild_id)"
)


@dataclass(frozen=True)
class CursorFrontier:
    """Both ends of a channel's scanned history.

    None values mean "never scanned in that direction yet" (or pre-v2 row
    with no oldest_seen populated until the first backfill run).
    """

    last_message_id: str | None        # v1 alias: most recent forward-end
    oldest_seen_message_id: str | None
    newest_seen_message_id: str | None
    backfill_complete: bool
    backfill_runs: int


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
        # v2 schema migration: additive columns, idempotent. PRAGMA
        # table_info introspection avoids touching SQL string interpolation
        # rules (the `_MIGRATIONS` SQL is pure literals — see module docstring).
        existing_cols: set[str] = {
            row[1] for row in self._conn.execute("PRAGMA table_info(cursor)")
        }
        for col_name, ddl in _MIGRATIONS:
            if col_name not in existing_cols:
                self._conn.execute(ddl)
        # v3 burner index — idempotent, runs every init.
        self._conn.execute(_BURNER_INDEX_DDL)
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
        """Return the last-persisted `last_message_id` for a channel, or None.

        Note: this method does NOT filter by burner_id — the (guild_id,
        channel_id) PK ensures uniqueness across the table. Multi-burner
        callers should still pass burner_id to `advance()` so ownership
        tracking is recorded; reads use this getter unchanged.
        """
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
        *,
        burner_id: str = "",
    ) -> None:
        """Atomically advance the cursor AFTER a successful channel dump.

        Call this only after the JSONL file has been fsync'd to disk — otherwise
        a crash between cursor-advance and file-flush would lose messages on
        the next run (seed-spec §2.7: "Updated AFTER successful message dump").

        v2: also updates `newest_seen_message_id` to keep the dual-cursor view
        consistent. `oldest_seen_message_id` is left untouched (advance() is
        forward-only; backfill uses `advance_backward()`).

        v3 (M.1): records `burner_id` for ownership tracking. Default ''
        preserves v1/v2 back-compat for single-burner callers.
        """
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO cursor "
            "(guild_id, channel_id, last_message_id, newest_seen_message_id, "
            " burner_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, channel_id) DO UPDATE SET "
            "  last_message_id = excluded.last_message_id, "
            "  newest_seen_message_id = excluded.newest_seen_message_id, "
            "  burner_id = excluded.burner_id, "
            "  updated_at = excluded.updated_at",
            (
                guild_id,
                channel_id,
                last_message_id,
                last_message_id,
                burner_id,
                now,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # v2 — bidirectional cursor methods (workspace plan §"Part A — A.3").
    # ------------------------------------------------------------------

    def get_frontier(self, guild_id: str, channel_id: str) -> CursorFrontier:
        """Return both-ends frontier for a channel.

        For pre-v2 rows (where backfill columns default to None/0), this still
        returns a coherent CursorFrontier with `oldest_seen_message_id=None`,
        meaning "backfill has never run on this channel". The first
        `advance_backward()` call seeds it.
        """
        row = self._conn.execute(
            "SELECT last_message_id, oldest_seen_message_id, newest_seen_message_id, "
            "       backfill_complete, backfill_runs "
            "FROM cursor WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        if row is None:
            return CursorFrontier(None, None, None, backfill_complete=False, backfill_runs=0)
        last, oldest, newest, complete, runs = row
        return CursorFrontier(
            last_message_id=last,
            oldest_seen_message_id=oldest,
            newest_seen_message_id=newest if newest is not None else last,
            backfill_complete=bool(complete),
            backfill_runs=int(runs) if runs is not None else 0,
        )

    def advance_backward(
        self,
        guild_id: str,
        channel_id: str,
        oldest_message_id: str,
        oldest_message_at: str | None = None,
        *,
        burner_id: str = "",
    ) -> None:
        """Update `oldest_seen_message_id` after a successful backward dump.

        Call AFTER fsync of the dump file (same durability rule as `advance`).
        Idempotent: monotonic-decreasing semantics — passing a NEWER id than
        the current `oldest_seen_message_id` is silently ignored (the existing
        oldest stays). This matches the invariant that backfill walks
        backwards through history.

        Initialises `backfill_started_at` on the first call.
        """
        now = _now_iso()
        existing = self._conn.execute(
            "SELECT oldest_seen_message_id, backfill_started_at "
            "FROM cursor WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        # Determine started_at — preserved across calls.
        started_at: str | None
        if existing is not None and existing[1]:
            started_at = existing[1]
        else:
            started_at = now
        # Monotonic-decreasing guard: only accept a new oldest that is
        # snowflake-numerically-LESS-than the current oldest (or current is
        # None == accept anything).
        new_oldest = oldest_message_id
        if existing is not None and existing[0] is not None:
            current_oldest = existing[0]
            if _snowflake_less(current_oldest, new_oldest):
                # Current oldest is already older than the candidate — keep it.
                return
        self._conn.execute(
            "INSERT INTO cursor "
            "(guild_id, channel_id, oldest_seen_message_id, oldest_seen_at, "
            " backfill_started_at, burner_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, channel_id) DO UPDATE SET "
            "  oldest_seen_message_id = excluded.oldest_seen_message_id, "
            "  oldest_seen_at = excluded.oldest_seen_at, "
            "  backfill_started_at = COALESCE(cursor.backfill_started_at, excluded.backfill_started_at), "
            "  burner_id = excluded.burner_id, "
            "  updated_at = excluded.updated_at",
            (
                guild_id,
                channel_id,
                new_oldest,
                oldest_message_at,
                started_at,
                burner_id,
                now,
            ),
        )
        self._conn.commit()

    def increment_backfill_runs(
        self,
        guild_id: str,
        channel_id: str,
        *,
        burner_id: str = "",
    ) -> int:
        """Bump `backfill_runs` counter for safety bound enforcement.

        Returns the NEW count after increment. Pre-v2 rows with no row at all
        are upserted with runs=1. Caller can compare against
        `backfill.max_scan_runs_per_channel`.
        """
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO cursor "
            "(guild_id, channel_id, backfill_runs, burner_id, updated_at) "
            "VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT (guild_id, channel_id) DO UPDATE SET "
            "  backfill_runs = cursor.backfill_runs + 1, "
            "  burner_id = excluded.burner_id, "
            "  updated_at = excluded.updated_at",
            (guild_id, channel_id, burner_id, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT backfill_runs FROM cursor "
            "WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def mark_backfilled(self, guild_id: str, channel_id: str) -> None:
        """Mark a channel as fully backfilled (reached channel start)."""
        now = _now_iso()
        self._conn.execute(
            "UPDATE cursor SET backfill_complete = 1, updated_at = ? "
            "WHERE guild_id = ? AND channel_id = ?",
            (now, guild_id, channel_id),
        )
        self._conn.commit()

    def all_rows(self) -> list[tuple[str, str, str | None, str]]:
        """Return `(guild_id, channel_id, last_message_id, updated_at)` rows
        sorted by `(guild_id, channel_id)` for deterministic output.

        Read-only — used by the `status` command. Backwards-compatible: the
        returned tuple shape is unchanged from v1. Use `all_frontiers()` for
        the v2 backfill view.
        """
        return list(
            self._conn.execute(
                "SELECT guild_id, channel_id, last_message_id, updated_at "
                "FROM cursor "
                "ORDER BY guild_id ASC, channel_id ASC"
            )
        )

    def all_frontiers(self) -> list[tuple[str, str, CursorFrontier]]:
        """Return `(guild_id, channel_id, CursorFrontier)` rows for the v2
        status command extension. Read-only."""
        rows = self._conn.execute(
            "SELECT guild_id, channel_id, last_message_id, "
            "       oldest_seen_message_id, newest_seen_message_id, "
            "       backfill_complete, backfill_runs "
            "FROM cursor "
            "ORDER BY guild_id ASC, channel_id ASC"
        ).fetchall()
        out: list[tuple[str, str, CursorFrontier]] = []
        for r in rows:
            gid, cid, last, oldest, newest, complete, runs = r
            out.append(
                (
                    gid,
                    cid,
                    CursorFrontier(
                        last_message_id=last,
                        oldest_seen_message_id=oldest,
                        newest_seen_message_id=newest if newest is not None else last,
                        backfill_complete=bool(complete),
                        backfill_runs=int(runs) if runs is not None else 0,
                    ),
                )
            )
        return out


def _now_iso() -> str:
    """UTC ISO-8601 timestamp, second precision — deterministic enough for ops."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _snowflake_less(a: str, b: str) -> bool:
    """Return True iff snowflake `a` is older (numerically smaller) than `b`.

    Used by `advance_backward` to enforce monotonic-decreasing oldest cursor:
    if the existing oldest is ALREADY older than the candidate, keep existing.
    Numeric compare avoids the 17-/18-digit string-sort pitfall noted in
    fetch/messages.py.
    """
    if not a or not b:
        # Empty strings — defer to the "accept anything" branch in caller.
        return False
    if not a.isdigit() or not b.isdigit():
        # Malformed ID — fall back to lexicographic; safer than crashing.
        return a < b
    return int(a) < int(b)
