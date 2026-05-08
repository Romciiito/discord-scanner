"""Paginated message fetch.

Traces to:
- seed-spec.md §2.3 (message fetching + pagination + jitter)
- security-model.md §6 SEC-P0-14 (jitter), SEC-P0-15 (429 handling)
- claude-rules.md MUST "Rate-limit + jitter"
- workspace plan §"Part A — A.3 Phase C: Backward Backfill"; AGENT-TEAM-WORKPLAN
  §A.3 (channel-start polish from prior scanner)

Iterates `GET /channels/{channel_id}/messages?limit=100&after={cursor}` until
one of:
- fewer than 100 messages returned (end of channel)
- `max_messages_per_scan` cap reached (default 10_000)
- any call returns ChannelAbort from the retry layer (SEC-P0-15)

Between requests, awaits `sleep_with_jitter(per_channel_delay_sec)` — never
`time.sleep` (CI grep-blocker).
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from discord_scanner.config import Settings
from discord_scanner.logging_conf import get_logger
from discord_scanner.session.rate_limit import sleep_with_jitter
from discord_scanner.session.retry import ChannelAbort, request_with_retry

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE: Final[int] = 100


def _snowflake_key(msg: dict[str, Any]) -> int:
    """Sort key: integer snowflake id, or -1 for malformed records (sorted first)."""
    mid = msg.get("id")
    if isinstance(mid, str) and mid.isdigit():
        return int(mid)
    return -1


@dataclass
class ForwardTermination:
    """Out-param mirror of `BackfillTermination` for the forward (after=)
    fetch direction. Lets the orchestrator distinguish "channel returned 0
    new messages cleanly" from "channel was aborted mid-fetch".

    Default `aborted=False`; iterator sets True on `ChannelAbort` /
    HTTP error / malformed response. Existing callers that pass no
    `result=` argument get the v1 behaviour (errors swallowed silently)."""

    aborted: bool = False
    abort_reason: str | None = None
    emitted: int = 0


class BackfillStop(str, enum.Enum):
    """Why a backfill iteration stopped — operator-meaningful taxonomy.

    The caller / orchestrator inspects `BackfillTermination.reason` to decide
    whether to call `CursorStore.mark_backfilled()` (only on `CHANNEL_START`).

    - `CHANNEL_START`   Discord returned an empty page. Confirmed first message
                        reached. SAFE to mark_backfilled.
    - `STUCK_CURSOR`    Discord returned a page whose oldest id equals the
                        `before=` cursor we sent (or contains it). API is
                        telling us "this IS the oldest accessible". SAFE to
                        mark_backfilled — there's nothing further to fetch.
    - `SHORT_PAGE`      Discord returned <100 msgs but >0. Could be a real
                        gap (deleted messages, permission edge), NOT confirmed
                        channel start. DO NOT mark_backfilled; next scan
                        retries from the new oldest cursor.
    - `CAP_REACHED`     Per-scan message budget exhausted. Resume next scan.
    - `ABORTED`         ChannelAbort or HTTP error mid-iteration. Resume next scan.
    - `MALFORMED`       Discord returned a non-list / id-less batch. Bail
                        defensively; resume next scan.
    """

    CHANNEL_START = "channel_start"
    STUCK_CURSOR = "stuck_cursor"
    SHORT_PAGE = "short_page"
    CAP_REACHED = "cap_reached"
    ABORTED = "aborted"
    MALFORMED = "malformed"


@dataclass
class BackfillTermination:
    """Out-param the caller passes to `fetch_channel_messages_backward` to
    learn how iteration ended. Mutated by the iterator before it returns.

    `channel_start_confirmed = True` iff `reason in {CHANNEL_START, STUCK_CURSOR}`.
    Only `mark_backfilled()` when this is True.
    """

    reason: BackfillStop = BackfillStop.CAP_REACHED
    channel_start_confirmed: bool = False
    emitted: int = 0
    last_oldest_id: str | None = None
    pages_fetched: int = 0
    notes: list[str] = field(default_factory=list)


async def fetch_channel_messages(
    client: httpx.AsyncClient,
    channel_id: str,
    *,
    settings: Settings,
    after: str | None = None,
    max_messages: int | None = None,
    result: ForwardTermination | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield raw message dicts oldest → newest, paginating on `after=<id>`.

    Args:
        client: the shared AsyncClient (pre-configured with headers + allowlist).
        channel_id: target Discord channel.
        settings: drives page size, jitter, cap.
        after: resume cursor. If None, starts from channel beginning. Pass
            `CursorStore.get(guild_id, channel_id)` as the resume point.
        max_messages: overrides `settings.http.max_messages_per_scan` if given.
        result: optional out-param. If passed, set to `aborted=True` on
            `ChannelAbort` / HTTP error / malformed response so the caller
            can distinguish "0 new messages cleanly" from "fetch was
            aborted mid-pagination" (and thus skip subsequent fetches like
            pinned/threads on the same channel). Default `None` keeps v1
            behaviour — errors swallowed silently.

    Caller MUST:
        - persist the cursor to `CursorStore` only AFTER the dump file is
          fsync'd (seed-spec §2.7); yielding here does not imply a cursor
          advance.
        - if `result` is None, callers cannot distinguish abort from clean
          empty; production callers SHOULD pass a `ForwardTermination`.
    """
    cap = max_messages if max_messages is not None else settings.http.max_messages_per_scan
    emitted = 0
    last_seen: str | None = after

    def _set_abort(reason: str) -> None:
        if result is not None:
            result.aborted = True
            result.abort_reason = reason
            result.emitted = emitted

    while emitted < cap:
        params = {"limit": str(DEFAULT_PAGE_SIZE)}
        if last_seen is not None:
            params["after"] = last_seen
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"

        try:
            resp = await request_with_retry(
                client,
                "GET",
                url,
                params=params,
                attempts=settings.retry.attempts,
                backoff_initial_sec=settings.retry.backoff_initial_sec,
                backoff_max_sec=settings.retry.backoff_max_sec,
            )
        except ChannelAbort:
            logger.warning("channel_abort", channel_id=channel_id, emitted=emitted)
            _set_abort("channel_abort")
            return

        if resp.status_code >= 400:
            logger.warning(
                "messages_http_error",
                channel_id=channel_id,
                status=resp.status_code,
            )
            _set_abort(f"http_{resp.status_code}")
            return

        batch = resp.json()
        if not isinstance(batch, list):
            logger.warning("messages_non_list_response", channel_id=channel_id)
            _set_abort("malformed_response")
            return
        if not batch:
            logger.debug("messages_end_of_channel", channel_id=channel_id)
            return

        # Discord returns messages NEWEST-first by default. Sort defensively
        # by NUMERIC snowflake value so `last_seen` is always the newest id
        # in the page. String-sort would break on mixed 17-/18-digit snowflakes
        # (pre-2015 vs post-2015 messages); numeric sort is always correct.
        batch.sort(key=_snowflake_key)

        for msg in batch:
            if emitted >= cap:
                return
            yield msg
            mid = msg.get("id")
            if isinstance(mid, str):
                last_seen = mid
            emitted += 1

        if len(batch) < DEFAULT_PAGE_SIZE:
            logger.debug("messages_short_page_end", channel_id=channel_id)
            return

        # Jitter between pages — SEC-P0-14.
        await sleep_with_jitter(settings.http.per_channel_delay_sec)


