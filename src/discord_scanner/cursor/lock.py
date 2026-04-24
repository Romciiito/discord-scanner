"""Cross-platform cursor filelock — prevents concurrent scans on one state root.

Traces to:
- seed-spec.md §2.7 (resumable / single-writer)
- security-model.md §6 SEC-P0-13-adjacent (one scan per burner at a time)
- claude-rules.md MUST "Cross-platform locking" (filelock, not fcntl)
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

from filelock import FileLock, Timeout

from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)


class CursorConcurrencyError(RuntimeError):
    """Another scan already holds the cursor lock for this state root."""


class CursorLock:
    """Context manager over `state/cursor.lock`. Second acquire → exit 1."""

    def __init__(self, state_root: Path) -> None:
        state_root.mkdir(parents=True, exist_ok=True)
        self._lock_path = state_root / "cursor.lock"
        self._lock = FileLock(str(self._lock_path), timeout=0)

    def acquire(self) -> None:
        try:
            self._lock.acquire()
        except Timeout as e:
            raise CursorConcurrencyError(f"another scan already holds {self._lock_path}") from e
        # chmod 0o600 best-effort (Windows: read-only bit only)
        try:
            os.chmod(self._lock_path, 0o600)
        except OSError as e:
            logger.debug("chmod_best_effort_failed", path=str(self._lock_path), err=str(e))

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> CursorLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
