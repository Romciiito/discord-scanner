"""Guild enumeration via `/users/@me/guilds`.

Traces to:
- seed-spec.md §2.2 (guild enumeration)
- claude-rules.md MUST-NOT "No auto-joining" + MUST-NOT "No member enumeration"

We list guilds the burner has **already manually joined**. We never POST to
join an invite and we never enumerate members.
"""

from __future__ import annotations

import httpx

from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import Guild

logger = get_logger(__name__)


async def list_my_guilds(client: httpx.AsyncClient) -> list[Guild]:
    """Return every guild the burner has joined. Skips malformed records."""
    url = "https://discord.com/api/v10/users/@me/guilds"
    resp = await client.get(url)
    if resp.status_code >= 400:
        logger.warning("list_guilds_http_error", status=resp.status_code)
        return []
    raw = resp.json()
    if not isinstance(raw, list):
        logger.warning("list_guilds_non_list_response")
        return []
    result: list[Guild] = []
    for entry in raw:
        try:
            result.append(Guild.model_validate(entry))
        except Exception as e:  # noqa: BLE001 — malformed records skipped per claude-rules "Pydantic tolerance"
            logger.warning("guild_record_invalid", err=str(e))
    logger.info("list_guilds_ok", count=len(result))
    return result


def intersect_with_resolved_guild_ids(
    joined_guilds: list[Guild], resolved_guild_ids: set[str]
) -> list[Guild]:
    """Return only guilds in BOTH sets — seed-spec §2.2 defensive intersect.

    The burner must manually join target guilds first; this function filters
    the @me/guilds response against the invite-resolve output.
    """
    return [g for g in joined_guilds if g.id in resolved_guild_ids]
