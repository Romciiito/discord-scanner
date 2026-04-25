"""Cross-platform secure directory creation.

Traces to:
- security-model.md §6 SEC-P0-22 (`0o700` on directories, `0o600` on files)
- claude-rules.md MUST "Cross-platform chmod 0o600 / 0o700"
- code-review M5 (Phase 11): `state_root` / `output_root` / `<guild_id>/<date>/`
  were created with the user's umask default, leaving them world-readable on
  multi-user POSIX boxes. `secure_mkdir` enforces 0o700 best-effort across
  every layer of the new tree.

Best-effort on Windows: `os.chmod` only sets the read-only bit; file-level
ACLs are documented as the operator's responsibility in `docs/claude/development.md`.
"""

from __future__ import annotations

import os
from pathlib import Path

from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)


def secure_mkdir(path: Path, *, mode: int = 0o700) -> None:
    """`mkdir(parents=True, exist_ok=True)` then chmod every newly-relevant
    directory (and the leaf) to `mode` best-effort.

    Failures during chmod are logged at DEBUG and do not raise — Windows,
    network mounts, and exotic filesystems may not honour POSIX modes.
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.debug("secure_mkdir_chmod_failed", path=str(path), err=str(e))
