"""Tests for `session/cookies.py` — per-burner jar + schema + perms.

Traces to: SEC-P0-12, SEC-P0-22.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from discord_scanner.session.cookies import jar_path, load_jar, save_jar


def test_jar_filename_includes_username(tmp_state_root: Path) -> None:
    """SEC-P0-12 merge-blocker: different burners → different files."""
    p1 = jar_path(tmp_state_root, "burner-1")
    p2 = jar_path(tmp_state_root, "burner-2")
    assert p1 != p2
    assert "burner-1" in p1.name
    assert "burner-2" in p2.name


def test_load_empty_jar_when_absent(tmp_state_root: Path) -> None:
    jar = load_jar(tmp_state_root, "absent-burner")
    assert isinstance(jar, httpx.Cookies)
    # newly-created httpx.Cookies has no cookies
    assert list(jar.jar) == []


def test_save_and_load_round_trip(tmp_state_root: Path) -> None:
    jar = httpx.Cookies()
    jar.set("__dcfduid", "abc123", domain="discord.com", path="/")
    jar.set("locale", "en-US", domain="discord.com", path="/")
    save_jar(jar, tmp_state_root, "burner-1")
    reloaded = load_jar(tmp_state_root, "burner-1")
    assert reloaded.get("__dcfduid", domain="discord.com") == "abc123"
    assert reloaded.get("locale", domain="discord.com") == "en-US"


def test_malformed_jar_raises_validation(tmp_state_root: Path) -> None:
    path = jar_path(tmp_state_root, "corrupt")
    path.write_text(json.dumps({"schema_version": 1, "cookies": "not-a-list"}))
    with pytest.raises(ValidationError):
        load_jar(tmp_state_root, "corrupt")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only perm bits")
def test_saved_file_is_0o600_on_posix(tmp_state_root: Path) -> None:
    """SEC-P0-22: saved cookie jar must be `0o600` on POSIX systems."""
    jar = httpx.Cookies()
    save_jar(jar, tmp_state_root, "burner-1")
    mode = os.stat(jar_path(tmp_state_root, "burner-1")).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
