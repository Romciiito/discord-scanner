"""Tests for cursor state + filelock.

Traces to: workplan.md Phase 5, SEC-P0-22 (state file perms), claude-rules
MUST "SQLite parameterisation".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from discord_scanner.cursor.lock import CursorConcurrencyError, CursorLock
from discord_scanner.cursor.state import CursorStore

# ----------------------------------------------------------------------
# CursorStore — CRUD + determinism
# ----------------------------------------------------------------------


def test_get_missing_returns_none(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        assert s.get("g1", "c1") is None


def test_advance_then_get_round_trips(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        s.advance("g1", "c1", "msg_42")
        assert s.get("g1", "c1") == "msg_42"


def test_advance_replaces_existing(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        s.advance("g1", "c1", "msg_10")
        s.advance("g1", "c1", "msg_20")
        assert s.get("g1", "c1") == "msg_20"


def test_all_rows_sorted_for_determinism(tmp_state_root: Path) -> None:
    """`status` output + idempotence both rely on stable ordering."""
    with CursorStore(tmp_state_root) as s:
        s.advance("guild_b", "chan_2", "x")
        s.advance("guild_a", "chan_1", "y")
        s.advance("guild_b", "chan_1", "z")
        rows = s.all_rows()
    ids = [(g, c) for g, c, _, _ in rows]
    assert ids == sorted(ids)
    assert ids[0] == ("guild_a", "chan_1")


def test_all_rows_empty_on_fresh(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        assert s.all_rows() == []


# ----------------------------------------------------------------------
# File permissions + location
# ----------------------------------------------------------------------


def test_creates_sqlite_file_at_expected_path(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root):
        pass
    assert (tmp_state_root / "cursor.sqlite").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only perm bits")
def test_sqlite_file_is_0o600_on_posix(tmp_state_root: Path) -> None:
    """SEC-P0-22: state files chmod 0o600 on create."""
    with CursorStore(tmp_state_root):
        pass
    mode = os.stat(tmp_state_root / "cursor.sqlite").st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# ----------------------------------------------------------------------
# SQL parameterisation — source-level regex guard
# ----------------------------------------------------------------------


def test_no_f_string_or_percent_sql_in_sqlite_modules() -> None:
    """claude-rules "SQLite parameterisation": forbid f-string / %-formatted SQL
    anywhere in modules that touch sqlite. Parameterisation is enforced by the
    absence of these patterns; every real query body uses `?` (verified by
    reading the source)."""
    repo = Path(__file__).resolve().parents[1] / "src" / "discord_scanner"
    for rel in (
        "cursor/state.py",
        "cursor/lock.py",
        "discovery/invite_cache.py",
    ):
        src = (repo / rel).read_text(encoding="utf-8")
        for pat in (
            'f"SELECT',
            "f'SELECT",
            'f"INSERT',
            "f'INSERT",
            'f"UPDATE',
            "f'UPDATE",
            'f"DELETE',
            "f'DELETE",
            "' % ",  # C-style %-formatting leaking into SQL string literal
            '" % ',
        ):
            assert pat not in src, f"forbidden SQL-format pattern {pat!r} in {rel}"


def test_cursor_real_write_with_param_binding(tmp_state_root: Path) -> None:
    """Positive coverage: advance() + get() round-trip proves parameterised SQL
    actually works end-to-end with special chars (quote, semicolon) that would
    break any string-interpolated path."""
    tricky_id = "msg_with_'quote;--drop"
    with CursorStore(tmp_state_root) as s:
        s.advance("g1", "c1", tricky_id)
        assert s.get("g1", "c1") == tricky_id


# ----------------------------------------------------------------------
# v2 schema migration + bidirectional cursor (workspace plan §"Part A — A.3")
# ----------------------------------------------------------------------


def test_v2_columns_added_on_fresh_db(tmp_state_root: Path) -> None:
    """A fresh init creates all v2 columns (oldest_seen, backfill_runs, etc.)."""
    with CursorStore(tmp_state_root) as s:
        cols = {row[1] for row in s._conn.execute("PRAGMA table_info(cursor)")}
    expected_v2 = {
        "oldest_seen_message_id",
        "newest_seen_message_id",
        "oldest_seen_at",
        "backfill_complete",
        "backfill_started_at",
        "backfill_runs",
    }
    assert expected_v2.issubset(cols)


def test_v2_migration_idempotent_on_double_init(tmp_state_root: Path) -> None:
    """Init twice — migration must not error or duplicate columns."""
    with CursorStore(tmp_state_root):
        pass
    # Re-open the same DB; PRAGMA-introspection guard should silently pass.
    with CursorStore(tmp_state_root) as s:
        cols = {row[1] for row in s._conn.execute("PRAGMA table_info(cursor)")}
    assert "oldest_seen_message_id" in cols


def test_v2_migration_against_legacy_v1_db(tmp_state_root: Path) -> None:
    """Simulate a v1 DB by creating only the v1 columns, then opening with v2 code."""
    import sqlite3

    db_path = tmp_state_root / "cursor.sqlite"
    tmp_state_root.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE cursor ("
        "guild_id TEXT NOT NULL, "
        "channel_id TEXT NOT NULL, "
        "last_message_id TEXT, "
        "updated_at TEXT NOT NULL, "
        "PRIMARY KEY (guild_id, channel_id))"
    )
    legacy.execute(
        "INSERT INTO cursor VALUES (?, ?, ?, ?)",
        ("g1", "c1", "msg_legacy", "2026-01-01T00:00:00Z"),
    )
    legacy.commit()
    legacy.close()

    # Open with v2 code — additive migration runs.
    with CursorStore(tmp_state_root) as s:
        # Legacy data preserved
        assert s.get("g1", "c1") == "msg_legacy"
        # New columns present and at default values
        frontier = s.get_frontier("g1", "c1")
        assert frontier.last_message_id == "msg_legacy"
        assert frontier.oldest_seen_message_id is None
        assert frontier.backfill_complete is False
        assert frontier.backfill_runs == 0


def test_get_frontier_missing_returns_empty_frontier(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        f = s.get_frontier("g1", "c1")
    assert f.last_message_id is None
    assert f.oldest_seen_message_id is None
    assert f.backfill_complete is False


def test_advance_keeps_newest_seen_in_sync(tmp_state_root: Path) -> None:
    """advance() should also populate newest_seen_message_id."""
    with CursorStore(tmp_state_root) as s:
        s.advance("g1", "c1", "msg_500")
        f = s.get_frontier("g1", "c1")
    assert f.last_message_id == "msg_500"
    assert f.newest_seen_message_id == "msg_500"


def test_advance_backward_seeds_oldest(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        s.advance_backward("g1", "c1", "100")
        f = s.get_frontier("g1", "c1")
    assert f.oldest_seen_message_id == "100"


def test_advance_backward_monotonic_decreasing(tmp_state_root: Path) -> None:
    """Passing a NEWER candidate id than current oldest must be ignored."""
    with CursorStore(tmp_state_root) as s:
        s.advance_backward("g1", "c1", "200")
        # Try to "go back" to 300 — should be a no-op (300 > 200, can't be "older").
        s.advance_backward("g1", "c1", "300")
        f = s.get_frontier("g1", "c1")
    assert f.oldest_seen_message_id == "200"


def test_advance_backward_accepts_older_candidate(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        s.advance_backward("g1", "c1", "200")
        s.advance_backward("g1", "c1", "100")
        f = s.get_frontier("g1", "c1")
    assert f.oldest_seen_message_id == "100"


def test_increment_backfill_runs_increments(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        n1 = s.increment_backfill_runs("g1", "c1")
        n2 = s.increment_backfill_runs("g1", "c1")
        n3 = s.increment_backfill_runs("g1", "c1")
    assert (n1, n2, n3) == (1, 2, 3)


def test_mark_backfilled_sets_flag(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        s.advance("g1", "c1", "msg_50")
        s.mark_backfilled("g1", "c1")
        f = s.get_frontier("g1", "c1")
    assert f.backfill_complete is True


def test_all_frontiers_returns_v2_view(tmp_state_root: Path) -> None:
    with CursorStore(tmp_state_root) as s:
        s.advance("g1", "c1", "100")
        s.advance_backward("g1", "c1", "50")
        rows = s.all_frontiers()
    assert len(rows) == 1
    gid, cid, frontier = rows[0]
    assert (gid, cid) == ("g1", "c1")
    assert frontier.last_message_id == "100"
    assert frontier.oldest_seen_message_id == "50"


def test_legacy_get_advance_still_work_after_v2_migration(tmp_state_root: Path) -> None:
    """Back-compat: existing callers that only know `get`/`advance` keep working."""
    with CursorStore(tmp_state_root) as s:
        assert s.get("g1", "c1") is None
        s.advance("g1", "c1", "msg_x")
        assert s.get("g1", "c1") == "msg_x"
        rows = s.all_rows()
    assert rows == [("g1", "c1", "msg_x", rows[0][3])]


# ----------------------------------------------------------------------
# CursorLock — single-writer
# ----------------------------------------------------------------------


def test_lock_acquire_release_round_trip(tmp_state_root: Path) -> None:
    lock = CursorLock(tmp_state_root)
    lock.acquire()
    lock.release()
    # Acquiring again after release MUST succeed
    lock.acquire()
    lock.release()


def test_second_lock_refused(tmp_state_root: Path) -> None:
    l1 = CursorLock(tmp_state_root)
    l1.acquire()
    try:
        l2 = CursorLock(tmp_state_root)
        with pytest.raises(CursorConcurrencyError):
            l2.acquire()
    finally:
        l1.release()


def test_lock_context_manager(tmp_state_root: Path) -> None:
    with CursorLock(tmp_state_root):
        # The lock file must exist while held
        assert (tmp_state_root / "cursor.lock").exists()
    # After release another acquire must succeed
    with CursorLock(tmp_state_root):
        pass