async def fetch_channel_messages_backward(
    client: httpx.AsyncClient,
    channel_id: str,
    *,
    settings: Settings,
    before: str | None = None,
    max_messages: int | None = None,
    result: BackfillTermination | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield raw message dicts NEWEST → OLDEST, paginating on `before=<id>`.

    Mirror of `fetch_channel_messages` for the backward (backfill) direction.
    Yielded messages are sorted oldest-first WITHIN each page so the caller's
    downstream sort (`(channel_id, ts, msg_id)` in `dump/sort.py`) stays
    deterministic — but the OUTER iteration walks page-by-page from newest-
    known toward oldest.

    Termination is reported via the optional `result` out-param. The caller
    inspects `result.reason` after iteration completes to decide whether to
    call `CursorStore.mark_backfilled()`. Only `CHANNEL_START` and
    `STUCK_CURSOR` set `channel_start_confirmed = True`. A `SHORT_PAGE`
    termination does NOT confirm channel start (could be a deletion gap)
    and the next scan should resume from the new oldest cursor.

    The `STUCK_CURSOR` case handles a Discord quirk: when the operator's
    `before=<oldest>` cursor IS the oldest message, Discord may return a
    page that contains that same id (or returns an empty page — both are
    acceptable channel-start signals). Detecting this prevents an infinite
    loop where the cursor never advances.

    Args:
        before: cursor — start from messages OLDER than this snowflake. Pass
            `CursorFrontier.oldest_seen_message_id` (current oldest known).
            None means start from the very newest (Discord defaults to that
            when `before=` is absent).
        result: optional out-param mutated by the iterator. If None, behaves
            exactly as v1 (no termination metadata exposed).
    """
    cap = max_messages if max_messages is not None else settings.http.max_messages_per_scan
    emitted = 0
    cursor_id: str | None = before
    pages_fetched = 0
    last_oldest_id: str | None = before

    def _set_result(reason: BackfillStop, *, confirmed: bool, note: str = "") -> None:
        if result is None:
            return
        result.reason = reason
        result.channel_start_confirmed = confirmed
        result.emitted = emitted
        result.last_oldest_id = last_oldest_id
        result.pages_fetched = pages_fetched
        if note:
            result.notes.append(note)

    while emitted < cap:
        params = {"limit": str(DEFAULT_PAGE_SIZE)}
        if cursor_id is not None:
            params["before"] = cursor_id
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"

        try:
            resp = await request_with_retry(
                client,
                "GET",
                url,
                params=params,
                attempts=settings.retry.attempts,
                backoff_initial_sec=settings.retry.backoff_initial_sec,
                backoff_max_sec=settings.retry.backoff_max_sec,
            )
        except ChannelAbort:
            logger.warning("backfill_channel_abort", channel_id=channel_id, emitted=emitted)
            _set_result(BackfillStop.ABORTED, confirmed=False, note="ChannelAbort")
            return

        pages_fetched += 1

        if resp.status_code >= 400:
            logger.warning(
                "backfill_messages_http_error",
                channel_id=channel_id,
                status=resp.status_code,
            )
            _set_result(
                BackfillStop.ABORTED,
                confirmed=False,
                note=f"http_{resp.status_code}",
            )
            return

        batch = resp.json()
        if not isinstance(batch, list):
            logger.warning("backfill_messages_non_list_response", channel_id=channel_id)
            _set_result(BackfillStop.MALFORMED, confirmed=False, note="non_list_response")
            return
        if not batch:
            # Empty page = confirmed channel start. Caller can mark_backfilled.
            logger.info(
                "backfill_messages_channel_start",
                channel_id=channel_id,
                emitted=emitted,
                pages_fetched=pages_fetched,
            )
            _set_result(BackfillStop.CHANNEL_START, confirmed=True)
            return

        # Discord returns newest-first. Sort oldest-first WITHIN each page
        # (deterministic for downstream sort) and track the OLDEST id as the
        # next `before=` cursor so we walk further back.
        batch.sort(key=_snowflake_key)
        oldest_in_batch = batch[0].get("id") if isinstance(batch[0].get("id"), str) else None

        # Stuck-cursor detection (Discord quirk): if the API returns a page
        # whose oldest id equals the `before=` cursor we sent — or, more
        # commonly, the new oldest-in-batch is NOT strictly less than the
        # current cursor — we cannot make further progress. Treat as a
        # confirmed channel start so the orchestrator marks_backfilled and
        # subsequent scans skip this channel's backfill.
        if oldest_in_batch is None:
            _set_result(BackfillStop.MALFORMED, confirmed=False, note="oldest_id_not_str")
            return
        if cursor_id is not None and not _strictly_older(oldest_in_batch, cursor_id):
            # Yield messages in this page first (caller still wants the data),
            # then terminate as channel-start.
            for msg in batch:
                if emitted >= cap:
                    last_oldest_id = oldest_in_batch
                    _set_result(BackfillStop.CAP_REACHED, confirmed=False)
                    return
                yield msg
                emitted += 1
            last_oldest_id = oldest_in_batch
            logger.info(
                "backfill_messages_stuck_cursor",
                channel_id=channel_id,
                cursor_id=cursor_id,
                oldest_in_batch=oldest_in_batch,
                pages_fetched=pages_fetched,
            )
            _set_result(BackfillStop.STUCK_CURSOR, confirmed=True)
            return

        for msg in batch:
            if emitted >= cap:
                last_oldest_id = oldest_in_batch
                _set_result(BackfillStop.CAP_REACHED, confirmed=False)
                return
            yield msg
            emitted += 1

        last_oldest_id = oldest_in_batch
        cursor_id = oldest_in_batch

        if len(batch) < DEFAULT_PAGE_SIZE:
            # Short page = end of THIS request, NOT confirmed channel start.
            # Don't mark_backfilled — let next scan retry from the new oldest.
            # (A real channel start would be the empty-page branch above; a
            # short page often signals deletion gaps or permission slices.)
            logger.info(
                "backfill_messages_short_page_end",
                channel_id=channel_id,
                emitted=emitted,
                pages_fetched=pages_fetched,
                page_size=len(batch),
            )
            _set_result(BackfillStop.SHORT_PAGE, confirmed=False)
            return

        # Jitter between pages — SEC-P0-14.
        await sleep_with_jitter(settings.http.per_channel_delay_sec)

    # Exited loop because emitted >= cap.
    _set_result(BackfillStop.CAP_REACHED, confirmed=False)


def _strictly_older(candidate: str, cursor: str) -> bool:
    """Return True iff `candidate` is numerically (snowflake-)older than
    `cursor`. Used in stuck-cursor detection above."""
    if not candidate or not cursor:
        return False
    if candidate.isdigit() and cursor.isdigit():
        return int(candidate) < int(cursor)
    # Malformed — fall back to lex comparison rather than crash.
    return candidate < cursor
