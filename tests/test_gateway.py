"""Tests for `session/gateway.py` — DormantGateway FSM.

Traces to: SEC-P0-10 (IDENTIFY fingerprint == REST XSP), SEC-P0-13 (concurrent-
instance filelock), SEC-P0-25 (fingerprint single source), SEC-P0-26 (plausible
build number).

**Merge-blocker floor: ≥85% coverage on `src/discord_scanner/session/gateway.py`.**
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from discord_scanner.config import load_config
from discord_scanner.session import gateway as gw_mod
from discord_scanner.session.gateway import (
    MAX_HEARTBEAT_MS,
    MIN_HEARTBEAT_MS,
    OP_HEARTBEAT,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_PRESENCE_UPDATE,
    OP_RESUME,
    DormantGateway,
    GatewayConcurrencyError,
    GatewayError,
)
from discord_scanner.session.headers import XSP_JSON_KWARGS, build_x_super_properties

# Re-import the fixture type for type hints; conftest provides it.
from tests.conftest import FakeWebSocket


@pytest.fixture()
def gw(tmp_config_yaml: Path, tmp_state_root: Path) -> DormantGateway:
    cfg = load_config(tmp_config_yaml)
    return DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)


# ----------------------------------------------------------------------
# SEC-P0-10 / SEC-P0-25 — byte-for-byte IDENTIFY == X-Super-Properties parity
# ----------------------------------------------------------------------


def test_identify_properties_equal_rest_xsp_dict(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    """IDENTIFY `properties` dict MUST equal the dict encoded in REST XSP."""
    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    identify = g._build_identify_payload()
    assert identify["op"] == OP_IDENTIFY

    xsp_b64 = build_x_super_properties(cfg)
    xsp_dict = json.loads(base64.b64decode(xsp_b64).decode("utf-8"))

    assert identify["d"]["properties"] == xsp_dict


def test_identify_properties_byte_stable_via_shared_kwargs(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    """Serializing the IDENTIFY `properties` with `XSP_JSON_KWARGS` must produce
    the SAME bytes that REST XSP produced. This is the real byte-for-byte parity
    contract — if either side drifts on kwargs, this test fails."""
    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    identify = g._build_identify_payload()
    identify_bytes = json.dumps(identify["d"]["properties"], **XSP_JSON_KWARGS).encode("utf-8")

    xsp_b64 = build_x_super_properties(cfg)
    xsp_bytes = base64.b64decode(xsp_b64)

    assert identify_bytes == xsp_bytes


def test_identify_client_build_number_plausible(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    """SEC-P0-26: build number in IDENTIFY properties is >= 300000."""
    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    identify = g._build_identify_payload()
    build = identify["d"]["properties"]["client_build_number"]
    assert isinstance(build, int)
    assert build >= 300_000


def test_heartbeat_clamps_to_min_and_max() -> None:
    assert DormantGateway._clamp_heartbeat(500) == MIN_HEARTBEAT_MS
    assert DormantGateway._clamp_heartbeat(999_999) == MAX_HEARTBEAT_MS
    assert DormantGateway._clamp_heartbeat(41_250) == 41_250


# ----------------------------------------------------------------------
# SEC-P0-13 — filelock prevents two gateways on same burner
# ----------------------------------------------------------------------


def test_concurrent_gateway_for_same_burner_refuses(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    cfg = load_config(tmp_config_yaml)
    g1 = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    g1._acquire_lock()
    try:
        g2 = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
        with pytest.raises(GatewayConcurrencyError):
            g2._acquire_lock()
    finally:
        if g1._filelock is not None:
            g1._filelock.release()


# ----------------------------------------------------------------------
# Gateway disabled short-circuits
# ----------------------------------------------------------------------


def _mutate_gateway_disabled(tmp_config_yaml: Path) -> None:
    import yaml

    data = yaml.safe_load(tmp_config_yaml.read_text())
    data["gateway"]["enabled"] = False
    tmp_config_yaml.write_text(yaml.safe_dump(data))


@pytest.mark.asyncio
async def test_gateway_disabled_noops(tmp_config_yaml: Path, tmp_state_root: Path) -> None:
    _mutate_gateway_disabled(tmp_config_yaml)
    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    await g.connect()
    # No websocket opened, no lock acquired
    assert g._ws is None
    assert g._filelock is None
    await g.close()  # must not raise even when connect was a no-op


# ----------------------------------------------------------------------
# Handshake happy path via FakeWebSocket
# ----------------------------------------------------------------------


async def _run_handshake_with_fake(
    g: DormantGateway, fake: FakeWebSocket, *, resume: bool = False
) -> None:
    """Swap `g._ws` with the fake and run `_do_handshake`."""
    g._ws = fake  # type: ignore[assignment]
    await g._do_handshake(resume=resume)
    # cancel heartbeat to avoid leaking tasks
    g._shutdown.set()
    if g._heartbeat_task is not None:
        g._heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await g._heartbeat_task


@pytest.mark.asyncio
async def test_handshake_happy_path_sends_identify_and_presence(
    gw: DormantGateway, fake_ws: FakeWebSocket
) -> None:
    """HELLO → IDENTIFY → READY → PRESENCE UPDATE (once) → heartbeat running."""
    fake_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {
                "op": 0,
                "t": "READY",
                "s": 1,
                "d": {"session_id": "sess-abc", "v": 10},
            },
        ]
    )
    await _run_handshake_with_fake(gw, fake_ws, resume=False)

    assert gw.session_id == "sess-abc"
    assert gw.sequence == 1
    assert gw.heartbeat_interval_ms == 41250
    assert gw.presence_sent is True

    ops_sent = [json.loads(p)["op"] for p in fake_ws.sent]
    assert OP_IDENTIFY in ops_sent
    assert OP_PRESENCE_UPDATE in ops_sent
    assert ops_sent.count(OP_PRESENCE_UPDATE) == 1  # presence exactly once
    assert gw.counters["identify"] == 1


@pytest.mark.asyncio
async def test_handshake_discards_non_ready_dispatch(
    gw: DormantGateway, fake_ws: FakeWebSocket
) -> None:
    """DISPATCH events other than READY/RESUMED during handshake are discarded
    but the sequence counter still advances."""
    fake_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {"op": 0, "t": "TYPING_START", "s": 1, "d": {}},
            {"op": 0, "t": "PRESENCE_UPDATE", "s": 2, "d": {}},
            {"op": 0, "t": "READY", "s": 3, "d": {"session_id": "sess-xyz"}},
        ]
    )
    await _run_handshake_with_fake(gw, fake_ws, resume=False)

    assert gw.counters["events_discarded"] == 2
    assert gw.sequence == 3
    assert gw.session_id == "sess-xyz"


@pytest.mark.asyncio
async def test_resume_on_invalid_session_falls_back_to_fresh_identify(
    gw: DormantGateway, fake_ws: FakeWebSocket
) -> None:
    """OPCODE 9 INVALID_SESSION during resume → fresh IDENTIFY → READY."""
    gw._state.session_id = "old-sess"
    gw._state.sequence = 42
    fake_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {"op": OP_INVALID_SESSION, "d": False},
            {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "new-sess"}},
        ]
    )
    await _run_handshake_with_fake(gw, fake_ws, resume=True)

    ops_sent = [json.loads(p)["op"] for p in fake_ws.sent]
    # First send should be RESUME, then IDENTIFY after invalid session, then PRESENCE
    assert OP_RESUME in ops_sent
    assert OP_IDENTIFY in ops_sent
    assert gw.counters["resume_fail"] == 1
    assert gw.counters["identify"] == 1
    assert gw.session_id == "new-sess"


@pytest.mark.asyncio
async def test_fresh_identify_raises_on_invalid_session(
    gw: DormantGateway, fake_ws: FakeWebSocket
) -> None:
    """On a NON-resume handshake, INVALID_SESSION is a hard error (bad token/fingerprint)."""
    fake_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {"op": OP_INVALID_SESSION, "d": False},
        ]
    )
    gw._ws = fake_ws  # type: ignore[assignment]
    with pytest.raises(GatewayError):
        await gw._do_handshake(resume=False)
    gw._shutdown.set()
    if gw._heartbeat_task is not None:
        gw._heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await gw._heartbeat_task


# ----------------------------------------------------------------------
# Heartbeat
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_on_interval(
    gw: DormantGateway, fake_ws: FakeWebSocket, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat coroutine emits OPCODE 1 on the clamped interval."""
    # tiny interval for fast test
    gw._state.heartbeat_interval_ms = 1000
    gw._ws = fake_ws  # type: ignore[assignment]

    # Patch random.uniform to 0.01 so the loop fires quickly
    monkeypatch.setattr(gw_mod.random, "uniform", lambda a, b: 0.01)

    gw._shutdown.clear()
    task = asyncio.create_task(gw._heartbeat_loop())
    await asyncio.sleep(0.08)  # should allow ~5+ beats at 10ms interval
    gw._shutdown.set()
    await task

    assert gw.counters["heartbeat_sent"] >= 1
    for payload in fake_ws.sent:
        op = json.loads(payload).get("op")
        assert op == OP_HEARTBEAT


