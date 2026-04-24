"""Per-thread message fetch — delegates to `fetch_channel_messages`.

Traces to: seed-spec.md §2.3 (forum / thread fetch).

For a forum channel, Phase 4's `discovery/forums.py` enumerates active +
archived public threads; Phase 6 fetches the messages of each thread using
the same pagination logic as a regular text channel. This module is a thin
orchestration layer over `fetch/messages.py` so tests can assert that
threads go through the identical retry + jitter pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from discord_scanner.config import Settings
from discord_scanner.fetch.messages import fetch_channel_messages
from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)


async def fetch_thread_messages(
    client: httpx.AsyncClient,
    thread_id: str,
    *,
    settings: Settings,
    after: str | None = None,
    max_messages: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield raw thread-message dicts. Threads use the same endpoint shape as
    channels (Discord treats threads as child channels)."""
    logger.debug("fetch_thread_start", thread_id=thread_id, after=after)
    async for msg in fetch_channel_messages(
        client,
        thread_id,
        settings=settings,
        after=after,
        max_messages=max_messages,
    ):
        yield msg
