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
    """Write prior.txt. Empty string if no prior scan exists (REQ-F-029)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (prior_date or "") + "\n" if prior_date else ""
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.debug("chmod_best_effort_failed", path=str(path), err=str(e))
