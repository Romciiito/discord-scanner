"""Tests for retention.prune_output — SEC-P0-23 (symlink reject +
realpath), SEC-P0-24 (never follow symlinks).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from discord_scanner.retention import (
    RetentionError,
    _assert_within,
    _safe_rmtree,
    prune_output,
)


def _mk_guild_date(output_root: Path, guild_id: str, date: str, age_days: float = 0.0) -> Path:
    """Create `output_root/<guild>/<date>/` with a sentinel file, age-adjust mtime."""
    d = output_root / guild_id / date
    d.mkdir(parents=True)
    (d / "messages.jsonl").write_text('{"id":"1"}\n', encoding="utf-8")
    (d / "attachments").mkdir()
    (d / "attachments" / "1_photo.png").write_bytes(b"fake")
    if age_days > 0:
        epoch = time.time() - (age_days * 86400)
        os.utime(d, (epoch, epoch))
        for sub in d.rglob("*"):
            os.utime(sub, (epoch, epoch))
    return d


def test_no_output_root_returns_zero_counters(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    counters = prune_output(missing, keep_days=30)
    assert counters == {
        "scanned": 0,
        "deleted": 0,
        "skipped_symlink": 0,
        "skipped_non_date": 0,
        "refused_escape": 0,
    }


def test_nothing_to_prune_when_all_fresh(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    _mk_guild_date(root, "g1", "2026-04-24", age_days=1.0)
    counters = prune_output(root, keep_days=30)
    assert counters["scanned"] == 1
    assert counters["deleted"] == 0


def test_prune_deletes_stale_date_dir(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    old_dir = _mk_guild_date(root, "g1", "2025-01-01", age_days=60.0)
    counters = prune_output(root, keep_days=30)
    assert counters["deleted"] == 1
    assert not old_dir.exists()


def test_prune_keeps_fresh_and_deletes_stale(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    fresh = _mk_guild_date(root, "g1", "2026-04-20", age_days=2.0)
    stale = _mk_guild_date(root, "g1", "2025-01-01", age_days=120.0)
    counters = prune_output(root, keep_days=30)
    assert fresh.exists()
    assert not stale.exists()
    assert counters["deleted"] == 1
    assert counters["scanned"] == 2


def test_prune_ignores_non_date_dirs(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    junk = root / "g1" / "not-a-date"
    junk.mkdir(parents=True)
    (junk / "file").write_text("x")
    counters = prune_output(root, keep_days=30)
    assert counters["skipped_non_date"] == 1
    assert counters["deleted"] == 0
    assert junk.exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlinks require admin/dev-mode on Windows",
)
def test_prune_refuses_symlinked_date_dir(tmp_path: Path) -> None:
    """SEC-P0-24: symlink at the date-dir level → skipped, never followed."""
    root = tmp_path / "output"
    root.mkdir()
    # Real data outside output root that we want to NOT delete
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "important.txt"
    outside_file.write_text("do not delete")
    # Malicious symlink inside output root pointing outside
    (root / "g1").mkdir()
    sym = root / "g1" / "2020-01-01"
    sym.symlink_to(outside)

    counters = prune_output(root, keep_days=30)
    assert counters["skipped_symlink"] >= 1
    assert counters["deleted"] == 0
    # The outside file MUST still exist
    assert outside_file.exists()
    # The symlink itself is not deleted either
    assert sym.is_symlink()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlinks require admin/dev-mode on Windows",
)
def test_prune_refuses_symlinked_guild_dir(tmp_path: Path) -> None:
    """Symlink at the guild_id level → skipped."""
    root = tmp_path / "output"
    root.mkdir()
    outside = tmp_path / "outside_guild"
    outside.mkdir()
    sym = root / "g-malicious"
    sym.symlink_to(outside)
    counters = prune_output(root, keep_days=30)
    assert counters["skipped_symlink"] >= 1
    assert outside.exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlinks require admin/dev-mode on Windows",
)
def test_safe_rmtree_skips_symlink_file_inside(tmp_path: Path) -> None:
    """If a stale dir contains a symlinked file, we refuse to unlink it."""
    root = tmp_path / "output"
    root.mkdir()
    root_resolved = Path(os.path.realpath(root)).resolve()
    stale = _mk_guild_date(root, "g1", "2025-01-01", age_days=100.0)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "critical.txt"
    outside_file.write_text("critical")
    (stale / "attachments" / "link_to_outside.txt").symlink_to(outside_file)

    ok = _safe_rmtree(stale, root_resolved)
    # Incomplete because the symlink was refused
    assert ok is False
    # Outside file untouched
    assert outside_file.exists()


# ----------------------------------------------------------------------
# _assert_within
# ----------------------------------------------------------------------


def test_assert_within_accepts_inside(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    inside = root / "sub"
    inside.mkdir()
    _assert_within(inside, Path(os.path.realpath(root)).resolve())


def test_assert_within_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(RetentionError):
        _assert_within(outside, Path(os.path.realpath(root)).resolve())


# ----------------------------------------------------------------------
# _safe_rmtree — regular (non-symlink) branches — must run on Windows too
# ----------------------------------------------------------------------


def test_safe_rmtree_deletes_nested_regular_dir(tmp_path: Path) -> None:
    """Happy path: nested dirs + files → all gone, returns True."""
    root = tmp_path / "output"
    root.mkdir()
    root_resolved = Path(os.path.realpath(root)).resolve()
    stale = root / "g1" / "2025-01-01"
    stale.mkdir(parents=True)
    (stale / "messages.jsonl").write_text("x")
    att = stale / "attachments"
    att.mkdir()
    (att / "1_a.png").write_bytes(b"a")
    (att / "2_b.png").write_bytes(b"b")
    # nested subdirectory
    deep = stale / "nested" / "deeper"
    deep.mkdir(parents=True)
    (deep / "file.txt").write_text("deep")

    ok = _safe_rmtree(stale, root_resolved)
    assert ok is True
    assert not stale.exists()


def test_safe_rmtree_refuses_path_outside_root(tmp_path: Path) -> None:
    """Defence-in-depth: a target argument that is OUTSIDE root_resolved
    returns False immediately, zero deletions."""
    root = tmp_path / "output"
    root.mkdir()
    root_resolved = Path(os.path.realpath(root)).resolve()
    outside = tmp_path / "not_under_root"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")

    ok = _safe_rmtree(outside, root_resolved)
    assert ok is False
    assert (outside / "keep.txt").exists()


def test_prune_output_stale_deletes_attachments_too(tmp_path: Path) -> None:
    """End-to-end: stale dir with attachments subdir + files → all gone."""
    root = tmp_path / "output"
    root.mkdir()
    stale = _mk_guild_date(root, "g1", "2025-01-01", age_days=120.0)
    att_file = stale / "attachments" / "1_photo.png"
    assert att_file.exists()
    counters = prune_output(root, keep_days=30)
    assert counters["deleted"] == 1
    assert not stale.exists()
    assert not att_file.exists()


def test_prune_output_multiple_guilds(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    _mk_guild_date(root, "g1", "2025-01-01", age_days=120.0)
    _mk_guild_date(root, "g2", "2025-01-01", age_days=120.0)
    _mk_guild_date(root, "g3", "2026-04-20", age_days=2.0)
    counters = prune_output(root, keep_days=30)
    assert counters["deleted"] == 2  # g1 + g2
    assert counters["scanned"] == 3


def test_prune_output_rejects_guild_entry_refused_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force `_assert_within` to raise for the guild entry → refused_escape
    counter incremented, guild dir untouched. Covers the escape-rejection
    branch of prune_output without needing real symlinks (works on Windows too)."""
    root = tmp_path / "output"
    root.mkdir()
    _mk_guild_date(root, "g1", "2025-01-01", age_days=120.0)
    import discord_scanner.retention as retention_mod

    real_assert = retention_mod._assert_within
    call_count = 0

    def _fake(candidate: Path, root_resolved: Path) -> None:
        nonlocal call_count
        call_count += 1
        # Fail only on first guild-entry check
        if call_count == 1:
            raise RetentionError("synthetic escape")
        real_assert(candidate, root_resolved)

    monkeypatch.setattr(retention_mod, "_assert_within", _fake)
    counters = prune_output(root, keep_days=30)
    assert counters["refused_escape"] >= 1
    assert counters["deleted"] == 0


