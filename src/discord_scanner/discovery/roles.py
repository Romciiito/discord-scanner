"""Role enumeration — names only for mention resolution.

Traces to:
- seed-spec.md §2.2 (no member list calls)
- claude-rules.md MUST-NOT "No /guilds/{id}/members enumeration"
"""

from __future__ import annotations

import httpx

from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import Role

logger = get_logger(__name__)


async def list_roles(client: httpx.AsyncClient, guild_id: str) -> list[Role]:
    """Return every role in a guild. One API call, no member enumeration."""
    url = f"https://discord.com/api/v10/guilds/{guild_id}/roles"
    resp = await client.get(url)
    if resp.status_code >= 400:
        logger.warning("list_roles_http_error", guild_id=guild_id, status=resp.status_code)
        return []
    raw = resp.json()
    if not isinstance(raw, list):
        return []
    roles: list[Role] = []
    for entry in raw:
        try:
            roles.append(Role.model_validate(entry))
        except Exception as e:  # noqa: BLE001 — malformed skipped
            logger.warning("role_record_invalid", err=str(e))
    logger.info("list_roles_ok", guild_id=guild_id, count=len(roles))
    return roles
