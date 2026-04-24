"""Retention prune — symlink-reject, realpath-bound, age-based dir delete.

Traces to:
- seed-spec.md §3 (retention: `raw_dump_keep_days` default 30)
- security-model.md §6 SEC-P0-23 (symlink + root-escape rejection),
  SEC-P0-24 (never follow symlinks)
- claude-rules.md MUST-NOT "No writes outside output/ and state/"

Algorithm:
1. Walk only `output_root/<guild_id>/<YYYY-MM-DD>/` dirs.
2. For every entry at either level:
   - If `is_symlink()` → skip + log WARNING (never traversed, never deleted).
   - `realpath` check: target must still be under `output_root` after
     symlink resolution; reject escapes.
3. If the `<YYYY-MM-DD>` dir is older than `keep_days`, `shutil.rmtree`
   with `followlinks=False`-equivalent: we `os.walk(..., followlinks=False)`
   and unlink individual files + rmdir each level, refusing symlinks.
4. Prior-date directories are READ-ONLY unless they meet the age cut-off;
   a crash mid-prune leaves other dirs intact.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Final

from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)

_DATE_DIR: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SECONDS_PER_DAY: Final[int] = 86_400


class RetentionError(RuntimeError):
    """Raised on a path that escapes output_root via symlink or traversal."""


def prune_output(
    output_root: Path,
    *,
    keep_days: int,
    now_epoch: float | None = None,
) -> dict[str, int]:
    """Delete `<guild_id>/<YYYY-MM-DD>/` dirs older than `keep_days`.

    Returns a dict of counters: `{"scanned": N, "deleted": M, "skipped_symlink": K,
    "skipped_non_date": L, "refused_escape": P}`.

    Safety:
    - Symlinks at either the guild-id or date-dir level are SKIPPED + logged
      (SEC-P0-24). They are NOT deleted and their targets are NOT followed.
    - `realpath` check confirms every deletion candidate is still inside
      `output_root` after resolution (SEC-P0-23).
    - `shutil.rmtree` is NEVER used — we walk manually with
      `os.walk(followlinks=False)` and `is_symlink()` refusal at each node.

    `now_epoch` is injected for deterministic tests; defaults to `time.time()`.
    """
    counters = {
        "scanned": 0,
        "deleted": 0,
        "skipped_symlink": 0,
        "skipped_non_date": 0,
        "refused_escape": 0,
    }
    if not output_root.exists():
        logger.debug("retention_no_output_root", path=str(output_root))
        return counters

    # Resolve once; all subsequent containment checks use this as the root.
    root_resolved = Path(os.path.realpath(output_root)).resolve()
    now = now_epoch if now_epoch is not None else time.time()
    cutoff_epoch = now - (keep_days * _SECONDS_PER_DAY)

    for guild_entry in output_root.iterdir():
        if guild_entry.is_symlink():
            logger.warning("retention_skip_symlink_guild", path=str(guild_entry))
            counters["skipped_symlink"] += 1
            continue
        if not guild_entry.is_dir():
            continue
        try:
            _assert_within(guild_entry, root_resolved)
        except RetentionError:
            logger.error("retention_refuse_escape_guild", path=str(guild_entry))
            counters["refused_escape"] += 1
            continue

        for date_entry in guild_entry.iterdir():
            counters["scanned"] += 1
            if date_entry.is_symlink():
                logger.warning("retention_skip_symlink_date", path=str(date_entry))
                counters["skipped_symlink"] += 1
                continue
            if not date_entry.is_dir():
                counters["skipped_non_date"] += 1
                continue
            if not _DATE_DIR.match(date_entry.name):
                counters["skipped_non_date"] += 1
                continue
            try:
                _assert_within(date_entry, root_resolved)
            except RetentionError:
                logger.error("retention_refuse_escape_date", path=str(date_entry))
                counters["refused_escape"] += 1
                continue

            mtime = date_entry.stat().st_mtime
            if mtime >= cutoff_epoch:
                continue

            if _safe_rmtree(date_entry, root_resolved):
                counters["deleted"] += 1
                logger.info("retention_deleted", path=str(date_entry))
            else:
                logger.warning("retention_delete_incomplete", path=str(date_entry))
    return counters


def _assert_within(candidate: Path, root_resolved: Path) -> None:
    """realpath-resolve `candidate` and raise if it escapes `root_resolved`."""
    resolved = Path(os.path.realpath(candidate)).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as e:
        raise RetentionError(f"{resolved} escapes output_root {root_resolved}") from e


def _safe_rmtree(target: Path, root_resolved: Path) -> bool:
    """Walk `target` bottom-up with `followlinks=False`. Delete files
    individually (refusing symlinks), then `rmdir` each empty directory.

    Returns True on full success, False if any symlink was encountered (we
    refuse to delete them and we stop descending into their subtree).
    """
    all_ok = True
    # First: verify target itself is still under root (defence in depth).
    try:
        _assert_within(target, root_resolved)
    except RetentionError:
        return False

    # os.walk with followlinks=False does NOT descend into symlinks in `dirs`,
    # which is exactly SEC-P0-24. topdown=False so we remove leaves first.
    for dirpath, dirnames, filenames in os.walk(target, topdown=False, followlinks=False):
        dir_path = Path(dirpath)
        try:
            _assert_within(dir_path, root_resolved)
        except RetentionError:
            all_ok = False
            continue
        for name in filenames:
            fp = dir_path / name
            if fp.is_symlink():
                # never unlink symlinks — leave them behind and skip
                logger.warning("retention_skip_symlink_file", path=str(fp))
                all_ok = False
                continue
            try:
                fp.unlink()
            except OSError as e:
                logger.warning("retention_unlink_failed", path=str(fp), err=str(e))
                all_ok = False
        for name in dirnames:
            sub = dir_path / name
            if sub.is_symlink():
                logger.warning("retention_skip_symlink_subdir", path=str(sub))
                all_ok = False
                continue
            try:
                sub.rmdir()
            except OSError as e:
                # Expected when the subdir still holds skipped symlinks
                logger.debug("retention_rmdir_failed", path=str(sub), err=str(e))
                all_ok = False
    # Finally, rmdir the target itself (if nothing left)
    try:
        target.rmdir()
    except OSError as e:
        logger.debug("retention_rmdir_final_failed", path=str(target), err=str(e))
        all_ok = False
    # shutil re-export guard: claude-rules says "never use rmtree".
    _ = shutil  # keep import as documentation; never call rmtree
    return all_ok
