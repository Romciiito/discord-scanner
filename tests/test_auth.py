"""Tests for `session/auth.py` — token priority + plaintext refusal.

Traces to: SEC-P0-01, SEC-P0-05, SEC-P0-06.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import keyring as real_kr
import pytest
from pydantic import SecretStr

from discord_scanner.config import load_config
from discord_scanner.session.auth import (
    PlaintextKeyringRefused,
    TokenNotFound,
    TokenSource,
    load_token,
)


def test_load_token_priority_keyring(tmp_config_yaml: Path, mock_keyring: MagicMock) -> None:
    """Tier 1: keyring hit wins."""
    real_kr.set_password("discord-scanner-test", "test-burner", "KEYRING_TOKEN")
    cfg = load_config(tmp_config_yaml)
    tok, src = load_token(cfg)
    assert tok.get_secret_value() == "KEYRING_TOKEN"
    assert src is TokenSource.KEYRING


def test_load_token_falls_back_to_env(
    tmp_config_yaml: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: env var wins when keyring is empty."""
    monkeypatch.setenv("DISCORD_TOKEN", "ENV_TOKEN")
    cfg = load_config(tmp_config_yaml)
    tok, src = load_token(cfg)
    assert tok.get_secret_value() == "ENV_TOKEN"
    assert src is TokenSource.ENV


def test_load_token_falls_back_to_config(tmp_config_yaml: Path, mock_keyring: MagicMock) -> None:
    """Tier 3: config.yaml is last-resort, emits WARNING."""
    import yaml

    data = yaml.safe_load(tmp_config_yaml.read_text())
    data["auth"]["discord_token"] = "CONFIG_TOKEN"  # noqa: S105 — test fixture value, not a real secret
    tmp_config_yaml.write_text(yaml.safe_dump(data))
    cfg = load_config(tmp_config_yaml)
    tok, src = load_token(cfg)
    assert tok.get_secret_value() == "CONFIG_TOKEN"
    assert src is TokenSource.CONFIG


def test_load_token_raises_when_all_empty(tmp_config_yaml: Path, mock_keyring: MagicMock) -> None:
    cfg = load_config(tmp_config_yaml)
    with pytest.raises(TokenNotFound):
        load_token(cfg)


def test_plaintext_keyring_refused(tmp_config_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-P0-06: plaintext backend → refuse even before reading."""
    fake_plaintext = MagicMock()
    type(fake_plaintext).__name__ = "PlaintextKeyring"
    type(fake_plaintext).__module__ = "keyrings.alt.file"
    monkeypatch.setattr(real_kr, "get_keyring", lambda: fake_plaintext)
    cfg = load_config(tmp_config_yaml)
    with pytest.raises(PlaintextKeyringRefused):
        load_token(cfg)


def test_token_is_secretstr(tmp_config_yaml: Path, mock_keyring: MagicMock) -> None:
    """SEC-P0-05: returned token MUST be a SecretStr so it never leaks to repr."""
    real_kr.set_password("discord-scanner-test", "test-burner", "X-TOKEN-X")
    cfg = load_config(tmp_config_yaml)
    tok, _ = load_token(cfg)
    assert isinstance(tok, SecretStr)
    assert "X-TOKEN-X" not in repr(tok)
