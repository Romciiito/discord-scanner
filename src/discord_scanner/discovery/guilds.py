"""Guild enumeration via `/users/@me/guilds`.

Traces to:
- seed-spec.md §2.2 (guild enumeration)
- claude-rules.md MUST-NOT "No auto-joining" + MUST-NOT "No member enumeration"

We list guilds the burner has **already manually joined**. We never POST to
join an invite and we never enumerate members.
"""

from __future__ import annotations

import httpx

from discord_scanner.discovery.invite_resolve import TokenInvalid
from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import Guild
from discord_scanner.session.retry import (
    ChannelAbort,
    RetryableResponseError,
    request_with_retry,
)

logger = get_logger(__name__)


async def list_my_guilds(client: httpx.AsyncClient) -> list[Guild]:
    """Return every guild the burner has joined. Skips malformed records.

    Wrapped in `request_with_retry` so 429 Retry-After + 5xx backoff are
    honoured (SEC-P0-15). On `ChannelAbort` (3 consecutive 429s) returns []
    so callers can degrade gracefully.

    Raises `TokenInvalid` on 401 — same contract as
    `discovery.invite_resolve.resolve_invite`. The CLI / orchestrator maps
    this to exit code 3 (detected ban / invalid token). Don't silently
    swallow a 401 here: every subsequent request would also 401 and the
    operator wouldn't get a meaningful exit signal.
    """
    url = "https://discord.com/api/v10/users/@me/guilds"
    try:
        resp = await request_with_retry(client, "GET", url)
    except ChannelAbort:
        logger.warning("list_guilds_channel_abort")
        return []
    except RetryableResponseError as e:
        # Retries exhausted on a 5xx — degrade to empty list rather than
        # bubbling up. Caller logs the http error and continues.
        logger.warning("list_guilds_retries_exhausted", status=e.status)
        return []
    if resp.status_code == 401:
        logger.error("list_guilds_unauthorized")
        raise TokenInvalid("401 Unauthorized on /users/@me/guilds")
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
