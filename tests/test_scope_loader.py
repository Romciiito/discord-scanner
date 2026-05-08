"""Tests for `scan/scopes.py` — scope-profile YAML loader (A.7).

Covers: happy path 6-profile load, malformed YAML skipped, forbidden field
combos rejected, duplicate scope_id / guild_id detected, missing dir is OK.
"""

from __future__ import annotations

from pathlib import Path

from discord_scanner.scan.scopes import ScopeMap, load_scope_profiles


def _write_profile(scopes_dir: Path, scope_id: str, **kwargs: object) -> Path:
    """Build a minimal valid YAML for a scope profile."""
    import yaml as _yaml

    payload = {
        "scope_id": scope_id,
        "guilds": kwargs.get("guilds", []),
        "allowed_categories": kwargs.get("allowed_categories", ["model", "lora"]),
        "allowed_pipeline_kinds": kwargs.get("allowed_pipeline_kinds", ["t2i"]),
        "allowed_arch_families": kwargs.get("allowed_arch_families", ["flux"]),
        "keep_threshold": kwargs.get("keep_threshold", 0.55),
        "folder_prefix": kwargs.get("folder_prefix", ""),
        "tag_prefix": kwargs.get("tag_prefix", f"topic/{scope_id}"),
        "nsfw_policy": kwargs.get("nsfw_policy", "drop"),
        "vault": kwargs.get("vault", "main"),
        "persona_anchor_id": kwargs.get("persona_anchor_id"),
        "intent_allowlist": kwargs.get("intent_allowlist", ["prompt_sharing"]),
        "selectors_hint": kwargs.get("selectors_hint", {}),
    }
    p = scopes_dir / f"{scope_id}.yaml"
    p.write_text(_yaml.safe_dump(payload), encoding="utf-8")
    return p


def test_missing_directory_returns_empty_map(tmp_path: Path) -> None:
    sm = load_scope_profiles(tmp_path / "does-not-exist")
    assert isinstance(sm, ScopeMap)
    assert sm.profiles_by_id == {}
    assert sm.guild_to_scope == {}


def test_load_single_profile_indexes_guilds(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_profile(scopes, "influencer-photo", guilds=["g_1", "g_2"])

    sm = load_scope_profiles(scopes)
    assert "influencer-photo" in sm.profiles_by_id
    assert sm.guild_to_scope["g_1"] == "influencer-photo"
    assert sm.guild_to_scope["g_2"] == "influencer-photo"


def test_get_output_tag_for_guild_default_main(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_profile(scopes, "influencer-photo", guilds=["g_1"], vault="main")

    sm = load_scope_profiles(scopes)
    assert sm.get_output_tag_for_guild("g_1") == "main"
    # Unmapped guild defaults to "main"
    assert sm.get_output_tag_for_guild("g_999") == "main"


def test_sidecar_vault_routing(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_profile(
        scopes,
        "fanvue-creator-nsfw",
        guilds=["g_nsfw"],
        vault="sidecar",
        nsfw_policy="keep",
    )

    sm = load_scope_profiles(scopes)
    assert sm.get_output_tag_for_guild("g_nsfw") == "sidecar"


def test_forbidden_combo_sidecar_with_drop_rejected(
    tmp_path: Path, caplog
) -> None:
    """vault=sidecar + nsfw_policy=drop is contradictory; profile skipped."""
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_profile(
        scopes,
        "broken",
        guilds=["g_x"],
        vault="sidecar",
        nsfw_policy="drop",
    )

    with caplog.at_level("WARNING"):
        sm = load_scope_profiles(scopes)
    assert "broken" not in sm.profiles_by_id
    assert "g_x" not in sm.guild_to_scope


def test_empty_pipeline_kinds_rejected(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_profile(scopes, "empty-pipelines", allowed_pipeline_kinds=[])

    sm = load_scope_profiles(scopes)
    assert "empty-pipelines" not in sm.profiles_by_id


def test_duplicate_guild_in_two_profiles_first_wins(
    tmp_path: Path, caplog
) -> None:
    """A guild can only belong to one scope; the second profile to claim
    it gets a WARN and the assignment is rejected (first one stays)."""
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_profile(scopes, "alpha", guilds=["g_shared"])
    _write_profile(scopes, "bravo", guilds=["g_shared"])

    with caplog.at_level("WARNING"):
        sm = load_scope_profiles(scopes)

    # Sorted load — alpha first.
    assert sm.guild_to_scope["g_shared"] == "alpha"


def test_malformed_yaml_skipped(tmp_path: Path, caplog) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    (scopes / "bad.yaml").write_text("not: [yaml: malformed", encoding="utf-8")
    # Plus one valid profile to confirm loader keeps going.
    _write_profile(scopes, "ok", guilds=["g_ok"])

    with caplog.at_level("WARNING"):
        sm = load_scope_profiles(scopes)
    assert "ok" in sm.profiles_by_id
    assert "bad" not in sm.profiles_by_id


def test_selector_hints_propagate(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_profile(
        scopes,
        "with-hints",
        guilds=["g_h"],
        selectors_hint={
            "categories": ["🎨 art"],
            "channels": ["showcase", "*-share"],
            "exclude_channels": ["spam-*"],
        },
    )

    sm = load_scope_profiles(scopes)
    sel = sm.get_selector_for_guild("g_h")
    assert sel is not None
    assert sel.categories == ["🎨 art"]
    assert sel.channels == ["showcase", "*-share"]
    assert sel.exclude_channels == ["spam-*"]


def test_six_v1_profiles_load_from_real_scopes_dir() -> None:
    """Smoke test against the actual `scopes/` directory at project root.

    The 6 v1 profiles ship with empty `guilds: []` — operator fills them.
    Loader should still produce 6 ScopeProfile entries with 0 guild
    assignments. This catches regressions in the YAML schema relative to
    the 6 committed files.
    """
    project_root = Path(__file__).resolve().parents[2]
    scopes_dir = project_root / "scopes"
    if not scopes_dir.exists():
        # Don't fail when running outside the repo (e.g., installed pkg).
        return
    sm = load_scope_profiles(scopes_dir)
    expected = {
        "influencer-photo",
        "product-ugc-persona",
        "product-ugc-creative",
        "fanvue-creator-sfw",
        "fanvue-creator-nsfw",
        "realistic-film-tv",
        "cartoon-film-tv",
    }
    # Allow for additional operator-added profiles; require all 6 v1 present.
    assert expected.issubset(sm.profiles_by_id.keys()), (
        f"missing v1 profiles: {expected - set(sm.profiles_by_id.keys())}"
    )
