"""Channel listing with per-guild include/exclude overrides.

Traces to:
- seed-spec.md §2.2 (channel filter types 0, 5, 15)
- claude-rules.md MUST-NOT "No /guilds/{id}/members enumeration"
"""

from __future__ import annotations

import httpx

from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import SCANNABLE_CHANNEL_TYPES, Channel

logger = get_logger(__name__)


async def list_channels(client: httpx.AsyncClient, guild_id: str) -> list[Channel]:
    """Return every channel in a guild — the caller applies type + include/exclude filters."""
    url = f"https://discord.com/api/v10/guilds/{guild_id}/channels"
    resp = await client.get(url)
    if resp.status_code >= 400:
        logger.warning("list_channels_http_error", guild_id=guild_id, status=resp.status_code)
        return []
    raw = resp.json()
    if not isinstance(raw, list):
        return []
    channels: list[Channel] = []
    for entry in raw:
        try:
            channels.append(Channel.model_validate(entry))
        except Exception as e:  # noqa: BLE001 — malformed skipped
            logger.warning("channel_record_invalid", err=str(e))
    logger.info("list_channels_ok", guild_id=guild_id, count=len(channels))
    return channels


def filter_channels(
    channels: list[Channel],
    *,
    include_channels: list[str] | None = None,
    exclude_channels: list[str] | None = None,
) -> list[Channel]:
    """Apply SCANNABLE_CHANNEL_TYPES + optional per-guild include/exclude lists.

    Precedence: include overrides type filter (explicit whitelist wins);
    exclude takes precedence over include.
    """
    include_set = set(include_channels or [])
    exclude_set = set(exclude_channels or [])

    result: list[Channel] = []
    for ch in channels:
        if ch.id in exclude_set or (ch.name and ch.name in exclude_set):
            continue
        if include_set:
            if ch.id in include_set or (ch.name and ch.name in include_set):
                result.append(ch)
            continue
        if ch.type in SCANNABLE_CHANNEL_TYPES:
            result.append(ch)
    return result
