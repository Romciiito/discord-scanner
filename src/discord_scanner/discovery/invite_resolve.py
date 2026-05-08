"""Resolve invite codes to guild IDs via `/api/v10/invites/{code}`.

Traces to:
- seed-spec.md §2.1 (invite resolve)
- security-model.md §6 SEC-P0-04 (invite-code redaction in logs)
- claude-rules.md MUST "Pydantic tolerance"

The ONE Discord API call that Stage 2 makes to invite endpoints is
`GET /api/v10/invites/{code}?with_counts=true&with_expiration=true`. This is
resolve-only — we never POST to /invites/{code} (that would join the guild).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from discord_scanner.discovery.invite_cache import InviteCache
from discord_scanner.logging_conf import get_logger, redact_invite_code
from discord_scanner.models.discord import ResolvedInvite, is_valid_invite_code
from discord_scanner.session.retry import ChannelAbort, request_with_retry

logger = get_logger(__name__)


class TokenInvalid(RuntimeError):
    """Raised when Discord returns 401 on invite resolve — caller maps to CLI exit 3.

    SEC-P0 adjacent: 401 on any Discord call is a ban / invalid-token signal.
    Unlike 403/404 (which just mean this invite is unavailable), 401 propagates
    upward so the operator sees the distinction.
    """


async def resolve_invite(
    client: httpx.AsyncClient,
    code: str,
    *,
    cache: InviteCache | None = None,
) -> ResolvedInvite | None:
    """Resolve one invite code. Returns None on 404 / 403 / invalid-format.

    Cache hit within 7 days bypasses the network. On 5xx / transport error the
    caller's retry wrapper (`session/retry.py`) will have already handled retries;
    we let exceptions propagate.
    """
    if not is_valid_invite_code(code):
        logger.warning("invite_code_invalid_format", code=redact_invite_code(code))
        return None

    if cache is not None:
        hit = cache.get(code)
        if hit is not None:
            logger.debug("invite_cache_hit", code=redact_invite_code(code))
            return hit

    url = f"https://discord.com/api/v10/invites/{code}?with_counts=true&with_expiration=true"
    try:
        resp = await request_with_retry(client, "GET", url)
    except ChannelAbort:
        logger.warning("invite_resolve_channel_abort", code=redact_invite_code(code))
        return None
    if resp.status_code == 401:
        # 401 = token invalid / banned. Propagate so the CLI exits 3 (detected-ban)
        # rather than silently skipping every invite and exiting 0.
        logger.error("invite_resolve_unauthorized", code=redact_invite_code(code))
        raise TokenInvalid(f"401 Unauthorized on invite resolve (code={redact_invite_code(code)})")
    if resp.status_code == 404:
        logger.info("invite_not_found", code=redact_invite_code(code))
        return None
    if resp.status_code == 403:
        logger.info("invite_forbidden", code=redact_invite_code(code))
        return None
    if resp.status_code >= 400:
        logger.warning(
            "invite_resolve_http_error",
            code=redact_invite_code(code),
            status=resp.status_code,
        )
        return None

    body = resp.json()
    guild = body.get("guild") or {}
    resolved = ResolvedInvite(
        code=code,
        guild_id=guild.get("id"),
        guild_name=guild.get("name"),
        expires_at=body.get("expires_at"),
        approximate_member_count=body.get("approximate_member_count"),
    )

    if cache is not None:
        cache.put(resolved)
    logger.info(
        "invite_resolved",
        code=redact_invite_code(code),
        guild_id=resolved.guild_id,
    )
    return resolved


def load_enriched_invites(path: Path, *, max_bytes: int = 1_000_000) -> list[dict[str, Any]]:
    """Load Stage 1's invite feed from disk.

    Accepts BOTH shapes emitted by Stage 1 (civit-hf-scanner / `discord-finder`):

    - `invites.json`              — JSON root is a `list[ScoredInvite]`
    - `invites.enriched.json`     — JSON root is `{"metadata": {...}, "invites": [...]}`
                                     where each invite carries the bare
                                     `ScoredInvite` fields PLUS Stage 1.5
                                     `intent`, `aesthetic_tags`, `confidence`,
                                     `summary`, and Claude metadata.

    Size-capped at 1 MB by default to prevent a malicious or corrupted file
    from blowing the event loop on load. Returns raw dicts; the caller
    applies `select_invite_codes()` if it wants intent/confidence filtering.
    """
    if not path.is_file():
        logger.warning("invites_input_missing", path=str(path))
        return []
    size = path.stat().st_size
    if size > max_bytes:
        logger.error("invites_input_too_large", path=str(path), size=size, cap=max_bytes)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.error("invites_input_parse_failed", path=str(path), err=str(e))
        return []
    # Stage 1.5 enriched form: {"metadata": {...}, "invites": [...]}
    if isinstance(data, dict):
        invites = data.get("invites")
        if isinstance(invites, list):
            logger.info(
                "invites_input_enriched_form",
                path=str(path),
                count=len(invites),
                metadata_keys=sorted((data.get("metadata") or {}).keys()),
            )
            return invites
        logger.error("invites_input_dict_missing_invites", path=str(path))
        return []
    # Bare ScoredInvite list form
    if isinstance(data, list):
        return data
    logger.error("invites_input_not_a_list_or_dict", path=str(path))
    return []


def resolve_invites_input_path(invites_input: Path) -> Path:
    """Prefer `invites.enriched.json` over `invites.json` if both exist.

    Stage 1.5 (civit-hf-scanner `discord-finder enrich`) writes the enriched
    artefact alongside the bare one. When operator supplies a path to
    `invites.json`, this helper looks for a sibling `invites.enriched.json`
    and returns that instead so the loader can pick up `intent` + `confidence`.
    Returns the original path unchanged if no enriched sibling exists.
    """
    if invites_input.name == "invites.json":
        sibling = invites_input.with_name("invites.enriched.json")
        if sibling.is_file():
            logger.info(
                "invites_input_prefer_enriched",
                bare=str(invites_input),
                enriched=str(sibling),
            )
            return sibling
    return invites_input


def select_invite_codes(
    records: list[dict[str, Any]],
    *,
    intent_allowlist: list[str] | None = None,
    min_confidence: float = 0.0,
) -> list[str]:
    """Filter Stage 1 invite records by Stage 1.5 intent + confidence.

    Returns the deduplicated list of `invite_code` values that pass the gate.
    Behaviour by record shape:

    - Bare `ScoredInvite` (no `intent` / `confidence` fields): pass through.
      This preserves v1 default behaviour when only `invites.json` exists.
    - Enriched record (has `intent` AND `confidence`):
        * drop if `confidence < min_confidence`
        * drop if `intent_allowlist` is non-empty AND `intent` is not in it
        * otherwise keep
    - Malformed record (missing `invite_code` or wrong type): skip + log.
    """
    out: list[str] = []
    seen: set[str] = set()
    allowlist = list(intent_allowlist or [])
    dropped_low_confidence = 0
    dropped_intent = 0
    kept_bare = 0
    kept_enriched = 0
    for rec in records:
        code = rec.get("invite_code") if isinstance(rec, dict) else None
        if not isinstance(code, str) or not code:
            continue
        intent = rec.get("intent") if isinstance(rec, dict) else None
        confidence = rec.get("confidence") if isinstance(rec, dict) else None
        is_enriched = isinstance(intent, str) and isinstance(confidence, (int, float))
        if is_enriched:
            if float(confidence) < min_confidence:
                dropped_low_confidence += 1
                continue
            if allowlist and intent not in allowlist:
                dropped_intent += 1
                continue
            kept_enriched += 1
        else:
            kept_bare += 1
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
    if dropped_low_confidence or dropped_intent or kept_enriched or kept_bare:
        logger.info(
            "invites_select_summary",
            kept_bare=kept_bare,
            kept_enriched=kept_enriched,
            dropped_low_confidence=dropped_low_confidence,
            dropped_intent=dropped_intent,
            min_confidence=min_confidence,
            intent_allowlist=allowlist,
        )
    return out
