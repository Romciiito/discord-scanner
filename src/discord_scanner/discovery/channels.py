"""Channel listing with per-guild include/exclude overrides.

Traces to:
- seed-spec.md §2.2 (channel filter types 0, 5, 15)
- claude-rules.md MUST-NOT "No /guilds/{id}/members enumeration"
- workspace plan §"Part A — A.2 Phase B: Category + Name + Glob Selectors"

v2 adds:
- Category resolution: human-readable category names (type-4 channels) →
  match all child channels whose `parent_id` matches.
- fnmatch glob support for channel names ("*-share", "showcase-*").
- Resolution logging: every selector entry → resolved IDs, one log line per
  invocation. Operator can verify "🎨 art / showcase → 1234567890".
- fail-open semantics: missing category/glob WARN+skip, never crash the scan.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

import httpx

from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import SCANNABLE_CHANNEL_TYPES, Channel
from discord_scanner.session.retry import (
    ChannelAbort,
    RetryableResponseError,
    request_with_retry,
)

logger = get_logger(__name__)

# Discord channel type for categories (parents).
_CATEGORY_CHANNEL_TYPE: int = 4


async def list_channels(client: httpx.AsyncClient, guild_id: str) -> list[Channel]:
    """Return every channel in a guild — the caller applies type + include/exclude filters.

    Wrapped in `request_with_retry` for SEC-P0-15 (Retry-After + 5xx backoff).
    """
    url = f"https://discord.com/api/v10/guilds/{guild_id}/channels"
    try:
        resp = await request_with_retry(client, "GET", url)
    except ChannelAbort:
        logger.warning("list_channels_channel_abort", guild_id=guild_id)
        return []
    except RetryableResponseError as e:
        logger.warning("list_channels_retries_exhausted", guild_id=guild_id, status=e.status)
        return []
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


@dataclass(frozen=True)
class GuildSelector:
    """Operator-declared per-guild channel selector.

    All four lists are independent and ANDed in scope:
    - `categories`: parent category names (type-4); selects ALL their children.
    - `channels`: explicit channel names or fnmatch globs (within categories
      if `categories` is set; across all categories otherwise).
    - `exclude_channels`: globs that filter the result set after include.

    `fail_open: true` (default) means missing names/globs WARN and skip;
    `false` raises ValueError so operator notices the typo.
    """

    categories: list[str] | None = None
    channels: list[str] | None = None
    exclude_channels: list[str] | None = None
    fail_open: bool = True


def _norm(s: str | None) -> str:
    """Normalise channel names for matching — strip+lowercase. Emoji preserved."""
    return (s or "").strip().lower()


def _resolve_categories(
    channels: list[Channel],
    category_specs: list[str],
    *,
    fail_open: bool,
) -> set[str]:
    """Return parent_ids of categories matching any spec (case-insensitive).

    Logs each resolution. WARN+skip on missing spec when `fail_open=True`.
    """
    parent_ids: set[str] = set()
    by_name: dict[str, str] = {
        _norm(ch.name): ch.id for ch in channels if ch.type == _CATEGORY_CHANNEL_TYPE
    }
    for spec in category_specs:
        key = _norm(spec)
        if key in by_name:
            parent_ids.add(by_name[key])
            logger.info("selector_category_resolved", spec=spec, category_id=by_name[key])
        else:
            msg_kwargs = {"spec": spec, "available": sorted(by_name.keys())[:10]}
            if fail_open:
                logger.warning("selector_category_not_found", **msg_kwargs)
            else:
                raise ValueError(
                    f"selector category {spec!r} not found in guild; available: "
                    f"{sorted(by_name.keys())[:10]}"
                )
    return parent_ids


def _name_matches_any(name: str | None, patterns: list[str]) -> bool:
    """fnmatch-glob against a list of patterns. Case-insensitive."""
    if not name or not patterns:
        return False
    norm = _norm(name)
    return any(fnmatch.fnmatchcase(norm, _norm(p)) for p in patterns)


def filter_channels(
    channels: list[Channel],
    *,
    # Legacy v1 inputs (still supported, unchanged behaviour when selector is None)
    include_channels: list[str] | None = None,
    exclude_channels: list[str] | None = None,
    # v2 selector — when provided, replaces v1 path entirely.
    selector: GuildSelector | None = None,
) -> list[Channel]:
    """Apply SCANNABLE_CHANNEL_TYPES + optional per-guild include/exclude lists.

    v1 path (selector=None): preserve existing behaviour exactly.

    v2 path (selector set): resolve categories by name, match channel names
    via fnmatch globs, apply exclude_channels globs over the result. Logs
    every resolution; WARN+skip on missing names when fail_open is True.

    Precedence (v1): include overrides type filter; exclude takes precedence.
    Precedence (v2): categories scope the candidate pool; channel-globs further
    narrow within (or, if categories empty, across all scannable types);
    exclude-globs prune; SCANNABLE_CHANNEL_TYPES still applies as a final guard.
    """
    if selector is None:
        # v1 fast path — unchanged.
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

    # v2 path
    candidate: list[Channel] = list(channels)

    # 1. Categories filter — if specified, narrow to children of resolved parents.
    if selector.categories:
        parent_ids = _resolve_categories(
            candidate, selector.categories, fail_open=selector.fail_open
        )
        candidate = [ch for ch in candidate if ch.parent_id in parent_ids]

    # 2. Apply channel-name globs (or names) to candidate pool.
    channel_specs = selector.channels or []
    matched: list[Channel] = []
    if channel_specs:
        for ch in candidate:
            if ch.type not in SCANNABLE_CHANNEL_TYPES:
                continue
            if _name_matches_any(ch.name, channel_specs):
                matched.append(ch)
        # Log unresolved channel-name specs (no channel matched at all).
        unresolved = [
            spec
            for spec in channel_specs
            if not any(
                _name_matches_any(ch.name, [spec])
                for ch in candidate
                if ch.type in SCANNABLE_CHANNEL_TYPES
            )
        ]
        for spec in unresolved:
            if selector.fail_open:
                logger.warning("selector_channel_glob_no_match", spec=spec)
            else:
                raise ValueError(f"selector channel glob {spec!r} matched no channel")
    else:
        # No channel specs → all scannable children of selected categories
        # (or all scannable channels if no categories specified).
        matched = [ch for ch in candidate if ch.type in SCANNABLE_CHANNEL_TYPES]

    # 3. exclude_channels — globs against name OR raw id.
    if selector.exclude_channels:
        excluded: list[Channel] = []
        for ch in matched:
            if ch.id in selector.exclude_channels:
                continue
            if _name_matches_any(ch.name, selector.exclude_channels):
                continue
            excluded.append(ch)
        matched = excluded

    # 4. Single resolution log line for the operator.
    logger.info(
        "selector_resolved",
        categories=selector.categories or [],
        channels=channel_specs,
        exclude=selector.exclude_channels or [],
        resolved_count=len(matched),
        resolved_names=[ch.name for ch in matched if ch.name][:20],
    )
    return matched
