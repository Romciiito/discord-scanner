"""prior.txt writer — date of the previous scan for this guild.

Traces to:
- seed-spec.md §2.8 (prior.txt for diff-friendly Stage 3 consumption)

Lists the most recent date directory under `output/{guild_id}/` that is
strictly older than `current_date`. Stage 3 reads this to jump straight to
the predecessor when computing diffs.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

from discord_scanner._paths import secure_mkdir
from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)

_DATE_DIR: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_prior_date(guild_dir: Path, current_date: str) -> str | None:
    """Return the most recent `YYYY-MM-DD` dir name in `guild_dir` that is
    strictly earlier than `current_date`, or None.

    Silently skips non-date directories + non-directories.
    """
    if not guild_dir.is_dir():
        return None
    candidates: list[str] = []
    for entry in guild_dir.iterdir():
        # TODO-P8-02 fix: never trust symlinked YYYY-MM-DD dirs. A symlinked
        # entry named `2026-04-20` could point anywhere; downstream readers
        # that do `guild_dir / prior_date` would land at the symlink target.
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        name = entry.name
        if not _DATE_DIR.match(name):
            continue
        if name >= current_date:
            continue
        candidates.append(name)
    if not candidates:
        return None
    candidates.sort()  # lexicographic == chronological for ISO-8601 dates
    return candidates[-1]


def write_prior(path: Path, prior_date: str | None) -> None:
    """Write prior.txt. Empty string if no prior scan exists (REQ-F-029).

    Flushed + fsync'd before return — same durability contract as the JSONL
    writers (seed-spec §2.7).
    """
    secure_mkdir(path.parent)
    content = (prior_date or "") + "\n" if prior_date else ""
    blob = content.encode("utf-8")
    with path.open("wb") as fh:
        fh.write(blob)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError as e:
            logger.debug("prior_fsync_failed", path=str(path), err=str(e))
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.debug("chmod_best_effort_failed", path=str(path), err=str(e))
