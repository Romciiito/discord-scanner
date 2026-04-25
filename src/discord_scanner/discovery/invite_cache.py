"""sqlite-backed invite-resolution cache with 7-day TTL.

Traces to:
- seed-spec.md §2.1 (7-day resolution cache)
- security-model.md §6 SEC-P0-29 (no Discord write endpoints)
- claude-rules.md MUST "SQLite parameterisation" — every query uses `?` placeholders

Concurrency model: this is a **single-writer** cache. `discord-scanner` is a
single-user CLI — only one scan runs at a time, and the process-wide filelock
in `session/gateway.py` prevents a second scan from starting in parallel.
Under that assumption the SELECT-then-DELETE in `get()` is effectively atomic
even without an explicit `BEGIN IMMEDIATE`. If P9 ever introduces concurrent
writers within one scan, revisit this to use explicit transactions.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Final

from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import ResolvedInvite

logger = get_logger(__name__)

TTL_SECONDS: Final[int] = 7 * 24 * 60 * 60  # 7 days

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS invites (
    code           TEXT PRIMARY KEY,
    guild_id       TEXT,
    guild_name     TEXT,
    expires_at     TEXT,
    member_count   INTEGER,
    cached_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_invites_cached_at ON invites(cached_at);
"""


class InviteCache:
    """Thin sqlite wrapper — opens/creates the cache DB on first use."""

    def __init__(self, state_root: Path) -> None:
        from discord_scanner._paths import secure_mkdir

        secure_mkdir(state_root)
        self._path = state_root / "invite_cache.sqlite"
        fresh = not self._path.exists()
        self._conn = sqlite3.connect(self._path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        if fresh:
            try:
                os.chmod(self._path, 0o600)
            except OSError as e:
                logger.debug("chmod_best_effort_failed", path=str(self._path), err=str(e))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> InviteCache:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def get(self, code: str) -> ResolvedInvite | None:
        """Return cached ResolvedInvite if within TTL, else delete the stale row."""
        cutoff = int(time.time()) - TTL_SECONDS
        row = self._conn.execute(
            "SELECT code, guild_id, guild_name, expires_at, member_count, cached_at "
            "FROM invites WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None:
            return None
        if row[5] < cutoff:
            # stale — delete in same transaction before the network fetch
            self._conn.execute("DELETE FROM invites WHERE code = ?", (code,))
            self._conn.commit()
            return None
        return ResolvedInvite(
            code=row[0],
            guild_id=row[1],
            guild_name=row[2],
            expires_at=row[3],
            approximate_member_count=row[4],
        )

    def put(self, resolved: ResolvedInvite) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO invites "
            "(code, guild_id, guild_name, expires_at, member_count, cached_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                resolved.code,
                resolved.guild_id,
                resolved.guild_name,
                resolved.expires_at,
                resolved.approximate_member_count,
                int(time.time()),
            ),
        )
        self._conn.commit()
