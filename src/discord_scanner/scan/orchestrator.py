"""Production scan orchestrator — composes the building blocks for the live
`discord-scanner scan` and `discord-scanner daemon` CLI paths.

Traces to:
- AGENT-TEAM-WORKPLAN.md §A.0 (5-sub-task delivery contract)
- workspace plan §"Part A — A.3 / Phase C" (backfill termination signals)
- seed-spec.md §2.7 (cursor advance AFTER fsync — durability rule)
- security-model.md §6 SEC-P0-15 (per-channel error isolation)
- claude-rules.md MUST/MUST NOT (httpx-only, no Discord libs, etc.)

The orchestrator's job is to add coordination over the already-tested building
blocks: error isolation per channel, multi-channel + multi-guild aggregation,
forward + backward (backfill) cursor management, gateway lifecycle, structured
run-summary telemetry. Building blocks themselves remain unchanged.

A.0.1 ships:
- `run_one_pass` — guild loop + per-guild scan
- `scan_one_channel` — single-channel forward fetch + dump + cursor.advance,
  per-channel error isolation around the four primary exception classes
  (`ChannelAbort`, `RetryableResponseError` exhaustion, `httpx.TransportError`,
  `pydantic.ValidationError` per-record)
- `_open_shared_resources` — async context manager for client + cursor lock

A.0.2 will extend the exception matrix to all 7 classes.
A.0.3 will add backfill direction inside `scan_one_channel`.
A.0.4 will wire `AdaptiveRateLimiter.record_response` (already wired via
`request_with_retry(adaptive=...)` — this sub-task surfaces the limiter to
the orchestrator and threads it through).
A.0.5 will replace `cli.py:_stub_scan` and `cli.py::scan` stubs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from discord_scanner.config import BurnerHttpOverrides, Settings
from discord_scanner.cursor.lock import CursorLock
from discord_scanner.cursor.state import CursorStore
from discord_scanner.discovery.channels import GuildSelector, filter_channels, list_channels
from discord_scanner.discovery.guilds import list_my_guilds
from discord_scanner.discovery.forums import list_archived_public_threads
from discord_scanner.dump.jsonl_writer import write_jsonl
from discord_scanner.fetch.attachments import download_attachment
from discord_scanner.fetch.threads import fetch_thread_messages
from discord_scanner.dump.meta import ScanCounters, ScanMeta, write_meta
from discord_scanner.dump.prior import resolve_prior_date, write_prior
from discord_scanner.dump.video_triage import (
    VideoTriageWriter,
    is_gif,
    is_video,
    write_video_triage_md,
)
from discord_scanner.dump.zstd_writer import write_jsonl_zst
from discord_scanner.fetch.messages import (
    BackfillStop,
    BackfillTermination,
    ForwardTermination,
    fetch_channel_messages,
    fetch_channel_messages_backward,
)
from discord_scanner.fetch.pinned import fetch_channel_pinned
from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import Channel
from discord_scanner.models.message import Message
from discord_scanner.cursor.lock import CursorConcurrencyError
from discord_scanner.scan.scopes import ScopeMap, ScopeProfile, load_scope_profiles
from discord_scanner.discovery.invite_resolve import TokenInvalid
from discord_scanner.session.captcha import CaptchaAborted
from discord_scanner.session.gateway import (
    DormantGateway,
    GatewayConcurrencyError,
    GatewayError,
)
from discord_scanner.session.rate_limit import burst_pause
from discord_scanner.session.rest import SSRFViolation, make_client, persist_cookies
from discord_scanner.session.retry import ChannelAbort, RetryableResponseError

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Run-fatal exception (raised by orchestrator on conditions that abort the
# entire scan, not just a single channel — see A.0.2 isolation matrix).
# ---------------------------------------------------------------------------


class FatalScanError(RuntimeError):
    """Raised by `run_one_pass` when a non-recoverable condition is hit
    (token invalid, captcha, SSRF violation, gateway lock contention,
    cursor lock contention). The CLI maps the `code` field to an exit
    status:
        1 = generic fatal (cursor/gateway lock contention)
        2 = captcha / SSRF (operator action required)
        3 = token invalid / banned
    """

    def __init__(self, *, code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason


# ---------------------------------------------------------------------------
# Result dataclasses (workplan §"A.0 — orchestrator design" Q1)
# ---------------------------------------------------------------------------


@dataclass
class ChannelResult:
    """Per-channel outcome aggregated by `run_one_pass`. Never raises — all
    exceptions captured in `error` so one bad channel doesn't kill the run."""

    guild_id: str
    channel_id: str
    channel_name: str | None
    messages_fetched: int = 0
    pinned_fetched: int = 0
    thread_messages_fetched: int = 0
    threads_scanned: int = 0
    attachments_downloaded: int = 0
    attachments_skipped_mime: int = 0
    attachments_skipped_other: int = 0
    # v3 — non-GIF videos sent to `video-triage.jsonl` instead of being
    # downloaded. GIFs are silent-skipped and NOT counted (they're noise
    # per operator decision 2026-04-26).
    video_attachments_triaged: int = 0
    backfill_messages_fetched: int = 0
    backfill_stop_reason: str | None = None
    backfill_marked_complete: bool = False
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None
    duration_sec: float = 0.0
    last_message_id: str | None = None
    oldest_message_id: str | None = None


