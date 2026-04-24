"""Paginated message fetch.

Traces to:
- seed-spec.md §2.3 (message fetching + pagination + jitter)
- security-model.md §6 SEC-P0-14 (jitter), SEC-P0-15 (429 handling)
- claude-rules.md MUST "Rate-limit + jitter"

Iterates `GET /channels/{channel_id}/messages?limit=100&after={cursor}` until
one of:
- fewer than 100 messages returned (end of channel)
- `max_messages_per_scan` cap reached (default 10_000)
- any call returns ChannelAbort from the retry layer (SEC-P0-15)

Between requests, awaits `sleep_with_jitter(per_channel_delay_sec)` — never
`time.sleep` (CI grep-blocker).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Final

import httpx

from discord_scanner.config import Settings
from discord_scanner.logging_conf import get_logger
from discord_scanner.session.rate_limit import sleep_with_jitter
from discord_scanner.session.retry import ChannelAbort, request_with_retry

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE: Final[int] = 100


async def fetch_channel_messages(
    client: httpx.AsyncClient,
    channel_id: str,
    *,
    settings: Settings,
    after: str | None = None,
    max_messages: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield raw message dicts oldest → newest, paginating on `after=<id>`.

    Args:
        client: the shared AsyncClient (pre-configured with headers + allowlist).
        channel_id: target Discord channel.
        settings: drives page size, jitter, cap.
        after: resume cursor. If None, starts from channel beginning. Pass
            `CursorStore.get(guild_id, channel_id)` as the resume point.
        max_messages: overrides `settings.http.max_messages_per_scan` if given.

    Caller MUST:
        - persist the cursor to `CursorStore` only AFTER the dump file is
          fsync'd (seed-spec §2.7); yielding here does not imply a cursor
          advance.
        - handle `ChannelAbort` from the retry layer to skip-to-next-channel.
    """
    cap = max_messages if max_messages is not None else settings.http.max_messages_per_scan
    emitted = 0
    last_seen: str | None = after

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
            return

        if resp.status_code >= 400:
            logger.warning(
                "messages_http_error",
                channel_id=channel_id,
                status=resp.status_code,
            )
            return

        batch = resp.json()
        if not isinstance(batch, list):
            logger.warning("messages_non_list_response", channel_id=channel_id)
            return
        if not batch:
            logger.debug("messages_end_of_channel", channel_id=channel_id)
            return

        # Discord returns messages NEWEST-first by default. For forward
        # pagination with `after=<id>`, responses come oldest→newest within
        # the page but we still sort defensively by `id` (string id sort ==
        # timestamp sort for snowflakes).
        batch.sort(key=lambda m: m.get("id", ""))

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
