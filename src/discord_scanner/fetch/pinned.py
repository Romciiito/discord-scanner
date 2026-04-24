"""Pinned-message fetch — `GET /channels/{channel_id}/pins`.

Traces to: seed-spec.md §2.3 (pinned messages).

Pinned messages are a separate endpoint that returns a flat list (no
pagination — Discord caps at 50 per channel). Emits each raw dict so the
caller can apply the shared `Message.from_api` coercer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from discord_scanner.config import Settings
from discord_scanner.logging_conf import get_logger
from discord_scanner.session.retry import ChannelAbort, request_with_retry

logger = get_logger(__name__)


async def fetch_channel_pinned(
    client: httpx.AsyncClient,
    channel_id: str,
    *,
    settings: Settings,
) -> AsyncIterator[dict[str, Any]]:
    """Yield pinned messages for a channel. No pagination (Discord max 50)."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/pins"
    try:
        resp = await request_with_retry(
            client,
            "GET",
            url,
            attempts=settings.retry.attempts,
            backoff_initial_sec=settings.retry.backoff_initial_sec,
            backoff_max_sec=settings.retry.backoff_max_sec,
        )
    except ChannelAbort:
        logger.warning("pinned_channel_abort", channel_id=channel_id)
        return
    if resp.status_code >= 400:
        logger.warning("pinned_http_error", channel_id=channel_id, status=resp.status_code)
        return
    batch = resp.json()
    if not isinstance(batch, list):
        logger.warning("pinned_non_list_response", channel_id=channel_id)
        return
    # Deterministic order: oldest → newest.
    batch.sort(key=lambda m: m.get("id", ""))
    for msg in batch:
        yield msg
