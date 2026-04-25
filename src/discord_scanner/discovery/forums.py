"""Forum / thread enumeration — scaffolding for Phase 6 fetch.

Traces to:
- seed-spec.md §2.3 (forum message fetching)

This module provides the listing API; Phase 6 will wire per-thread message
pagination. For Phase 4 we only need to enumerate active + archived public
threads of a forum channel so the scan plan is complete.
"""

from __future__ import annotations

from typing import Any

import httpx

from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import Channel
from discord_scanner.session.retry import (
    ChannelAbort,
    RetryableResponseError,
    request_with_retry,
)

logger = get_logger(__name__)


async def list_active_threads(client: httpx.AsyncClient, channel_id: str) -> list[Channel]:
    """Active threads in a forum or text channel.

    Wrapped in `request_with_retry` for SEC-P0-15.
    """
    url = f"https://discord.com/api/v10/channels/{channel_id}/threads/active"
    try:
        resp = await request_with_retry(client, "GET", url)
    except ChannelAbort:
        logger.warning("list_active_threads_channel_abort", channel_id=channel_id)
        return []
    except RetryableResponseError as e:
        logger.warning(
            "list_active_threads_retries_exhausted", channel_id=channel_id, status=e.status
        )
        return []
    if resp.status_code >= 400:
        logger.warning(
            "list_active_threads_http_error",
            channel_id=channel_id,
            status=resp.status_code,
        )
        return []
    raw = resp.json()
    threads = raw.get("threads", []) if isinstance(raw, dict) else []
    return _coerce_threads(threads)


async def list_archived_public_threads(
    client: httpx.AsyncClient,
    channel_id: str,
    *,
    limit: int = 50,
    max_pages: int = 20,
) -> list[Channel]:
    """Archived public threads, paginated until `has_more` is false.

    `limit` clamps the per-call page size (Discord caps it at 50). The
    `before` cursor is the archive timestamp of the last thread in the
    previous page, per Discord's archived-threads API. `max_pages` is a
    safety stop to avoid runaway loops on a misbehaving guild.
    """
    page = min(limit, 50)
    out: list[Channel] = []
    before: str | None = None
    for _ in range(max_pages):
        url = (
            f"https://discord.com/api/v10/channels/{channel_id}"
            f"/threads/archived/public?limit={page}"
        )
        if before:
            url += f"&before={before}"
        try:
            resp = await request_with_retry(client, "GET", url)
        except ChannelAbort:
            logger.warning("list_archived_threads_channel_abort", channel_id=channel_id)
            return out
        except RetryableResponseError as e:
            logger.warning(
                "list_archived_threads_retries_exhausted",
                channel_id=channel_id,
                status=e.status,
            )
            return out
        if resp.status_code >= 400:
            logger.warning(
                "list_archived_threads_http_error",
                channel_id=channel_id,
                status=resp.status_code,
            )
            return out
        raw = resp.json()
        if not isinstance(raw, dict):
            return out
        threads = _coerce_threads(raw.get("threads", []))
        out.extend(threads)
        if not raw.get("has_more") or not threads:
            return out
        # Discord paginates archived threads by archive_timestamp; pull from
        # the last thread's metadata if available, else fall back to id.
        last = threads[-1]
        meta = getattr(last, "thread_metadata", None) or {}
        if isinstance(meta, dict):
            ts = meta.get("archive_timestamp")
            before = str(ts) if ts else last.id
        else:
            before = last.id
    logger.warning(
        "list_archived_threads_max_pages_reached", channel_id=channel_id, max_pages=max_pages
    )
    return out


def _coerce_threads(raw: list[dict[str, Any]]) -> list[Channel]:
    out: list[Channel] = []
    for entry in raw:
        try:
            out.append(Channel.model_validate(entry))
        except Exception as e:  # noqa: BLE001 — malformed skipped
            logger.warning("thread_record_invalid", err=str(e))
    return out
