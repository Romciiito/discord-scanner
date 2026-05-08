"""Dormant-but-present Discord gateway session.

Traces to:
- seed-spec.md §2.5, §4.3 (gateway design)
- security-model.md §6 SEC-P0-10 (byte-for-byte fingerprint parity with REST),
  SEC-P0-13 (concurrent-instance filelock), SEC-P0-25 (single fingerprint source),
  SEC-P0-26 (plausible client_build_number)
- claude-rules.md MUST "Gateway dormant presence", "Cross-platform locking"

Design rules:
- ONE gateway session per scan — filelock `state/gateway-{keyring_username}.lock`
  acquired with `timeout=0`; a second scan with the same burner exits 1.
- IDENTIFY `properties` payload is the EXACT dict returned by
  `settings.http.fingerprint()` — no separate fingerprint blob, no re-derivation.
  Parity with REST `X-Super-Properties` is byte-for-byte (verified by test).
- Events other than HELLO (OP 10) and READY are DISCARDED. The session is
  purely dormant presence — zero business logic.
- HEARTBEAT interval clamped to [1000, 120000] ms and applied with
  `random.uniform(0.8, 1.0) * interval_ms` jitter to imitate real clients.
- OPCODE 3 PRESENCE sent exactly ONCE after READY.
- On disconnect: OPCODE 6 RESUME is tried first (session_id + sequence). On
  RESUME failure (Invalid Session, OPCODE 9) → fresh IDENTIFY.

This module deliberately does NOT try to parse every gateway event — that
would be detectable traffic and is also out of scope for Stage 2.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal

import websockets
from filelock import FileLock, Timeout
from pydantic import SecretStr

from discord_scanner.config import Settings
from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)

GATEWAY_URL: Final[str] = "wss://gateway.discord.gg/?v=10&encoding=json"

# Opcode constants per the Discord gateway protocol.
OP_DISPATCH: Final[int] = 0
OP_HEARTBEAT: Final[int] = 1
OP_IDENTIFY: Final[int] = 2
OP_PRESENCE_UPDATE: Final[int] = 3
OP_RESUME: Final[int] = 6
OP_RECONNECT: Final[int] = 7
OP_INVALID_SESSION: Final[int] = 9
OP_HELLO: Final[int] = 10
OP_HEARTBEAT_ACK: Final[int] = 11

# Heartbeat clamp per seed-spec §2.5 / claude-rules "Gateway dormant presence".
MIN_HEARTBEAT_MS: Final[int] = 1000
MAX_HEARTBEAT_MS: Final[int] = 120_000


class GatewayError(RuntimeError):
    """Gateway-layer failure. Callers typically map this to CLI exit 2."""


class GatewayConcurrencyError(GatewayError):
    """Raised when another instance already holds the gateway lock for this burner.

    SEC-P0-13: two concurrent scans with the same burner would doubles the
    gateway IDENTIFY footprint and guarantee a Discord flag. Lock via `filelock`
    with `timeout=0` prevents it.
    """


@dataclass
class _SessionState:
    """Mutable session state — session_id + last sequence from DISPATCH events."""

    session_id: str | None = None
    sequence: int | None = None
    heartbeat_interval_ms: int | None = None
    presence_sent: bool = False
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "identify": 0,
            "resume_ok": 0,
            "resume_fail": 0,
            "heartbeat_sent": 0,
            "heartbeat_ack": 0,
            "events_discarded": 0,
        }
    )


class DormantGateway:
    """WebSocket gateway that connects, identifies, heartbeats, and stays online.

    Intended lifecycle: `async with DormantGateway(...) as gw: await gw.run_forever()`
    or explicit `await gw.connect()` / `await gw.close()` when the scan manages
    the lifetime directly. The REST client and gateway are both owned by the
    top-level scan orchestrator (P9).
    """

    def __init__(
        self,
        settings: Settings,
        token: SecretStr,
        state_root: Path | None = None,
        *,
        http_overrides: Any | None = None,
    ) -> None:
        # M.3 multi-burner: when `http_overrides` is supplied, shallow-merge
        # it into `settings.http` before storing. CRITICAL: the orchestrator
        # MUST pass the same overrides object to both `make_client` and
        # this class so IDENTIFY `properties` and REST `X-Super-Properties`
        # are byte-identical (SEC-P0-25).
        if http_overrides is not None:
            from discord_scanner.session.rest import _apply_http_overrides

            settings = _apply_http_overrides(settings, http_overrides)
        self._settings = settings
        self._token = token
        self._state_root = state_root or settings.run.state_root
        self._state = _SessionState()
        # Duck-typed WebSocket — the fake in tests and the real websockets client
        # both expose `recv() / send() / close()`.
        self._ws: Any | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._lock_path = self._state_root / f"gateway-{settings.auth.keyring_username}.lock"
        self._filelock: FileLock | None = None
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle

    async def __aenter__(self) -> DormantGateway:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        """Acquire filelock + open WS + complete IDENTIFY handshake."""
        if not self._settings.gateway.enabled:
            logger.warning(
                "gateway_disabled",
                advisory=(
                    "gateway.enabled=false → higher detection risk. "
                    "Set to true unless you really know why."
                ),
            )
            return
        from discord_scanner._paths import secure_mkdir

        secure_mkdir(self._state_root)
        self._acquire_lock()
        self._ws = await websockets.connect(GATEWAY_URL, max_size=2**20)
        try:
            await self._do_handshake(resume=False)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Stop heartbeat task, close WS, release filelock."""
        self._shutdown.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:  # noqa: BLE001 — best-effort close
                logger.debug("ws_close_error", err=str(e))
            self._ws = None
        if self._filelock is not None:
            self._filelock.release()
            self._filelock = None

    # ------------------------------------------------------------------
    # Handshake

    def _acquire_lock(self) -> None:
        """SEC-P0-13: second instance on the same burner → GatewayConcurrencyError.

        Also best-effort `chmod 0o600` on the lock file per claude-rules
        "every file in state/ gets 0o600" (Windows gets read-only bit only).
        """
        import os as _os

        fl = FileLock(str(self._lock_path), timeout=0)
        try:
            fl.acquire()
        except Timeout as e:
            raise GatewayConcurrencyError(
                f"another gateway session already holds {self._lock_path}"
            ) from e
        self._filelock = fl
        try:
            _os.chmod(self._lock_path, 0o600)
        except OSError as e:
            logger.debug("chmod_best_effort_failed", path=str(self._lock_path), err=str(e))

    def _build_identify_payload(self) -> dict[str, Any]:
        """Build OPCODE 2 IDENTIFY payload.

        The `properties` dict MUST be byte-for-byte identical to the dict
        encoded in `build_x_super_properties` — SEC-P0-25 single-fingerprint
        source enforcement. Parity verified by `test_gateway.py`.
        """
        return {
            "op": OP_IDENTIFY,
            "d": {
                "token": self._token.get_secret_value(),
                "capabilities": 16381,
                "properties": self._settings.http.fingerprint(),
                "presence": {
                    "status": self._settings.gateway.presence,
                    "activities": [],
                    "afk": False,
                },
                "compress": False,
                "client_state": {"guild_versions": {}},
            },
        }

    def _build_resume_payload(self) -> dict[str, Any]:
        return {
            "op": OP_RESUME,
            "d": {
                "token": self._token.get_secret_value(),
                "session_id": self._state.session_id,
                "seq": self._state.sequence,
            },
        }

    def _build_presence_payload(self) -> dict[str, Any]:
        return {
            "op": OP_PRESENCE_UPDATE,
            "d": {
                "status": self._settings.gateway.presence,
                "activities": [],
                "afk": False,
                "since": None,
            },
        }

    @staticmethod
    def _clamp_heartbeat(interval_ms: int) -> int:
        return max(MIN_HEARTBEAT_MS, min(MAX_HEARTBEAT_MS, interval_ms))

    async def _do_handshake(self, *, resume: bool) -> None:
        """HELLO → IDENTIFY/RESUME → READY → PRESENCE UPDATE (once) → heartbeat."""
        if self._ws is None:
            raise GatewayError("handshake called without open websocket")
        # 1. HELLO
        hello_raw = await self._ws.recv()
        hello = json.loads(hello_raw)
        if hello.get("op") != OP_HELLO:
            raise GatewayError(f"expected HELLO (op=10), got {hello.get('op')}")
        interval_ms = int(hello["d"]["heartbeat_interval"])
        self._state.heartbeat_interval_ms = self._clamp_heartbeat(interval_ms)
        logger.debug("gateway_hello", heartbeat_interval_ms=self._state.heartbeat_interval_ms)

        # 2. start heartbeat loop
        self._shutdown.clear()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # 3. IDENTIFY or RESUME
        if resume and self._state.session_id is not None:
            payload = self._build_resume_payload()
            logger.info("gateway_resume_send", session_id=self._state.session_id)
        else:
            payload = self._build_identify_payload()
            self._state.counters["identify"] += 1
            logger.info("gateway_identify_send")
        await self._ws.send(json.dumps(payload))

        # 4. wait for READY (or INVALID_SESSION on resume fail)
        await self._await_ready(resume=resume)

        # 5. presence once
        if not self._state.presence_sent:
            await self._ws.send(json.dumps(self._build_presence_payload()))
            self._state.presence_sent = True
            logger.debug("gateway_presence_sent")

    async def _await_ready(self, *, resume: bool) -> None:
        """Process messages until READY / RESUMED arrives OR session is invalidated."""
        if self._ws is None:
            raise GatewayError("_await_ready called without open websocket")
        while True:
            raw = await self._ws.recv()
            msg = json.loads(raw)
            op = msg.get("op")
            if op == OP_DISPATCH:
                event_type = msg.get("t")
                seq = msg.get("s")
                if isinstance(seq, int):
                    self._state.sequence = seq
                if event_type in ("READY", "RESUMED"):
                    data = msg.get("d") or {}
                    sid = data.get("session_id")
                    if isinstance(sid, str):
                        self._state.session_id = sid
                    if event_type == "RESUMED":
                        self._state.counters["resume_ok"] += 1
                    logger.info("gateway_ready", kind=event_type, session_id=self._state.session_id)
                    return
                # Any other DISPATCH is discarded during handshake.
                self._state.counters["events_discarded"] += 1
                continue
            if op == OP_INVALID_SESSION:
                if resume:
                    self._state.counters["resume_fail"] += 1
                    logger.warning("gateway_resume_invalid_session")
                    # reset and do a fresh IDENTIFY
                    self._state.session_id = None
                    self._state.sequence = None
                    self._state.presence_sent = False
                    payload = self._build_identify_payload()
                    self._state.counters["identify"] += 1
                    await self._ws.send(json.dumps(payload))
                    # fall through to the loop — next READY ends the wait
                    continue
                raise GatewayError("gateway sent INVALID_SESSION on fresh IDENTIFY")
            if op == OP_RECONNECT:
                raise GatewayError("gateway requested RECONNECT during handshake")
            # Any other opcode during handshake: discard.
            self._state.counters["events_discarded"] += 1

    # ------------------------------------------------------------------
    # Heartbeat

    async def _heartbeat_loop(self) -> None:
        """Emit OPCODE 1 HEARTBEAT per clamped interval, jittered 0.8-1.0×.

        Runs until `self._shutdown` is set. Never calls `time.sleep` — always
        `asyncio.sleep` (claude-rules + CI grep-blocker).
        """
        if self._state.heartbeat_interval_ms is None:
            logger.warning("heartbeat_loop_without_interval")
            return
        interval_sec = self._state.heartbeat_interval_ms / 1000.0
        while not self._shutdown.is_set():
            jitter = random.uniform(0.8, 1.0)  # noqa: S311 — jitter, not crypto
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval_sec * jitter)
                # shutdown requested
                return
            except TimeoutError:
                pass
            await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        if self._ws is None:
            return
        payload = {"op": OP_HEARTBEAT, "d": self._state.sequence}
        try:
            await self._ws.send(json.dumps(payload))
            self._state.counters["heartbeat_sent"] += 1
            logger.debug("gateway_heartbeat", seq=self._state.sequence)
        except Exception as e:  # noqa: BLE001 — surface once, let reconnect handle
            logger.warning("gateway_heartbeat_send_failed", err=str(e))

    # ------------------------------------------------------------------
    # Public probes (used by tests + status command)

    @property
    def counters(self) -> dict[str, int]:
        """Immutable snapshot of gateway lifecycle counters."""
        return dict(self._state.counters)

    @property
    def session_id(self) -> str | None:
        return self._state.session_id

    @property
    def sequence(self) -> int | None:
        return self._state.sequence

    @property
    def heartbeat_interval_ms(self) -> int | None:
        return self._state.heartbeat_interval_ms

    @property
    def presence_sent(self) -> bool:
        return self._state.presence_sent

    # ------------------------------------------------------------------
    # Disconnect recovery — exposed for future P9 scan loop integration

    async def reconnect_with_resume(self) -> Literal["resumed", "reidentified"]:
        """Reopen the WS and attempt OPCODE 6 RESUME; fall back to IDENTIFY.

        Returns `"resumed"` on success, `"reidentified"` on RESUME failure.

        MUST cancel the existing heartbeat task BEFORE `_do_handshake` is invoked,
        otherwise a second heartbeat loop is spawned and the effective rate
        doubles — a real detectability signal (reviewer-flagged MAJOR).
        """
        # 1. stop old heartbeat task (shutdown event is re-cleared by _do_handshake)
        self._shutdown.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._heartbeat_task
            self._heartbeat_task = None
        # 2. close the old WS if still open
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.debug("ws_close_error_on_reconnect", err=str(e))
        # 3. open a new connection + RESUME handshake
        pre_resume_fail = self._state.counters["resume_fail"]
        self._ws = await websockets.connect(GATEWAY_URL, max_size=2**20)
        await self._do_handshake(resume=True)
        if self._state.counters["resume_fail"] > pre_resume_fail:
            return "reidentified"
        return "resumed"
