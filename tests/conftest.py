"""Shared test fixtures.

Traces to: workplan.md Phase 0 TEST-P0-01, Phase 1 tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def tmp_state_root(tmp_path: Path) -> Path:
    """Temporary state root for cursor + cookies + locks."""
    p = tmp_path / "state"
    p.mkdir()
    return p


@pytest.fixture()
def tmp_output_root(tmp_path: Path) -> Path:
    """Temporary output root for artefacts."""
    p = tmp_path / "output"
    p.mkdir()
    return p


@pytest.fixture()
def tmp_config_yaml(tmp_path: Path, tmp_state_root: Path, tmp_output_root: Path) -> Path:
    """Minimal valid YAML config pointing at tmp roots."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""run:
  log_level: info
  output_root: {tmp_output_root.as_posix()}
  state_root: {tmp_state_root.as_posix()}
auth:
  token_source: keyring
  keyring_service: discord-scanner-test
  keyring_username: test-burner
gateway:
  enabled: true
  presence: online
http:
  http2: true
  timeout_sec: 30
  user_agent_chrome_version: "134.0.0.0"
  fake_os: "Windows NT 10.0; Win64; x64"
  fake_os_platform: "Windows"
  locale: "en-US"
  timezone: "Europe/Prague"
  client_build_number: 300000
retry:
  attempts: 3
discovery:
  invites_input: {(tmp_path / "invites.json").as_posix()}
  filter:
    min_score_pct: 70
    intent_allowlist: [prompt_sharing]
  manual_invites: []
attachments:
  download_images: true
  image_extensions: [.png, .jpg, .webp]
  max_size_mb: 20
daemon:
  interval_hours: 168
  jitter_hours: [-1, 2]
  scan_start_window: "02:00-06:00 UTC"
retention:
  raw_dump_keep_days: 30
  attachment_keep_days: 14
""",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture()
def mock_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Mock the real keyring module in-place so tests do not touch the OS
    credential store. Patches `keyring.get_keyring`, `set_password`, and the
    backend `__class__` metadata that `store-token` inspects for plaintext detection.

    Individual tests can swap `get_keyring.return_value` to simulate a plaintext
    fallback backend.
    """
    import keyring as real_kr

    store: dict[tuple[str, str], str] = {}

    def _set(service: str, user: str, value: str) -> None:
        store[(service, user)] = value

    def _get(service: str, user: str) -> str | None:
        return store.get((service, user))

    def _delete(service: str, user: str) -> None:
        store.pop((service, user), None)

    monkeypatch.setattr(real_kr, "set_password", _set)
    monkeypatch.setattr(real_kr, "get_password", _get)
    monkeypatch.setattr(real_kr, "delete_password", _delete)

    fake_backend = MagicMock()
    # Populate real class metadata that the plaintext-refusal check reads.
    type(fake_backend).__name__ = "WindowsCredentialStore"
    type(fake_backend).__module__ = "keyring.backends.Windows"
    get_keyring_mock = MagicMock(return_value=fake_backend)
    monkeypatch.setattr(real_kr, "get_keyring", get_keyring_mock)

    handle = MagicMock()
    handle.get_keyring = get_keyring_mock
    handle.set_password.side_effect = _set
    handle.get_password.side_effect = _get
    yield handle


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub DISCORD_TOKEN from env so tests never accidentally read a real one."""
    for key in ("DISCORD_TOKEN", "APP_DISCORD_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    # Guard: never leak real home-dir .env into tests
    monkeypatch.setenv("DISCORD_SCANNER_TEST_MODE", "1")
    assert os.environ.get("DISCORD_SCANNER_TEST_MODE") == "1"