@dataclass
class RunResult:
    """Per-guild aggregation. Returned to the CLI for an exit-status decision
    and structured logging."""

    guild_id: str
    guild_name: str | None
    scan_date: str
    started_at: str
    finished_at: str
    duration_sec: float
    channels_scanned: int = 0
    channels_skipped: int = 0
    channels_failed: int = 0
    messages_fetched: int = 0
    pinned_fetched: int = 0
    fatal_error: str | None = None
    channel_results: list[ChannelResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared resource bundle (internal)
# ---------------------------------------------------------------------------


@dataclass
class _SharedResources:
    """Live handles passed down into per-channel scanners. Owned by
    `_open_shared_resources` — never instantiate directly."""

    client: httpx.AsyncClient
    cursor: CursorStore
    settings: Settings
    scan_date: str
    gateway: DormantGateway | None = None
    scope_map: ScopeMap = field(default_factory=ScopeMap)
    # M.1: identifies which burner is doing the work — recorded into the
    # cursor's `burner_id` column so the operator can `discord-scanner
    # status` and see which burner owns each channel. Empty string for
    # legacy single-burner mode.
    burner_id: str = ""
    # v3 — per-guild video triage writers, lazily created the first time a
    # non-GIF video attachment is encountered. Keyed by guild_id so each
    # guild gets its own `output/{guild_id}/{date}/video-triage.jsonl`.
    video_triage_by_guild: dict[str, VideoTriageWriter] = field(
        default_factory=dict
    )


def _settings_for_burner(
    settings: Settings,
    *,
    http_overrides: BurnerHttpOverrides | None,
    keyring_username: str | None,
) -> Settings:
    """Return a per-burner settings snapshot.

    When neither `http_overrides` nor `keyring_username` is set, returns the
    input unchanged (single-burner legacy mode). Otherwise builds a shallow
    copy with `http.*` fields merged from `http_overrides` and/or
    `auth.keyring_username` swapped — both REST `make_client` and
    `DormantGateway` read from the SAME snapshot, guaranteeing IDENTIFY blob
    parity with `X-Super-Properties` (CLAUDE.md MUST, SEC-P0-25).
    """
    if http_overrides is None and keyring_username is None:
        return settings

    new_http = settings.http
    if http_overrides is not None:
        # `model_copy(update=...)` is shallow; only the named fields are
        # replaced. None values in BurnerHttpOverrides mean "fall back to
        # global default" — drop them from the update dict.
        update: dict[str, Any] = {}
        if http_overrides.user_agent_chrome_version is not None:
            update["user_agent_chrome_version"] = (
                http_overrides.user_agent_chrome_version
            )
        if http_overrides.fake_os_platform is not None:
            update["fake_os_platform"] = http_overrides.fake_os_platform
        if http_overrides.fake_os is not None:
            update["fake_os"] = http_overrides.fake_os
        if http_overrides.locale is not None:
            update["locale"] = http_overrides.locale
        if http_overrides.client_build_number is not None:
            update["client_build_number"] = http_overrides.client_build_number
        if update:
            new_http = settings.http.model_copy(update=update)

    new_auth = settings.auth
    if keyring_username is not None and keyring_username != settings.auth.keyring_username:
        new_auth = settings.auth.model_copy(update={"keyring_username": keyring_username})

    if new_http is settings.http and new_auth is settings.auth:
        return settings
    return settings.model_copy(update={"http": new_http, "auth": new_auth})


@asynccontextmanager
async def _open_shared_resources(
    settings: Settings,
    token: SecretStr,
    *,
    scan_date: str | None = None,
    scopes_dir: Path | None = None,
    burner_id: str = "",
    http_overrides: BurnerHttpOverrides | None = None,
    keyring_username: str | None = None,
) -> AsyncIterator[_SharedResources]:
    """Acquire client + cursor lock + cursor store. Cleanup is reverse-of-
    open per workplan §"Cleanup contract".

    Multi-burner (M.1/M.3): when `http_overrides` is set, build a
    settings-snapshot with merged `http.*` fields and pass to BOTH
    `make_client` and `DormantGateway` — IDENTIFY blob and X-Super-
    Properties must read from the same snapshot (CLAUDE.md MUST byte-for-
    byte parity, SEC-P0-25). `keyring_username` overrides
    `settings.auth.keyring_username` for per-burner cookie jar + gateway
    lock paths without mutating the shared Settings instance.
    """
    if scan_date is None:
        scan_date = datetime.now(UTC).strftime("%Y-%m-%d")

    # Build a per-burner settings snapshot when http_overrides or
    # keyring_username are set. Pydantic `model_copy(update=...)` returns a
    # shallow copy with the named fields replaced; the original `settings`
    # is untouched so the caller can iterate burners safely.
    burner_settings = _settings_for_burner(
        settings, http_overrides=http_overrides, keyring_username=keyring_username
    )

    # Load scope profiles before opening any network. `scopes_dir` defaults to
    # `<project_root>/scopes/` per AGENT-TEAM-WORKPLAN §A.2.
    if scopes_dir is None:
        # Default lookup: walk up from state_root to find scopes/ at project root.
        candidate = burner_settings.run.state_root.parent / "scopes"
        scopes_dir = candidate if candidate.exists() else Path("scopes")
    scope_map = load_scope_profiles(scopes_dir)

    # Wire `config.discovery.guilds[]` into scope_map. README and seed-spec
    # promise that `tools/csv_to_scan_scope.py` output (pasted under
    # `discovery.guilds[]`) drives per-guild channel selection at scan-time;
    # without this synthesis the orchestrator would only see explicit
    # `scopes/*.yaml` profiles and treat all other guilds as unscoped
    # (fail-open → scan everything). Explicit scope profiles still win.
    for entry in burner_settings.discovery.guilds:
        if not entry.id or entry.id in scope_map.guild_to_scope:
            continue
        synthetic_scope_id = f"__discovery_{entry.id}"
        scope_map.profiles_by_id[synthetic_scope_id] = ScopeProfile(
            scope_id=synthetic_scope_id,
            guilds=[entry.id],
            allowed_categories=[],
            allowed_pipeline_kinds=["t2i"],  # placeholder; unused at scan time
            allowed_arch_families=[],
            keep_threshold=0.55,
            folder_prefix="",
            tag_prefix="topic/discovery",
            nsfw_policy="keep",
            vault="main",
            persona_anchor_id=None,
            intent_allowlist=[],
            selector=GuildSelector(
                categories=list(entry.selectors.categories),
                channels=list(entry.selectors.channels),
                exclude_channels=list(entry.selectors.exclude_channels),
                fail_open=entry.selectors.fail_open,
            ),
            burner=None,
        )
        scope_map.guild_to_scope[entry.id] = synthetic_scope_id

    # Step 1 — Gateway dormant session FIRST (CLAUDE.md MUST: a session that
    # only does REST without the WS handshake stands out as a self-bot in
    # Discord telemetry). Skipped only when explicitly disabled in config
    # (e.g. tests that don't need it). On `GatewayConcurrencyError` the
    # filelock is held by another scan instance; convert to FatalScanError
    # so CLI exits 1.
    gateway: DormantGateway | None = None
    if burner_settings.gateway.enabled:
        gateway = DormantGateway(
            burner_settings, token, state_root=burner_settings.run.state_root
        )
        try:
            await gateway.connect()
        except GatewayConcurrencyError as e:
            raise FatalScanError(
                code=1,
                reason="gateway_lock_contention",
                message=f"gateway lock held by another scan: {e}",
            ) from e
        except GatewayError as e:
            # Gateway handshake itself failed (network, captcha at WS, etc.).
            # Bubble up as fatal — no point doing REST without the dormant
            # presence (anti-detection invariant).
            raise FatalScanError(
                code=2,
                reason="gateway_handshake_failed",
                message=f"gateway handshake failed: {e}",
            ) from e

    # Step 2 — Shared REST client (uses the same per-burner snapshot so
    # X-Super-Properties matches the IDENTIFY blob byte-for-byte).
    client = make_client(
        burner_settings, token, state_root=burner_settings.run.state_root
    )
    cursor_lock_ctx = CursorLock(burner_settings.run.state_root)
    try:
        cursor_lock_ctx.__enter__()
    except CursorConcurrencyError as e:
        # Another scan is in progress against the same state_root.
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
        if gateway is not None:
            try:
                await gateway.close()
            except Exception:  # noqa: BLE001
                pass
        raise FatalScanError(
            code=1,
            reason="cursor_lock_contention",
            message=f"another scan is in progress: {e}",
        ) from e
    resources_ref: _SharedResources | None = None
    try:
        with CursorStore(burner_settings.run.state_root) as cursor:
            resources_ref = _SharedResources(
                client=client,
                cursor=cursor,
                settings=burner_settings,
                scan_date=scan_date,
                gateway=gateway,
                scope_map=scope_map,
                burner_id=burner_id,
            )
            yield resources_ref
    finally:
        # Cleanup order matters — reverse-of-open per workplan §"Cleanup
        # contract". Gateway closes LAST so the WSS close frame can be sent
        # before the event loop tears down. Each step in its own try so a
        # downstream failure doesn't mask an upstream one.
        # v3 — flush video triage writers before client teardown so any
        # logging happens before the structlog timestamp processor sees
        # client-close noise. Per-guild JSONL + matching markdown view.
        if resources_ref is not None and resources_ref.video_triage_by_guild:
            for gid, writer in resources_ref.video_triage_by_guild.items():
                try:
                    jsonl_path = writer.flush()
                    if jsonl_path is not None:
                        write_video_triage_md(
                            jsonl_path, jsonl_path.with_suffix(".md")
                        )
                except Exception as e:  # noqa: BLE001 — best-effort sidecar
                    logger.warning(
                        "video_triage_flush_failed",
                        guild_id=gid,
                        err=str(e),
                    )
        try:
            persist_cookies(
                client, burner_settings, burner_settings.run.state_root
            )
        except Exception as e:  # noqa: BLE001 — best-effort; never block teardown
            logger.warning("persist_cookies_failed", err=str(e))
        try:
            await client.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning("client_aclose_failed", err=str(e))
        try:
            cursor_lock_ctx.__exit__(None, None, None)
        except Exception as e:  # noqa: BLE001
            logger.warning("cursor_lock_release_failed", err=str(e))
        if gateway is not None:
            try:
                await gateway.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("gateway_close_failed", err=str(e))


# ---------------------------------------------------------------------------
# Per-channel scan
# ---------------------------------------------------------------------------


async def scan_one_channel(
    resources: _SharedResources,
    guild_id: str,
    channel: Channel,
    settings: Settings,
) -> ChannelResult:
    """Fetch one channel's messages + pinned, write JSONL, advance cursor.

    Returns `ChannelResult` — never raises. Caller (`run_one_pass`) iterates
    channels and aggregates results. Per-channel error isolation: any
    exception is caught, encoded in the result, and the next channel runs.

    Cursor advance happens ONLY AFTER the JSONL fsync (`write_jsonl_zst`
    and `write_jsonl` both call `fh.flush() + os.fsync(fh.fileno())` before
    returning). This honours seed-spec §2.7's durability invariant.
    """
    started = time.monotonic()
    result = ChannelResult(
        guild_id=guild_id,
        channel_id=channel.id,
        channel_name=channel.name,
    )

    try:
        last_id = resources.cursor.get(guild_id, channel.id)
        logger.info(
            "channel.start",
            guild_id=guild_id,
            channel_id=channel.id,
            channel_name=channel.name,
            forward_cursor=last_id,
        )

        # Forward fetch — use ForwardTermination out-param so we can
        # distinguish "0 new messages cleanly" from "fetch was aborted".
        # Without this signal, ChannelAbort is swallowed inside the fetcher
        # (see messages.py:167) and we'd proceed to pinned-fetch on a
        # rate-limited / unreachable channel.
        forward_term = ForwardTermination()
        msgs_raw = [
            m
            async for m in fetch_channel_messages(
                resources.client,
                channel.id,
                settings=settings,
                after=last_id,
                result=forward_term,
            )
        ]
        result.messages_fetched = len(msgs_raw)

        if forward_term.aborted:
            # Skip pinned + cursor advance — we don't have authoritative
            # forward-edge data for this channel. Cursor stays where it
            # was so the next scan retries from the same point.
            result.skipped = True
            result.skip_reason = forward_term.abort_reason or "fetch_aborted"
            result.error = f"forward fetch aborted: {forward_term.abort_reason}"
            logger.warning(
                "channel.skip",
                guild_id=guild_id,
                channel_id=channel.id,
                reason=result.skip_reason,
            )
            result.duration_sec = time.monotonic() - started
            logger.info(
                "channel.summary",
                guild_id=guild_id,
                channel_id=channel.id,
                channel_name=channel.name,
                messages_fetched=result.messages_fetched,
                pinned_fetched=0,
                skipped=True,
                skip_reason=result.skip_reason,
                duration_sec=round(result.duration_sec, 3),
            )
            return result

        # Pinned (no cursor; full list each pass — small)
        pins_raw = [
            m
            async for m in fetch_channel_pinned(
                resources.client, channel.id, settings=settings
            )
        ]
        result.pinned_fetched = len(pins_raw)

        # Coerce to typed Message records — per-record validation tolerance:
        # ValidationError on one record skips it with a WARN, doesn't abort
        # the channel. Mirrors the existing tolerance in fetch_channel_messages.
        messages: list[Message] = []
        for raw in msgs_raw:
            try:
                messages.append(
                    Message.from_api(
                        raw,
                        guild_id=guild_id,
                        channel_id=channel.id,
                        channel_name=channel.name,
                    )
                )
            except (ValidationError, ValueError) as e:
                logger.warning(
                    "message_validation_error",
                    channel_id=channel.id,
                    msg_id=raw.get("id"),
                    err=str(e),
                )
        pinned_records: list[Message] = []
        for raw in pins_raw:
            try:
                pinned_records.append(
                    Message.from_api(
                        raw,
                        guild_id=guild_id,
                        channel_id=channel.id,
                        channel_name=channel.name,
                    )
                )
            except (ValidationError, ValueError) as e:
                logger.warning(
                    "pinned_validation_error",
                    channel_id=channel.id,
                    msg_id=raw.get("id"),
                    err=str(e),
                )

        # Write artefacts (fsync inside writers — see jsonl_writer.py:53,
        # zstd_writer.py:64). No cursor advance until ALL writes return.
        if messages or pinned_records:
            day_dir = resources.settings.run.output_root / guild_id / resources.scan_date
            day_dir.mkdir(parents=True, exist_ok=True)
            if messages:
                write_jsonl_zst(day_dir / "messages.jsonl.zst", messages)
            if pinned_records:
                write_jsonl(day_dir / "pinned.jsonl", pinned_records)

            # Cursor advance — ONLY after fsync (durability rule). The
            # `messages` list is already sorted oldest→newest by the fetcher
            # (`fetch_channel_messages` sorts by numeric snowflake before
            # yielding). Take the max id as the new forward cursor.
            if messages:
                new_last = max(m.message_id for m in messages)
                resources.cursor.advance(
                    guild_id,
                    channel.id,
                    new_last,
                    burner_id=resources.burner_id,
                )
                result.last_message_id = new_last

        # ── ATTACHMENTS (A.9) — download images referenced in messages ──
        if settings.attachments.download_images and messages:
            await _download_message_attachments(
                resources, guild_id, channel, messages, settings, result
            )

        # ── THREADS (A.10) — for forum channels (type 15), enumerate
        # active+archived public threads and fetch each one's messages ──
        if channel.type == 15:
            await _scan_channel_threads(
                resources, guild_id, channel, settings, result
            )

        # ── BACKFILL PHASE (A.0.3) — only when enabled + eligible ──
        if settings.backfill.enabled:
            await _run_backfill(
                resources, guild_id, channel, settings, result
            )

    # Fatal exceptions — propagate up to run_one_pass (do NOT swallow)
    except (
        TokenInvalid,
        CaptchaAborted,
        SSRFViolation,
        GatewayConcurrencyError,
        CursorConcurrencyError,
    ):
        # These conditions invalidate the entire scan run, not just one
        # channel. Re-raise; run_one_pass converts to FatalScanError with
        # the appropriate exit code.
        raise

    # Per-channel isolated exceptions — captured + skipped, run continues.
    except GatewayError as e:
        # Mid-scan gateway error (handshake already completed). The dormant
        # session's job is done by the time channel scans run; a late-error
        # here is recoverable per channel — skip and continue. (Wholesale
        # gateway loss across all channels would surface as a different
        # signal, but A.0.1 doesn't open the gateway anyway.)
        result.skipped = True
        result.skip_reason = "gateway_error"
        result.error = str(e)
        logger.warning(
            "channel.skip",
            guild_id=guild_id,
            channel_id=channel.id,
            reason="gateway_error",
        )
    except ChannelAbort as e:
        # 3× consecutive 429s → skip this channel, don't kill the run.
        result.skipped = True
        result.skip_reason = "channel_abort"
        result.error = str(e)
        logger.warning(
            "channel.skip",
            guild_id=guild_id,
            channel_id=channel.id,
            reason="channel_abort",
        )
    except RetryableResponseError as e:
        # Retries exhausted on this channel — skip + continue.
        result.skipped = True
        result.skip_reason = "retries_exhausted"
        result.error = f"retries_exhausted status={e.status} url={e.url}"
        logger.warning(
            "channel.skip",
            guild_id=guild_id,
            channel_id=channel.id,
            reason="retries_exhausted",
            status=e.status,
        )
    except httpx.TransportError as e:
        # Network drop mid-channel — skip + continue.
        result.skipped = True
        result.skip_reason = "transport_error"
        result.error = str(e)
        logger.warning(
            "channel.skip",
            guild_id=guild_id,
            channel_id=channel.id,
            reason="transport_error",
        )
    except Exception as e:  # noqa: BLE001 — defensive; defence-in-depth
        # Anything else — record + continue. Should be rare; an unanticipated
        # exception class shouldn't kill the entire scan run.
        result.skipped = True
        result.skip_reason = "unexpected_error"
        result.error = f"{type(e).__name__}: {e}"
        logger.exception(
            "channel.unexpected_error",
            guild_id=guild_id,
            channel_id=channel.id,
        )

    result.duration_sec = time.monotonic() - started
    logger.info(
        "channel.summary",
        guild_id=guild_id,
        channel_id=channel.id,
        channel_name=channel.name,
        messages_fetched=result.messages_fetched,
        pinned_fetched=result.pinned_fetched,
        skipped=result.skipped,
        skip_reason=result.skip_reason,
        duration_sec=round(result.duration_sec, 3),
    )
    return result


# ---------------------------------------------------------------------------
# Attachment helper (A.9) — download images referenced in messages
# ---------------------------------------------------------------------------


async def _download_message_attachments(
    resources: _SharedResources,
    guild_id: str,
    channel: Channel,
    messages: list[Message],
    settings: Settings,
    result: ChannelResult,
) -> None:
    """Iterate every message's attachments; download images via
    `download_attachment` (handles MIME sniff + size cap + path-traversal
    guard internally). Mutates `result` counters in place.

    Per-attachment errors are isolated via `download_attachment`'s
    `DownloadResult` (never raises for expected security rejections).
    """
    allowed_exts = tuple(settings.attachments.image_extensions)
    max_mb = settings.attachments.max_size_mb
    for msg in messages:
        for att in msg.attachments:
            if not att.cdn_url or not att.filename:
                continue
            # v3 — silent GIF skip BEFORE the download path. Operator
            # decision 2026-04-26: GIFs are noise; not downloaded, not
            # triaged, not logged at INFO. DEBUG line lets us audit.
            if is_gif(att.content_type, att.filename, att.cdn_url):
                logger.debug(
                    "attachment_skipped_gif",
                    channel_id=channel.id,
                    msg_id=msg.message_id,
                )
                continue
            # v3 — non-GIF videos go to triage JSONL, NEVER the disk.
            # Stage 3 reads metadata only (CLAUDE.md MUST NOT model-weight
            # or binary-blob downloads).
            if is_video(att.content_type, att.filename):
                writer = resources.video_triage_by_guild.get(guild_id)
                if writer is None:
                    writer = VideoTriageWriter(
                        output_root=settings.run.output_root,
                        guild_id=guild_id,
                        date_str=resources.scan_date,
                    )
                    resources.video_triage_by_guild[guild_id] = writer
                writer.append_record(
                    message=msg,
                    attachment=att,
                    channel=channel,
                    guild_id=guild_id,
                )
                result.video_attachments_triaged += 1
                continue
            try:
                outcome = await download_attachment(
                    resources.client,
                    cdn_url=att.cdn_url,
                    raw_filename=att.filename,
                    msg_id=msg.message_id,
                    guild_id=guild_id,
                    date_str=resources.scan_date,
                    output_root=settings.run.output_root,
                    max_size_mb=max_mb,
                    allowed_extensions=allowed_exts,
                )
            except Exception as e:  # noqa: BLE001 — per-attachment isolation
                logger.warning(
                    "attachment_download_unexpected_error",
                    channel_id=channel.id,
                    msg_id=msg.message_id,
                    err=str(e),
                )
                result.attachments_skipped_other += 1
                continue
            if outcome.reason == "ok":
                result.attachments_downloaded += 1
            elif outcome.reason == "mime_mismatch":
                result.attachments_skipped_mime += 1
            else:
                result.attachments_skipped_other += 1


# ---------------------------------------------------------------------------
# Threads helper (A.10) — for forum channels (type 15), scan each thread
# ---------------------------------------------------------------------------


async def _scan_channel_threads(
    resources: _SharedResources,
    guild_id: str,
    channel: Channel,
    settings: Settings,
    result: ChannelResult,
) -> None:
    """Enumerate active + archived public threads under a forum channel
    (type 15) and fetch each thread's messages. Writes to a separate
    `threads.jsonl` file so forward-channel and threads aren't conflated.

    Per-thread errors are caught + logged; one failed thread does NOT
    abort the parent channel. Threads that contributed messages are
    recorded in `result.threads_scanned`.
    """
    try:
        threads = await list_archived_public_threads(
            resources.client, channel.id, max_pages=20
        )
    except Exception as e:  # noqa: BLE001 — discovery error → 0 threads
        logger.warning(
            "thread_discovery_failed",
            channel_id=channel.id,
            err=str(e),
        )
        return
    if not threads:
        return

    all_thread_messages: list[Message] = []
    for thread in threads:
        thread_id = thread.id
        thread_name = thread.name or thread_id
        try:
            raw_msgs = [
                m
                async for m in fetch_thread_messages(
                    resources.client, thread_id, settings=settings
                )
            ]
        except Exception as e:  # noqa: BLE001 — per-thread isolation
            logger.warning(
                "thread_fetch_failed",
                channel_id=channel.id,
                thread_id=thread_id,
                err=str(e),
            )
            continue
        if not raw_msgs:
            continue
        result.threads_scanned += 1
        for raw in raw_msgs:
            try:
                all_thread_messages.append(
                    Message.from_api(
                        raw,
                        guild_id=guild_id,
                        channel_id=thread_id,
                        channel_name=(
                            f"{channel.name}/{thread_name}"
                            if channel.name
                            else thread_name
                        ),
                    )
                )
            except (ValidationError, ValueError) as e:
                logger.warning(
                    "thread_message_validation_error",
                    thread_id=thread_id,
                    msg_id=raw.get("id"),
                    err=str(e),
                )

    if all_thread_messages:
        day_dir = settings.run.output_root / guild_id / resources.scan_date
        day_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(day_dir / "threads.jsonl", all_thread_messages)
        result.thread_messages_fetched = len(all_thread_messages)
        # Note: thread cursors are NOT advanced here — Stage 2 v1 contract
        # doesn't track per-thread cursors. Stage 3 reads each scan-day's
        # threads.jsonl as a fresh snapshot. This matches the e2e smoke
        # test pattern (write threads.jsonl with empty list when no threads).


# ---------------------------------------------------------------------------
# Backfill helper (A.0.3) — walk backward toward channel start
# ---------------------------------------------------------------------------


async def _run_backfill(
    resources: _SharedResources,
    guild_id: str,
    channel: Channel,
    settings: Settings,
    result: ChannelResult,
) -> None:
    """Run the backward (backfill) walk for one channel.

    Skips when:
    - the channel is already `backfill_complete=True`
    - `backfill_runs >= backfill.max_scan_runs_per_channel` (safety bound)

    Calls `mark_backfilled` ONLY when `BackfillTermination.channel_start_confirmed`
    is True (covers both `CHANNEL_START` and `STUCK_CURSOR` reasons —
    the SHORT_PAGE case explicitly does NOT confirm channel start, so
    next scan retries from the new oldest).

    Mutates `result` in place — adds counters and stop reason.
    """
    cursor_obj = resources.cursor
    frontier = cursor_obj.get_frontier(guild_id, channel.id)

    if frontier.backfill_complete:
        logger.debug(
            "backfill.skip_complete",
            guild_id=guild_id,
            channel_id=channel.id,
        )
        return

    max_runs = settings.backfill.max_scan_runs_per_channel
    if frontier.backfill_runs >= max_runs:
        logger.info(
            "backfill.skip_max_runs",
            guild_id=guild_id,
            channel_id=channel.id,
            backfill_runs=frontier.backfill_runs,
            max_runs=max_runs,
        )
        return

    logger.info(
        "backfill.start",
        guild_id=guild_id,
        channel_id=channel.id,
        oldest_seen=frontier.oldest_seen_message_id,
        budget=settings.backfill.per_scan_message_budget,
        runs_so_far=frontier.backfill_runs,
    )

    termination = BackfillTermination()
    backfill_msgs_raw = [
        m
        async for m in fetch_channel_messages_backward(
            resources.client,
            channel.id,
            settings=settings,
            before=frontier.oldest_seen_message_id,
            max_messages=settings.backfill.per_scan_message_budget,
            result=termination,
        )
    ]

    # Coerce + filter malformed records (same per-record tolerance as forward).
    backfill_messages: list[Message] = []
    for raw in backfill_msgs_raw:
        try:
            backfill_messages.append(
                Message.from_api(
                    raw,
                    guild_id=guild_id,
                    channel_id=channel.id,
                    channel_name=channel.name,
                )
            )
        except (ValidationError, ValueError) as e:
            logger.warning(
                "backfill_message_validation_error",
                channel_id=channel.id,
                msg_id=raw.get("id"),
                err=str(e),
            )

    result.backfill_messages_fetched = len(backfill_messages)
    result.backfill_stop_reason = termination.reason.value

    # Write artefacts to a separate file so forward + backward don't collide
    # (workplan §"Risk 4 — Output naming collision"). Stage 3 reads both.
    if backfill_messages:
        day_dir = settings.run.output_root / guild_id / resources.scan_date
        day_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl_zst(day_dir / "backfill.jsonl.zst", backfill_messages)

        # Cursor advance_backward — ONLY after fsync (durability rule).
        if termination.last_oldest_id is not None:
            cursor_obj.advance_backward(
                guild_id,
                channel.id,
                termination.last_oldest_id,
                burner_id=resources.burner_id,
            )
            result.oldest_message_id = termination.last_oldest_id

    # Increment runs counter (regardless of whether we wrote anything — the
    # safety bound counts API attempts, not byte volume).
    cursor_obj.increment_backfill_runs(
        guild_id, channel.id, burner_id=resources.burner_id
    )

    # Mark complete ONLY when channel_start_confirmed (CHANNEL_START or
    # STUCK_CURSOR). SHORT_PAGE doesn't confirm — could be deletion gap.
    # This is the workplan §"Risk 1 — mark_backfilled guard" check.
    if termination.channel_start_confirmed:
        cursor_obj.mark_backfilled(guild_id, channel.id)
        result.backfill_marked_complete = True

    logger.info(
        "backfill.summary",
        guild_id=guild_id,
        channel_id=channel.id,
        messages_fetched=result.backfill_messages_fetched,
        pages_fetched=termination.pages_fetched,
        stop_reason=result.backfill_stop_reason,
        channel_start_confirmed=termination.channel_start_confirmed,
        marked_backfilled=result.backfill_marked_complete,
    )


# ---------------------------------------------------------------------------
# Per-guild scan
# ---------------------------------------------------------------------------


async def _scan_one_guild(
    resources: _SharedResources,
    guild_id: str,
    guild_name: str | None,
    settings: Settings,
) -> RunResult:
    """Scan every scannable channel in a guild. Returns RunResult.

    Channels are scanned sequentially — `RateLimiter`'s 2 req/s API token
    bucket and the per-channel burst-pause are deliberately serialised
    (workplan §"Concurrency model": sequential, single-guild, single-
    channel-at-a-time).
    """
    started_monotonic = time.monotonic()
    started_at_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    run_result = RunResult(
        guild_id=guild_id,
        guild_name=guild_name,
        scan_date=resources.scan_date,
        started_at=started_at_iso,
        finished_at="",  # filled below
        duration_sec=0.0,
    )

    try:
        all_channels = await list_channels(resources.client, guild_id)
    except Exception as e:  # noqa: BLE001 — discovery failure aborts only this guild
        run_result.fatal_error = f"list_channels_failed: {e}"
        logger.error(
            "run.guild_fatal",
            guild_id=guild_id,
            reason="list_channels_failed",
            err=str(e),
        )
        run_result.finished_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_result.duration_sec = time.monotonic() - started_monotonic
        return run_result

    # Apply per-guild selector if a scope profile owns this guild (Phase B v2).
    # Empty/missing selector falls back to v1 type-filter behaviour.
    selector = resources.scope_map.get_selector_for_guild(guild_id)
    output_tag = resources.scope_map.get_output_tag_for_guild(guild_id)
    has_selector = (
        selector is not None
        and (selector.categories or selector.channels or selector.exclude_channels)
    )
    scannable = filter_channels(all_channels, selector=selector if has_selector else None)
    logger.info(
        "guild.selector_applied",
        guild_id=guild_id,
        scope_id=resources.scope_map.guild_to_scope.get(guild_id),
        output_tag=output_tag,
        scannable_count=len(scannable),
        all_channels_count=len(all_channels),
    )

    for idx, channel in enumerate(scannable):
        channel_result = await scan_one_channel(
            resources, guild_id, channel, settings
        )
        run_result.channel_results.append(channel_result)
        run_result.messages_fetched += channel_result.messages_fetched
        run_result.pinned_fetched += channel_result.pinned_fetched
        if channel_result.skipped:
            run_result.channels_skipped += 1
        elif channel_result.error is not None:
            run_result.channels_failed += 1
        else:
            run_result.channels_scanned += 1

        # Inter-channel burst pause — anti-detection (claude-rules MUST
        # "Rate-limit + jitter"). Skip after the last channel.
        if idx < len(scannable) - 1:
            await burst_pause(settings.http.burst_pause_sec)

    # Write per-guild meta.json + prior.txt (only if we wrote anything).
    if run_result.messages_fetched > 0 or run_result.pinned_fetched > 0:
        day_dir = settings.run.output_root / guild_id / resources.scan_date
        day_dir.mkdir(parents=True, exist_ok=True)

        prior = resolve_prior_date(settings.run.output_root / guild_id, resources.scan_date)
        write_prior(day_dir / "prior.txt", prior)

        finished_at_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = ScanMeta(
            guild_id=guild_id,
            guild_name=guild_name or guild_id,
            scan_date=resources.scan_date,
            scan_started_at=started_at_iso,
            scan_finished_at=finished_at_iso,
            duration_sec=time.monotonic() - started_monotonic,
            counters=ScanCounters(
                channels_scanned=run_result.channels_scanned,
                messages_fetched=run_result.messages_fetched,
                pinned_fetched=run_result.pinned_fetched,
                attachments_downloaded=0,  # A.0.1 doesn't download attachments
                attachments_skipped_mime=0,
            ),
        )
        write_meta(day_dir / "meta.json", meta)

    run_result.finished_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_result.duration_sec = time.monotonic() - started_monotonic
    logger.info(
        "run.summary",
        guild_id=guild_id,
        guild_name=guild_name,
        scan_date=resources.scan_date,
        channels_scanned=run_result.channels_scanned,
        channels_skipped=run_result.channels_skipped,
        channels_failed=run_result.channels_failed,
        messages_fetched=run_result.messages_fetched,
        pinned_fetched=run_result.pinned_fetched,
        duration_sec=round(run_result.duration_sec, 3),
    )
    return run_result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def _run_one_pass_for_burner(
    settings: Settings,
    token: SecretStr,
    *,
    burner_id: str,
    http_overrides: BurnerHttpOverrides | None = None,
    keyring_username: str | None = None,
    scopes_dir: Path | None = None,
    scan_date: str | None = None,
    guild_filter: str | None = None,
) -> list[RunResult]:
    """One scan pass on behalf of ONE burner. Per-burner cookie jar +
    gateway lock are derived from `keyring_username` (defaults to
    `settings.auth.keyring_username`). When the orchestrator is running
    multi-burner, this function is called once per burner with that
    burner's `BurnerConfig.http_overrides` merged into a settings snapshot.

    Filters guilds by scope→burner ownership when scope_map has any
    `burner` field set: a guild owned by a different burner is silently
    skipped (it's that burner's job).
    """
    async with _open_shared_resources(
        settings,
        token,
        scan_date=scan_date,
        scopes_dir=scopes_dir,
        burner_id=burner_id,
        http_overrides=http_overrides,
        keyring_username=keyring_username,
    ) as resources:
        try:
            guilds = await list_my_guilds(resources.client)
        except TokenInvalid as e:
            raise FatalScanError(
                code=3,
                reason="token_invalid",
                message=f"token rejected by Discord: {e}",
            ) from e
        if guild_filter:
            guilds = [g for g in guilds if g.id == guild_filter]

        # M.2 — scope→burner ownership. When a guild is owned by a scope
        # whose `burner` field is set to a DIFFERENT burner_id, skip it
        # silently (that burner will scan it on its own pass). A scope
        # with `burner: null` is shared / unowned: every active burner
        # scans it. Single-burner mode (`burner_id == ""`) treats every
        # scope as shared.
        if burner_id:
            filtered: list[Any] = []
            skipped_count = 0
            for g in guilds:
                owner = resources.scope_map.get_burner_for_guild(g.id)
                if owner is None or owner == burner_id:
                    filtered.append(g)
                else:
                    skipped_count += 1
                    logger.debug(
                        "guild.skip_other_burner",
                        guild_id=g.id,
                        owner=owner,
                        this_burner=burner_id,
                    )
            if skipped_count:
                logger.info(
                    "burner.scope_filter_applied",
                    burner_id=burner_id,
                    skipped=skipped_count,
                    scanning=len(filtered),
                )
            guilds = filtered

        logger.info(
            "run.start",
            burner_id=burner_id,
            guild_count=len(guilds),
            guild_filter=guild_filter,
            scan_date=resources.scan_date,
        )
        results: list[RunResult] = []
        for guild in guilds:
            try:
                results.append(
                    await _scan_one_guild(
                        resources, guild.id, guild.name, resources.settings
                    )
                )
            except TokenInvalid as e:
                raise FatalScanError(
                    code=3,
                    reason="token_invalid_mid_scan",
                    message=f"token rejected mid-scan on guild {guild.id}: {e}",
                ) from e
            except CaptchaAborted as e:
                raise FatalScanError(
                    code=2,
                    reason="captcha_aborted",
                    message=f"captcha challenge on guild {guild.id}: {e}",
                ) from e
            except SSRFViolation as e:
                raise FatalScanError(
                    code=2,
                    reason="ssrf_violation",
                    message=f"SSRF allowlist violation on guild {guild.id}: {e}",
                ) from e
            except GatewayConcurrencyError as e:
                raise FatalScanError(
                    code=1,
                    reason="gateway_lock_contention",
                    message=f"gateway lock held by another process: {e}",
                ) from e
        logger.info(
            "run.finish",
            burner_id=burner_id,
            guilds_scanned=len(results),
            total_messages=sum(r.messages_fetched for r in results),
            total_channels=sum(r.channels_scanned for r in results),
            total_skipped=sum(r.channels_skipped for r in results),
        )
        return results


async def run_one_pass(
    settings: Settings,
    token: SecretStr,
    *,
    guild_filter: str | None = None,
    burner_filter: str | None = None,
    scan_date: str | None = None,
    scopes_dir: Path | None = None,
) -> list[RunResult]:
    """Top-level entry — one full scan pass.

    Single-burner mode (`settings.auth.burners == []`): legacy behaviour,
    use `settings.auth.keyring_username` as the burner_id and the input
    `token`.

    Multi-burner mode (`settings.auth.burners != []`): iterate burners
    sequentially. For each one, load its OWN token via
    `load_token_for_burner` (the input `token` is ignored as a credential —
    multi-burner pools never share tokens). `burner_filter` restricts to
    one burner_id; daemon mode is refused upstream when burners > 1.

    Returns: flat list of RunResult across all burners scanned.
    """
    burners = list(settings.auth.burners)
    if not burners:
        # Legacy single-burner path. The input `token` is the real credential.
        return await _run_one_pass_for_burner(
            settings,
            token,
            burner_id=settings.auth.keyring_username,
            http_overrides=None,
            keyring_username=None,
            scopes_dir=scopes_dir,
            scan_date=scan_date,
            guild_filter=guild_filter,
        )

    # Multi-burner pool. The caller-supplied `token` is ignored — each
    # burner owns its own credential, loaded fresh per iteration.
    if burner_filter:
        burners = [b for b in burners if b.keyring_username == burner_filter]
        if not burners:
            raise FatalScanError(
                code=1,
                reason="burner_filter_no_match",
                message=(
                    f"--burner {burner_filter!r} does not match any "
                    f"auth.burners[].keyring_username"
                ),
            )

    from discord_scanner.session.auth import (  # local import to avoid cycle
        PlaintextKeyringRefused,
        TokenNotFound,
        load_token_for_burner,
    )

    all_results: list[RunResult] = []
    for burner in burners:
        try:
            burner_token, source = load_token_for_burner(
                settings,
                keyring_username=burner.keyring_username,
                keyring_service=burner.keyring_service,
            )
        except (PlaintextKeyringRefused, TokenNotFound) as e:
            raise FatalScanError(
                code=1,
                reason="burner_token_missing",
                message=f"token load failed for burner {burner.keyring_username!r}: {e}",
            ) from e
        logger.info(
            "burner.start",
            burner_id=burner.keyring_username,
            token_source=source.value,
        )
        burner_results = await _run_one_pass_for_burner(
            settings,
            burner_token,
            burner_id=burner.keyring_username,
            http_overrides=burner.http_overrides,
            keyring_username=burner.keyring_username,
            scopes_dir=scopes_dir,
            scan_date=scan_date,
            guild_filter=guild_filter,
        )
        all_results.extend(burner_results)
        logger.info(
            "burner.finish",
            burner_id=burner.keyring_username,
            guilds_scanned=len(burner_results),
        )
    return all_results
