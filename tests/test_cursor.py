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