@pytest.mark.asyncio
async def test_heartbeat_shutdown_returns_cleanly(
    gw: DormantGateway, fake_ws: FakeWebSocket
) -> None:
    gw._state.heartbeat_interval_ms = 5000
    gw._ws = fake_ws  # type: ignore[assignment]
    gw._shutdown.clear()
    task = asyncio.create_task(gw._heartbeat_loop())
    await asyncio.sleep(0.01)
    gw._shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)  # returns cleanly (no cancel)


# ----------------------------------------------------------------------
# Counters + properties
# ----------------------------------------------------------------------


def test_initial_counters_all_zero(gw: DormantGateway) -> None:
    c = gw.counters
    assert c["identify"] == 0
    assert c["resume_ok"] == 0
    assert c["resume_fail"] == 0
    assert c["heartbeat_sent"] == 0
    assert c["events_discarded"] == 0


def test_counters_returns_copy(gw: DormantGateway) -> None:
    c1 = gw.counters
    c1["identify"] = 999  # mutate the snapshot
    assert gw.counters["identify"] == 0  # internal state untouched


# ----------------------------------------------------------------------
# connect() wiring with websockets.client.connect patched
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_uses_ws_url_and_holds_lock(
    tmp_config_yaml: Path,
    tmp_state_root: Path,
    fake_ws: FakeWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`connect()` calls `websockets.client.connect(GATEWAY_URL)` + takes lock."""
    fake_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "x"}},
        ]
    )

    captured: dict[str, Any] = {}

    async def fake_connect(url: str, **kwargs: Any) -> FakeWebSocket:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return fake_ws

    monkeypatch.setattr(gw_mod.websockets, "connect", fake_connect)

    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    await g.connect()
    try:
        assert captured["url"] == gw_mod.GATEWAY_URL
        assert g._filelock is not None
        assert g.session_id == "x"
    finally:
        await g.close()


@pytest.mark.asyncio
async def test_connect_failure_releases_lock(
    tmp_config_yaml: Path,
    tmp_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the handshake raises, `connect()` MUST still release the filelock."""
    bad_ws = FakeWebSocket()
    bad_ws.script([{"op": 99, "d": {}}])  # bogus first frame → GatewayError

    async def fake_connect(url: str, **kwargs: Any) -> FakeWebSocket:
        return bad_ws

    monkeypatch.setattr(gw_mod.websockets, "connect", fake_connect)

    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    with pytest.raises(GatewayError):
        await g.connect()

    # A second connect from a different instance must NOT see the lock held.
    g2 = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    g2._acquire_lock()
    g2._filelock.release() if g2._filelock else None


# ----------------------------------------------------------------------
# Context manager
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_context_manager(
    tmp_config_yaml: Path,
    tmp_state_root: Path,
    fake_ws: FakeWebSocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "ctx"}},
        ]
    )

    async def fake_connect(url: str, **kwargs: Any) -> FakeWebSocket:
        return fake_ws

    monkeypatch.setattr(gw_mod.websockets, "connect", fake_connect)

    cfg = load_config(tmp_config_yaml)
    async with DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root) as g:
        assert g.session_id == "ctx"


