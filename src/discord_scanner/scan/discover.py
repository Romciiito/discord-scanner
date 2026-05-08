"""Stage 1 → Stage 2 → scopes bridge — resolves enriched invites to
guild_ids and groups them by which scope's intent_allowlist matches.

Stage 1 (`civit-hf-scanner`) cannot emit guild_id directly (no Discord
API calls per its CLAUDE.md). The Stage 1.5 enriched feed
`invites.enriched.json` carries `invite_code` + `intent` + `confidence`,
never `guild_id`. The bridge:

  invite_code  ──► resolve_invite() ──► guild_id + guild_name
                                           │
                                           ▼
  scope.intent_allowlist matches intent? ──► route guild to scope
                                           │
                                           ▼
                                  scopes/<id>.yaml::guilds

Public surface:
  - `discover_guilds(client, settings, scope_map, ...)` -> DiscoverReport
  - `apply_discover_report_to_scopes(report, scopes_dir, dry_run=False)`
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from discord_scanner.config import Settings
from discord_scanner.discovery.invite_cache import InviteCache
from discord_scanner.discovery.invite_resolve import (
    TokenInvalid,
    load_enriched_invites,
    resolve_invite,
    resolve_invites_input_path,
    select_invite_codes,
)
from discord_scanner.logging_conf import get_logger, redact_invite_code
from discord_scanner.scan.scopes import ScopeMap, add_guilds_to_scope

logger = get_logger(__name__)


@dataclass
class DiscoveredGuild:
    """One resolved invite → guild record."""

    invite_code: str
    guild_id: str
    guild_name: str | None
    intent: str | None  # Stage 1.5 enrichment field; None if bare ScoredInvite
    confidence: float | None  # Stage 1.5 enrichment field
    matching_scopes: list[str]  # scope_ids whose intent_allowlist contains `intent`
    already_mapped_to: str | None  # if guild already in some scope's guilds, that scope_id


@dataclass
class DiscoverReport:
    """Full discover output — grouped by recommended action."""

    auto_route: dict[str, list[DiscoveredGuild]] = field(default_factory=lambda: defaultdict(list))
    """guild_id maps cleanly to ONE scope's intent_allowlist → safe to auto-add."""

    ambiguous: list[DiscoveredGuild] = field(default_factory=list)
    """Multiple scopes match — operator must choose."""

    no_match: list[DiscoveredGuild] = field(default_factory=list)
    """No scope's intent_allowlist contains the intent — operator may
    drop, or define a new scope."""

    already_mapped: list[DiscoveredGuild] = field(default_factory=list)
    """Guild already present in some scope's `guilds: []` — skipped."""

    unresolved: list[str] = field(default_factory=list)
    """invite_codes that failed to resolve (404 / 403 / network)."""

    def total_resolved(self) -> int:
        return (
            sum(len(v) for v in self.auto_route.values())
            + len(self.ambiguous)
            + len(self.no_match)
            + len(self.already_mapped)
        )


async def discover_guilds(
    client: httpx.AsyncClient,
    settings: Settings,
    scope_map: ScopeMap,
    *,
    state_root: Path | None = None,
    invites_input: Path | None = None,
) -> DiscoverReport:
    """Resolve enriched invites to guild IDs and group by scope match.

    Args:
        client: shared httpx.AsyncClient (already configured).
        settings: loaded Settings.
        scope_map: pre-loaded ScopeMap (caller provides — `_open_shared_resources`
            loads it during a normal scan, but `discover` runs lighter).
        state_root: where InviteCache lives (defaults to `settings.run.state_root`).
        invites_input: override path; defaults to `settings.discovery.invites_input`
            with sibling-prefer-enriched semantics.

    Returns: DiscoverReport.
    """
    if state_root is None:
        state_root = settings.run.state_root

    path = invites_input or settings.discovery.invites_input
    path = resolve_invites_input_path(path)
    enriched = load_enriched_invites(path)
    if not enriched:
        logger.info("discover_no_invites", path=str(path))
        return DiscoverReport()

    # Apply intent + confidence filter — same defaults as the scan loop.
    selected_codes = select_invite_codes(
        enriched,
        intent_allowlist=settings.discovery.filter.intent_allowlist,
        min_confidence=settings.discovery.filter.min_confidence,
    )
    if not selected_codes:
        logger.info("discover_no_codes_after_filter")
        return DiscoverReport()

    # Build code → enriched record map for fast lookup of intent/confidence.
    by_code: dict[str, dict[str, Any]] = {}
    for rec in enriched:
        if isinstance(rec, dict) and isinstance(rec.get("invite_code"), str):
            by_code[rec["invite_code"]] = rec

    report = DiscoverReport()

    with InviteCache(state_root) as cache:
        for code in selected_codes:
            try:
                resolved = await resolve_invite(client, code, cache=cache)
            except TokenInvalid:
                # Token-fatal. Re-raise — caller's loop maps to FatalScanError.
                raise
            if resolved is None or resolved.guild_id is None:
                report.unresolved.append(code)
                continue

            rec = by_code.get(code, {})
            intent = rec.get("intent") if isinstance(rec.get("intent"), str) else None
            conf_raw = rec.get("confidence")
            confidence = float(conf_raw) if isinstance(conf_raw, (int, float)) else None

            # Already-mapped check.
            already_in = scope_map.guild_to_scope.get(resolved.guild_id)
            matching = _scopes_for_intent(scope_map, intent)

            dg = DiscoveredGuild(
                invite_code=code,
                guild_id=resolved.guild_id,
                guild_name=resolved.guild_name,
                intent=intent,
                confidence=confidence,
                matching_scopes=matching,
                already_mapped_to=already_in,
            )

            if already_in is not None:
                report.already_mapped.append(dg)
            elif len(matching) == 1:
                report.auto_route[matching[0]].append(dg)
            elif len(matching) > 1:
                report.ambiguous.append(dg)
            else:
                report.no_match.append(dg)

    logger.info(
        "discover_complete",
        total_codes=len(selected_codes),
        resolved=report.total_resolved(),
        unresolved=len(report.unresolved),
        auto_route=sum(len(v) for v in report.auto_route.values()),
        ambiguous=len(report.ambiguous),
        no_match=len(report.no_match),
        already_mapped=len(report.already_mapped),
    )
    return report


