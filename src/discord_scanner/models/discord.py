"""Pydantic v2 models for Discord REST entities (Invite / Guild / Channel / Role).

Traces to:
- seed-spec.md §7 (data contracts), §2.2 (guild enumeration)
- security-model.md §6 SEC-P0-29 (no write endpoints or member enumeration)
- claude-rules.md MUST "Pydantic tolerance" (extra='allow' on upstream models)

Every model sets `extra='allow'` so Discord schema drift (new fields) doesn't
crash the scan. Validation errors on a single record log a WARNING + skip that
record; the run continues.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# Invite code validation regex per seed-spec §7 / REQ-F-004.
# Codes that don't match are skipped pre-network (never leave the tool).
INVITE_CODE_REGEX: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9-]{4,20}$")


def is_valid_invite_code(code: str) -> bool:
    """Cheap pre-network validation — reject obvious junk before it touches Discord."""
    return bool(INVITE_CODE_REGEX.match(code))


class Guild(BaseModel):
    """Discord guild (subset — only fields consumed by Stage 2)."""

    model_config = ConfigDict(extra="allow")
    id: str
    name: str


class Channel(BaseModel):
    """Discord channel.

    `type` per Discord reference:
    - 0 GUILD_TEXT, 2 GUILD_VOICE, 4 GUILD_CATEGORY, 5 GUILD_ANNOUNCEMENT,
    - 10 ANNOUNCEMENT_THREAD, 11 PUBLIC_THREAD, 12 PRIVATE_THREAD,
    - 13 GUILD_STAGE_VOICE, 15 GUILD_FORUM, 16 GUILD_MEDIA
    Stage 2 scans ONLY types {0, 5, 15} plus threads (REQ-F-007).
    """

    model_config = ConfigDict(extra="allow")
    id: str
    type: int
    name: str | None = None
    guild_id: str | None = None
    parent_id: str | None = None


# Channel types we actually scan.
SCANNABLE_CHANNEL_TYPES: Final[frozenset[int]] = frozenset({0, 5, 15})


class Role(BaseModel):
    """Discord role — name-only use for mention resolution (no member enumeration)."""

    model_config = ConfigDict(extra="allow")
    id: str
    name: str


class ResolvedInvite(BaseModel):
    """Flat struct consumed by `resolve` + `scan` orchestrator.

    Only the subset we need. `guild` may be missing on expired invites.
    """

    model_config = ConfigDict(extra="allow")
    code: str
    guild_id: str | None = None
    guild_name: str | None = None
    expires_at: str | None = None
    approximate_member_count: int | None = None


class EnrichedInvite(BaseModel):
    """Entry in `invites.enriched.json` produced by Stage 1 (civit-hf-scanner)."""

    model_config = ConfigDict(extra="allow")
    invite_code: str
    invite_url: str | None = None
    score_pct: float | None = Field(default=None, ge=0, le=100)
    intent: str | None = None
    is_nsfw_linked: bool | None = None