def test_prune_output_ignores_non_dir_entries(tmp_path: Path) -> None:
    """A file (not dir) inside output_root is silently ignored."""
    root = tmp_path / "output"
    root.mkdir()
    (root / "stray_file.txt").write_text("ignored")
    counters = prune_output(root, keep_days=30)
    assert counters["scanned"] == 0


def test_prune_output_non_dir_inside_guild_dir(tmp_path: Path) -> None:
    """A file inside a guild dir (not a date dir) is counted as skipped_non_date."""
    root = tmp_path / "output"
    root.mkdir()
    (root / "g1").mkdir()
    (root / "g1" / "README").write_text("notes")
    counters = prune_output(root, keep_days=30)
    assert counters["skipped_non_date"] >= 1


def test_prune_output_now_epoch_injection_deterministic(tmp_path: Path) -> None:
    """Inject `now_epoch` → prune acts as if it's exactly that instant."""
    root = tmp_path / "output"
    root.mkdir()
    # Directory mtime "now" per current wall clock
    d = _mk_guild_date(root, "g1", "2026-04-24", age_days=5.0)
    actual_mtime = d.stat().st_mtime
    # Pretend current time is >>> mtime+30d so the dir is stale
    future = actual_mtime + (31 * 86400)
    counters = prune_output(root, keep_days=30, now_epoch=future)
    assert counters["deleted"] == 1
    # Pretend current time is right at mtime+30d-1s → NOT stale
    not_quite = actual_mtime + (30 * 86400) - 1
    d2 = _mk_guild_date(root, "g2", "2026-04-24", age_days=5.0)
    counters2 = prune_output(root, keep_days=30, now_epoch=not_quite)
    assert counters2["deleted"] == 0
    assert d2.exists()
