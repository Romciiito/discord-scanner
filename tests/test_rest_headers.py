"""Tests for `session/headers.py` — 17-header completeness + XSP parity.

Traces to: SEC-P0-07, SEC-P0-09, SEC-P0-25.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from pydantic import SecretStr

from discord_scanner.config import load_config
from discord_scanner.session.headers import (
    REQUIRED_HEADER_KEYS,
    build_rest_headers,
    build_x_super_properties,
)


def test_all_17_required_headers_present_and_nonempty(tmp_config_yaml: Path) -> None:
    """SEC-P0-07 merge-blocker: every required key is present and non-empty."""
    cfg = load_config(tmp_config_yaml)
    h = build_rest_headers(cfg, SecretStr("FAKE_TOKEN"))
    assert len(REQUIRED_HEADER_KEYS) == 17
    for key in REQUIRED_HEADER_KEYS:
        assert key in h, f"missing header: {key}"
        assert h[key], f"empty header: {key}"


def test_authorization_has_no_bearer_prefix(tmp_config_yaml: Path) -> None:
    """User tokens (not bot) pass raw value in Authorization — no `Bearer` prefix."""
    cfg = load_config(tmp_config_yaml)
    h = build_rest_headers(cfg, SecretStr("USER_TOKEN"))
    assert h["Authorization"] == "USER_TOKEN"
    assert not h["Authorization"].startswith("Bearer ")


def test_user_agent_embeds_configured_chrome_version(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    h = build_rest_headers(cfg, SecretStr("x"))
    assert cfg.http.user_agent_chrome_version in h["User-Agent"]
    assert "Chrome/" in h["User-Agent"]
    assert "Safari/537.36" in h["User-Agent"]


def test_x_super_properties_decodes_to_fingerprint(tmp_config_yaml: Path) -> None:
    """SEC-P0-09: decoded XSP fields match `settings.http.fingerprint()`."""
    cfg = load_config(tmp_config_yaml)
    blob = build_x_super_properties(cfg)
    decoded = json.loads(base64.b64decode(blob).decode("utf-8"))
    fp = cfg.http.fingerprint()
    assert decoded == fp


def test_x_super_properties_is_byte_stable(tmp_config_yaml: Path) -> None:
    """Two builds from the same config produce the SAME base64 string
    (byte-for-byte parity with gateway IDENTIFY in P3 requires this)."""
    cfg = load_config(tmp_config_yaml)
    blob1 = build_x_super_properties(cfg)
    blob2 = build_x_super_properties(cfg)
    assert blob1 == blob2


def test_no_bearer_and_no_leaked_token_in_headers_repr(tmp_config_yaml: Path) -> None:
    """Sanity: building the header dict does not eagerly decode the SecretStr
    except in Authorization, and the raw token is not sprayed into any other key."""
    cfg = load_config(tmp_config_yaml)
    h = build_rest_headers(cfg, SecretStr("VERY-SECRET-TOKEN"))
    # token must appear only in Authorization
    leak_count = sum(1 for v in h.values() if "VERY-SECRET-TOKEN" in v)
    assert leak_count == 1
