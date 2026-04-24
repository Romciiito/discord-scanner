"""Per-burner cookie jar: JSON file at state/cookies-{username}.json.

Traces to:
- seed-spec.md §4.4 (cookie persistence)
- security-model.md §6 SEC-P0-12 (per-burner jar with username in filename),
  SEC-P0-22 (chmod 0o600 best-effort)
- claude-rules.md MUST "Per-burner cookie jar"

Discord issues `__dcfduid`, `__sdcfduid`, `locale` cookies; real sessions
persist them across requests. Cross-burner cookie leakage is prevented by
using the `keyring_username` as a filename component.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)


class _CookieRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    value: str
    domain: str | None = None
    path: str = "/"


class _CookieJarFile(BaseModel):
    """On-disk schema for the JSON jar (pydantic-validated on load)."""

    model_config = ConfigDict(extra="allow")
    schema_version: int = 1
    cookies: list[_CookieRecord] = Field(default_factory=list)


def jar_path(state_root: Path, username: str) -> Path:
    """Return the per-burner cookie-jar path. SEC-P0-12 requires username in name."""
    return state_root / f"cookies-{username}.json"


def load_jar(state_root: Path, username: str) -> httpx.Cookies:
    """Load the per-burner cookie jar. Returns an empty jar if the file is absent.

    Raises:
        pydantic.ValidationError: if the file is present but malformed.
    """
    path = jar_path(state_root, username)
    if not path.exists():
        logger.debug("cookie_jar_not_present", path=str(path))
        return httpx.Cookies()

    raw = path.read_text(encoding="utf-8")
    parsed = _CookieJarFile.model_validate_json(raw)
    jar = httpx.Cookies()
    for rec in parsed.cookies:
        jar.set(rec.name, rec.value, domain=rec.domain or "", path=rec.path)
    logger.debug("cookie_jar_loaded", count=len(parsed.cookies), path=str(path))
    return jar


def save_jar(jar: httpx.Cookies, state_root: Path, username: str) -> None:
    """Persist the cookie jar with file perms `0o600` (SEC-P0-22 best-effort on Windows)."""
    state_root.mkdir(parents=True, exist_ok=True)
    path = jar_path(state_root, username)
    records: list[dict[str, str]] = []
    for cookie in jar.jar:
        records.append(
            {
                "name": cookie.name,
                "value": cookie.value or "",
                "domain": cookie.domain,
                "path": cookie.path,
            }
        )
    payload = _CookieJarFile(cookies=[_CookieRecord(**r) for r in records])
    path.write_text(payload.model_dump_json(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        # Windows may reject the full POSIX mode; best-effort only (documented in
        # docs/claude/development.md Third-party PII section).
        logger.debug("chmod_best_effort_failed", path=str(path), err=str(e))
    logger.debug("cookie_jar_saved", count=len(records), path=str(path))
