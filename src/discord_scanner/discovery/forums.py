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

logger = get_logger(__name__)


async def list_active_threads(client: httpx.AsyncClient, channel_id: str) -> list[Channel]:
    """Active threads in a forum or text channel."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/threads/active"
    resp = await client.get(url)
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
    client: httpx.AsyncClient, channel_id: str, *, limit: int = 50
) -> list[Channel]:
    """Archived public threads. `limit` clamps the page size (Discord max 50)."""
    url = (
        f"https://discord.com/api/v10/channels/{channel_id}"
        f"/threads/archived/public?limit={min(limit, 50)}"
    )
    resp = await client.get(url)
    if resp.status_code >= 400:
        logger.warning(
            "list_archived_threads_http_error",
            channel_id=channel_id,
            status=resp.status_code,
        )
        return []
    raw = resp.json()
    threads = raw.get("threads", []) if isinstance(raw, dict) else []
    return _coerce_threads(threads)


def _coerce_threads(raw: list[dict[str, Any]]) -> list[Channel]:
    out: list[Channel] = []
    for entry in raw:
        try:
            out.append(Channel.model_validate(entry))
        except Exception as e:  # noqa: BLE001 — malformed skipped
            logger.warning("thread_record_invalid", err=str(e))
    return out
