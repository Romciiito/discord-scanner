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
  user_agent_chrome_version: "148.0.7778.56"
  fake_os: "Windows NT 10.0; Win64; x64"
  fake_os_platform: "Windows"
  locale: "en-US"
  timezone: "Europe/Prague"
  client_build_number: 300000
  per_host_rate_per_sec:
    "discord.com/api": 1000.0
    "cdn.discordapp.com": 1000.0
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


@pytest.fixture(autouse=True)
def _fast_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap `request_with_retry` so its default backoff is millisecond-scale.

    Production callers that pass explicit `backoff_initial_sec` keep their
    values. Callers that rely on defaults (discovery layer, list-guilds CLI)
    get sub-second waits in tests so a 500-mock test doesn't burn 30 s of CI
    time on exponential backoff.
    """
    import discord_scanner.discovery.channels as ch_mod
    import discord_scanner.discovery.forums as fr_mod
    import discord_scanner.discovery.guilds as gd_mod
    import discord_scanner.discovery.invite_resolve as iv_mod
    import discord_scanner.session.retry as retry_mod

    real = retry_mod.request_with_retry

    async def fast_request_with_retry(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("backoff_initial_sec", 0.001)
        kwargs.setdefault("backoff_max_sec", 0.01)
        kwargs.setdefault("attempts", 3)
        return await real(*args, **kwargs)

    monkeypatch.setattr(retry_mod, "request_with_retry", fast_request_with_retry)
    # The discovery modules import the symbol at module load time, so patch
    # each binding too.
    for mod in (gd_mod, ch_mod, fr_mod, iv_mod):
        monkeypatch.setattr(mod, "request_with_retry", fast_request_with_retry)


# ----------------------------------------------------------------------
# Gateway WebSocket mock (hand-rolled — no pytest-websocket dependency).
# Traces to: workplan.md P0 TEST-P0-01 gateway_ws_mock, P3 tests.
# ----------------------------------------------------------------------


class FakeWebSocket:
    """In-memory substitute for `websockets.client.WebSocketClientProtocol`.

    Provides:
    - `recv()` pops the next scripted server message (awaits if none ready)
    - `send(payload)` captures the payload to `self.sent`
    - `close()` marks the socket closed; further `recv` raises StopAsyncIteration

    Use `FakeWebSocket.script([...])` to pre-load a queue of server messages
    (each either a JSON-serialisable dict or a raw `str`).
    """

    def __init__(self) -> None:
        import asyncio as _asyncio

        self._queue: _asyncio.Queue[str] = _asyncio.Queue()
        self.sent: list[str] = []
        self._closed = False

    def script(self, messages: list[dict | str]) -> None:
        import json as _json

        for m in messages:
            payload = m if isinstance(m, str) else _json.dumps(m)
            self._queue.put_nowait(payload)

    async def recv(self) -> str:
        if self._closed:
            raise StopAsyncIteration("fake ws closed")
        return await self._queue.get()

    async def send(self, data: str) -> None:
        if self._closed:
            raise RuntimeError("send on closed fake ws")
        self.sent.append(data)

    async def close(self) -> None:
        self._closed = True


@pytest.fixture()
def fake_ws() -> FakeWebSocket:
    """A single hand-rolled FakeWebSocket per test — script it with `fake_ws.script(...)`."""
    return FakeWebSocket()
