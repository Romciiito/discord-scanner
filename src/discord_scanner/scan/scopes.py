"""Scope-profile YAML loader (A.7).

Reads `scopes/*.yaml` files (operator-declared topic scope profiles) and
produces a `ScopeMap` keyed by guild_id. The orchestrator uses this map to:
  1. Apply per-guild `GuildSelector` (categories + channel globs) at scan-start.
  2. Tag output `meta.json` with `output_tag` (main vs sidecar vault).
  3. Pass intent_allowlist + min_confidence to the Stage 1.5 invite filter.

Schema reference: `discord-curator-bootstrap/foundation_prefill.md`
§"SCOPE PROFILES". Six v1 profiles:
  influencer-photo, product-ugc-persona, product-ugc-creative,
  fanvue-creator-sfw, fanvue-creator-nsfw, realistic-film-tv, cartoon-film-tv.

Forbidden combinations (validated at load):
  - vault: sidecar + nsfw_policy: drop (contradiction)
  - nsfw_policy: route-to-sidecar + vault: main (contradiction)
  - empty allowed_pipeline_kinds (scope produces zero output)

Missing or malformed YAMLs are SKIPPED with a WARNING (fail-open) — operator
may have stub files with `guilds: []` for scopes not yet activated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from discord_scanner.discovery.channels import GuildSelector
from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ScopeProfile:
    """One scope profile, loaded from a single `scopes/<scope_id>.yaml` file."""

    scope_id: str
    guilds: list[str]
    allowed_categories: list[str]
    allowed_pipeline_kinds: list[str]
    allowed_arch_families: list[str]
    keep_threshold: float
    folder_prefix: str
    tag_prefix: str
    nsfw_policy: str  # drop | keep | route-to-sidecar
    vault: str  # main | sidecar
    persona_anchor_id: str | None
    intent_allowlist: list[str]
    selector: GuildSelector
    # M.2 — multi-burner scope ownership. None means "any active burner may
    # scan this scope's guilds" (shared / unowned). When set to a specific
    # burner_id (matching `auth.burners[].keyring_username`), only THAT
    # burner scans this scope's guilds; other burners skip them silently.
    burner: str | None = None


@dataclass
class ScopeMap:
    """Aggregate of all loaded scope profiles, indexed by guild_id."""

    profiles_by_id: dict[str, ScopeProfile] = field(default_factory=dict)
    guild_to_scope: dict[str, str] = field(default_factory=dict)

    def get_profile_for_guild(self, guild_id: str) -> ScopeProfile | None:
        """Return the profile a guild belongs to, or None if unmapped."""
        scope_id = self.guild_to_scope.get(guild_id)
        if scope_id is None:
            return None
        return self.profiles_by_id.get(scope_id)

    def get_selector_for_guild(self, guild_id: str) -> GuildSelector | None:
        """Return the GuildSelector for a guild, or None if no scope owns it."""
        profile = self.get_profile_for_guild(guild_id)
        return profile.selector if profile else None

    def get_output_tag_for_guild(self, guild_id: str) -> str:
        """Return the output tag (vault routing) for `meta.json`. Defaults
        to 'main' when no scope profile owns the guild."""
        profile = self.get_profile_for_guild(guild_id)
        return profile.vault if profile else "main"

    def get_burner_for_guild(self, guild_id: str) -> str | None:
        """M.2 — return the burner_id that owns this guild's scope, or
        None when the scope is shared/unowned or the guild has no scope.

        Caller (orchestrator) uses this to decide whether the current
        burner should scan the guild or silently defer to another burner.
        """
        profile = self.get_profile_for_guild(guild_id)
        return profile.burner if profile else None


def load_scope_profiles(scopes_dir: Path) -> ScopeMap:
    """Load every `*.yaml` under `scopes_dir` and build a ScopeMap.

    Skips and WARNs on:
      - missing directory (returns empty ScopeMap)
      - YAML parse error
      - schema validation failure (forbidden field combos)
      - duplicate scope_id
      - duplicate guild_id across profiles (operator error — guild can only
        belong to one scope at a time)
    """
    scope_map = ScopeMap()
    if not scopes_dir.exists() or not scopes_dir.is_dir():
        logger.info("scopes_dir_missing", path=str(scopes_dir))
        return scope_map

    for yaml_path in sorted(scopes_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            logger.warning("scope_yaml_parse_failed", path=str(yaml_path), err=str(e))
            continue
        if not isinstance(raw, dict):
            logger.warning("scope_yaml_not_a_mapping", path=str(yaml_path))
            continue

        try:
            profile = _build_profile(raw)
        except ValueError as e:
            logger.warning(
                "scope_profile_invalid", path=str(yaml_path), err=str(e)
            )
            continue

        if profile.scope_id in scope_map.profiles_by_id:
            logger.warning(
                "scope_id_duplicate",
                path=str(yaml_path),
                scope_id=profile.scope_id,
            )
            continue
        scope_map.profiles_by_id[profile.scope_id] = profile

        for guild_id in profile.guilds:
            existing = scope_map.guild_to_scope.get(guild_id)
            if existing is not None and existing != profile.scope_id:
                logger.warning(
                    "guild_in_multiple_scopes",
                    guild_id=guild_id,
                    new_scope=profile.scope_id,
                    existing_scope=existing,
                )
                continue
            scope_map.guild_to_scope[guild_id] = profile.scope_id

    logger.info(
        "scope_profiles_loaded",
        profiles=len(scope_map.profiles_by_id),
        guilds_mapped=len(scope_map.guild_to_scope),
    )
    return scope_map


def _build_profile(raw: dict[str, Any]) -> ScopeProfile:
    """Validate + coerce a raw YAML dict into a ScopeProfile.

    Raises ValueError on any forbidden field combination or missing required
    field.
    """
    scope_id = raw.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id:
        raise ValueError("scope_id missing or non-string")

    nsfw_policy = raw.get("nsfw_policy", "drop")
    if nsfw_policy not in ("drop", "keep", "route-to-sidecar"):
        raise ValueError(f"nsfw_policy {nsfw_policy!r} not in {{drop,keep,route-to-sidecar}}")
    vault = raw.get("vault", "main")
    if vault not in ("main", "sidecar"):
        raise ValueError(f"vault {vault!r} not in {{main,sidecar}}")

    # Forbidden combinations (per scopes/README.md).
    if vault == "sidecar" and nsfw_policy == "drop":
        raise ValueError(
            "vault=sidecar + nsfw_policy=drop is contradictory; sidecar exists "
            "to retain NSFW evidence"
        )
    if nsfw_policy == "route-to-sidecar" and vault == "main":
        raise ValueError(
            "nsfw_policy=route-to-sidecar + vault=main is contradictory"
        )

    pipeline_kinds = raw.get("allowed_pipeline_kinds", [])
    if not pipeline_kinds:
        raise ValueError("allowed_pipeline_kinds is empty (scope produces no output)")

    selectors_hint = raw.get("selectors_hint", {}) or {}
    selector = GuildSelector(
        categories=list(selectors_hint.get("categories", []) or []),
        channels=list(selectors_hint.get("channels", []) or []),
        exclude_channels=list(selectors_hint.get("exclude_channels", []) or []),
        fail_open=bool(selectors_hint.get("fail_open", True)),
    )

    burner_raw = raw.get("burner")
    burner: str | None
    if burner_raw is None:
        burner = None
    elif isinstance(burner_raw, str) and burner_raw.strip():
        burner = burner_raw.strip()
    else:
        raise ValueError(
            f"burner must be a non-empty string or null, got {burner_raw!r}"
        )

    return ScopeProfile(
        scope_id=scope_id,
        guilds=[str(g) for g in raw.get("guilds", []) or []],
        allowed_categories=list(raw.get("allowed_categories", []) or []),
        allowed_pipeline_kinds=list(pipeline_kinds),
        allowed_arch_families=list(raw.get("allowed_arch_families", []) or []),
        keep_threshold=float(raw.get("keep_threshold", 0.55)),
        folder_prefix=str(raw.get("folder_prefix", "")),
        tag_prefix=str(raw.get("tag_prefix", f"topic/{scope_id}")),
        nsfw_policy=nsfw_policy,
        vault=vault,
        persona_anchor_id=raw.get("persona_anchor_id"),
        intent_allowlist=list(raw.get("intent_allowlist", []) or []),
        selector=selector,
        burner=burner,
    )


# ---------------------------------------------------------------------------
# Round-trip writer (used by `discord-scanner discover --update-scopes`)
# ---------------------------------------------------------------------------


def add_guilds_to_scope(
    scope_yaml: Path, new_guild_ids: list[str]
) -> tuple[int, list[str]]:
    """Append `new_guild_ids` to a scope YAML's `guilds: [...]` list.

    Idempotent: skips IDs already present. Preserves all other fields and
    YAML formatting via round-trip safe-dump (comment preservation is
    best-effort — pyyaml safe-dump strips comments; tested in
    `tests/test_scope_loader.py`).

    Returns:
        (added_count, all_guild_ids_after) — `added_count` is the number of
        IDs newly appended; `all_guild_ids_after` is the full list after
        the merge for caller logging.

    Raises ValueError if the YAML is malformed or scope_id mismatches the
    filename (defensive — protects against accidental cross-scope writes).
    """
    if not scope_yaml.is_file():
        raise FileNotFoundError(f"scope YAML not found: {scope_yaml}")

    raw = yaml.safe_load(scope_yaml.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"scope YAML root not a mapping: {scope_yaml}")

    scope_id = raw.get("scope_id")
    if not isinstance(scope_id, str):
        raise ValueError(f"scope YAML missing scope_id: {scope_yaml}")
    expected_filename = f"{scope_id}.yaml"
    if scope_yaml.name != expected_filename:
        raise ValueError(
            f"scope_id={scope_id!r} mismatches filename {scope_yaml.name!r}"
        )

    existing = [str(g) for g in (raw.get("guilds") or [])]
    seen = set(existing)
    added: list[str] = []
    for gid in new_guild_ids:
        gid_s = str(gid)
        if gid_s in seen:
            continue
        existing.append(gid_s)
        seen.add(gid_s)
        added.append(gid_s)

    if not added:
        return 0, existing

    raw["guilds"] = existing
    # Atomic write: tmp + rename. Preserves chmod from original.
    tmp = scope_yaml.with_suffix(scope_yaml.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(scope_yaml)
    logger.info(
        "scope_yaml_updated",
        path=str(scope_yaml),
        scope_id=scope_id,
        added=len(added),
        total=len(existing),
    )
    return len(added), existing
