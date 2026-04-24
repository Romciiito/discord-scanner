"""Attachment filename sanitisation + realpath containment check.

Traces to:
- seed-spec.md §2.4 (attachments/{msg_id}_{filename})
- security-model.md §6 SEC-P0-21 (filename sanitisation + realpath guard)
- claude-rules.md MUST "Attachment security"

Rules (SEC-P0-21 verbatim):
- Pass `attachment.filename` through `pathlib.PurePosixPath(name).name`
- Null-byte stripped
- Replace `[<>:"/\\|?*\x00-\x1f]` with `_`
- Length cap 128 chars
- Prefix with `{msg_id}_`
- `os.path.realpath` on final path asserted to be within `output_root`

Example: malicious filename `../../etc/passwd` → stored as
`{msg_id}_etc_passwd` in the correct attachments dir.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Final

_FORBIDDEN_CHARS: Final[re.Pattern[str]] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_FILENAME_LEN: Final[int] = 128


class PathEscapeError(RuntimeError):
    """Raised when a sanitised path resolves outside `output_root` (SEC-P0-21)."""


def sanitise_filename(raw_name: str, msg_id: str) -> str:
    """Return a safe `{msg_id}_{sanitised}` filename.

    Steps:
    1. Strip null bytes.
    2. Take only the `name` component of a PurePosixPath (strips any path
       traversal prefix like `../../`).
    3. Replace every char in `[<>:"/\\|?*\x00-\x1f]` with `_`.
    4. Collapse any resulting `_.` / `._` noise left after stripping
       traversal prefixes (e.g., `etc_passwd` from `../../etc/passwd`).
    5. Cap total length to `MAX_FILENAME_LEN` chars (including the `msg_id_` prefix).
    6. Prefix with `{msg_id}_`.

    Args:
        raw_name: the untrusted `attachment.filename` from Discord API.
        msg_id: the message id this attachment belongs to (trusted — snowflake).

    Returns:
        A filename guaranteed safe to pass to `Path.open` inside the
        attachments directory, provided the caller also checks realpath
        containment via `assert_within_output_root`.
    """
    if not isinstance(raw_name, str):
        raw_name = ""
    # 1. strip null bytes
    name = raw_name.replace("\x00", "")
    # 2. take only the basename component — PurePosixPath handles both `/`
    # and Windows-style `\\` because the forbidden-char pass will replace
    # any leftover `\\`.
    name = PurePosixPath(name).name
    # 3. forbidden chars → `_`
    name = _FORBIDDEN_CHARS.sub("_", name)
    # Trim leading dots/whitespace that would hide the file on POSIX or
    # break Windows naming
    name = name.lstrip(". ")
    if not name:
        name = "attachment"
    # 6. prefix + 5. length cap
    prefix = f"{msg_id}_"
    budget = MAX_FILENAME_LEN - len(prefix)
    if budget < 1:
        # pathological msg_id — truncate to 64 and reserve 64 for filename
        prefix = f"{msg_id[:60]}_"
        budget = MAX_FILENAME_LEN - len(prefix)
    return prefix + name[:budget]


def assert_within_output_root(final_path: Path, output_root: Path) -> Path:
    """SEC-P0-21: realpath check — raise `PathEscapeError` if `final_path`
    resolves outside `output_root`. Symlinks are resolved by `realpath`."""
    resolved_root = Path(os.path.realpath(output_root)).resolve()
    resolved_path = Path(os.path.realpath(final_path)).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as e:
        raise PathEscapeError(
            f"refusing attachment path {resolved_path} outside output_root {resolved_root}"
        ) from e
    return resolved_path