@pytest.mark.asyncio
async def test_reconnect_with_resume_resumed(
    tmp_config_yaml: Path,
    tmp_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reconnect_with_resume` returns 'resumed' when RESUME succeeds."""
    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    g._state.session_id = "sess-resume"
    g._state.sequence = 99

    second_ws = FakeWebSocket()
    second_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {"op": 0, "t": "RESUMED", "s": 100, "d": {}},
        ]
    )
    connect_calls: list[str] = []

    async def fake_connect(url: str, **kwargs: Any) -> FakeWebSocket:
        connect_calls.append(url)
        return second_ws

    monkeypatch.setattr(gw_mod.websockets, "connect", fake_connect)

    result = await g.reconnect_with_resume()
    try:
        assert result == "resumed"
        assert g.counters["resume_ok"] == 1
        assert connect_calls == [gw_mod.GATEWAY_URL]
    finally:
        g._shutdown.set()
        if g._heartbeat_task is not None:
            g._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await g._heartbeat_task


@pytest.mark.asyncio
async def test_reconnect_with_resume_reidentified(
    tmp_config_yaml: Path,
    tmp_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reconnect_with_resume` returns 'reidentified' when RESUME fails."""
    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    g._state.session_id = "stale-sess"
    g._state.sequence = 42

    new_ws = FakeWebSocket()
    new_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {"op": OP_INVALID_SESSION, "d": False},
            {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "fresh"}},
        ]
    )

    async def fake_connect(url: str, **kwargs: Any) -> FakeWebSocket:
        return new_ws

    monkeypatch.setattr(gw_mod.websockets, "connect", fake_connect)

    result = await g.reconnect_with_resume()
    try:
        assert result == "reidentified"
        assert g.counters["resume_fail"] == 1
        assert g.session_id == "fresh"
    finally:
        g._shutdown.set()
        if g._heartbeat_task is not None:
            g._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await g._heartbeat_task


def test_handshake_without_ws_raises(gw: DormantGateway) -> None:
    """Defensive check: handshake with no WS open raises GatewayError."""
    assert gw._ws is None

    async def _coro() -> None:
        await gw._do_handshake(resume=False)

    with pytest.raises(GatewayError):
        asyncio.run(_coro())


def test_await_ready_without_ws_raises(gw: DormantGateway) -> None:
    assert gw._ws is None

    async def _coro() -> None:
        await gw._await_ready(resume=False)

    with pytest.raises(GatewayError):
        asyncio.run(_coro())


@pytest.mark.asyncio
async def test_heartbeat_loop_without_interval_returns(gw: DormantGateway) -> None:
    """Defensive: if `heartbeat_interval_ms` is unset, the loop returns without error."""
    gw._state.heartbeat_interval_ms = None
    gw._shutdown.clear()
    await gw._heartbeat_loop()  # should return immediately without raising


@pytest.mark.asyncio
async def test_reconnect_cancels_old_heartbeat_task(
    tmp_config_yaml: Path,
    tmp_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for reviewer MAJOR: `reconnect_with_resume` MUST cancel the
    prior heartbeat task before spawning a new one. Otherwise two loops emit
    OPCODE 1 in parallel and the effective rate doubles — a detectability
    signal the scanner must avoid."""
    cfg = load_config(tmp_config_yaml)
    g = DormantGateway(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    g._state.session_id = "pre-reconnect"
    g._state.sequence = 1

    # Plant a live heartbeat task that would continue running if not cancelled.
    async def _never_done() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    g._heartbeat_task = asyncio.create_task(_never_done())
    old_task = g._heartbeat_task

    new_ws = FakeWebSocket()
    new_ws.script(
        [
            {"op": OP_HELLO, "d": {"heartbeat_interval": 41250}},
            {"op": 0, "t": "RESUMED", "s": 2, "d": {}},
        ]
    )

    async def fake_connect(url: str, **kwargs: Any) -> FakeWebSocket:
        return new_ws

    monkeypatch.setattr(gw_mod.websockets, "connect", fake_connect)

    await g.reconnect_with_resume()
    try:
        # Old task must be done (cancelled); new task must be different + alive.
        assert old_task.done(), "old heartbeat task was not cancelled during reconnect"
        assert g._heartbeat_task is not None
        assert g._heartbeat_task is not old_task
    finally:
        g._shutdown.set()
        if g._heartbeat_task is not None:
            g._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await g._heartbeat_task