def _scopes_for_intent(scope_map: ScopeMap, intent: str | None) -> list[str]:
    """Return scope_ids whose `intent_allowlist` contains `intent`. If
    `intent` is None (bare ScoredInvite, no Stage 1.5 enrichment), no
    scope matches — operator must place the guild manually."""
    if intent is None:
        return []
    return sorted(
        sid
        for sid, profile in scope_map.profiles_by_id.items()
        if intent in profile.intent_allowlist
    )


def apply_discover_report_to_scopes(
    report: DiscoverReport,
    scopes_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Auto-route the unambiguous matches into scope YAML files.

    Only `report.auto_route` entries (single-scope matches) are written.
    `ambiguous` and `no_match` require operator action and are NEVER
    auto-applied.

    Returns: dict mapping `scope_id` → count of guilds added (0 entries
    suppressed; idempotent — re-running on the same report after an
    earlier write adds 0).
    """
    added_per_scope: dict[str, int] = {}
    for scope_id, guilds in report.auto_route.items():
        if not guilds:
            continue
        new_ids = [g.guild_id for g in guilds]
        scope_yaml = scopes_dir / f"{scope_id}.yaml"
        if not scope_yaml.is_file():
            logger.warning(
                "discover_scope_yaml_missing",
                scope_id=scope_id,
                path=str(scope_yaml),
            )
            continue
        if dry_run:
            logger.info(
                "discover_dry_run_would_add",
                scope_id=scope_id,
                guild_ids=new_ids,
            )
            added_per_scope[scope_id] = len(new_ids)
            continue
        added, _all_after = add_guilds_to_scope(scope_yaml, new_ids)
        if added > 0:
            added_per_scope[scope_id] = added
    return added_per_scope


def render_discover_report_table(report: DiscoverReport) -> str:
    """Render a human-readable summary string for `discord-scanner discover`
    CLI output. Used in lieu of Rich tables when the operator pipes output."""
    lines: list[str] = []
    lines.append(f"Resolved: {report.total_resolved()}, unresolved: {len(report.unresolved)}")
    if report.auto_route:
        lines.append("")
        lines.append("AUTO-ROUTE (unambiguous — safe to add with --update-scopes):")
        for scope_id, guilds in sorted(report.auto_route.items()):
            lines.append(f"  → {scope_id} ({len(guilds)} guild(s)):")
            for g in guilds:
                redacted = redact_invite_code(g.invite_code)
                name = g.guild_name or "(unknown name)"
                lines.append(
                    f"      {g.guild_id} {name!r} via {redacted} "
                    f"intent={g.intent} confidence={g.confidence}"
                )
    if report.ambiguous:
        lines.append("")
        lines.append("AMBIGUOUS (multiple scopes match — operator chooses):")
        for g in report.ambiguous:
            lines.append(
                f"  {g.guild_id} {g.guild_name!r} intent={g.intent} "
                f"matches={g.matching_scopes}"
            )
    if report.no_match:
        lines.append("")
        lines.append("NO MATCH (no scope's intent_allowlist contains this intent):")
        for g in report.no_match:
            lines.append(
                f"  {g.guild_id} {g.guild_name!r} intent={g.intent}"
            )
    if report.already_mapped:
        lines.append("")
        lines.append(f"ALREADY MAPPED ({len(report.already_mapped)} guild(s) skipped)")
    if report.unresolved:
        lines.append("")
        lines.append(f"UNRESOLVED INVITES ({len(report.unresolved)}):")
        for code in report.unresolved:
            lines.append(f"  {redact_invite_code(code)}")
    return "\n".join(lines)
