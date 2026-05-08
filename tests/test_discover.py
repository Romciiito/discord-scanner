"""Tests for the Stage 1 → Stage 2 → scopes bridge (`scan/discover.py`).

Covers:
- `_scopes_for_intent` matching logic
- `discover_guilds` happy path: enriched feed → resolve → grouped buckets
- `apply_discover_report_to_scopes` writes only auto_route entries
- `add_guilds_to_scope` dedupe + atomic write
- ambiguous bucket: same intent in multiple scopes
- no-match bucket: intent not in any allowlist
- already-mapped bucket: guild already in some scope
- unresolved invites tracked
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

import yaml as _yaml

from discord_scanner.config import load_config
from discord_scanner.scan import (
    DiscoverReport,
    apply_discover_report_to_scopes,
    discover_guilds,
    load_scope_profiles,
)
from discord_scanner.scan.discover import _scopes_for_intent
from discord_scanner.scan.scopes import add_guilds_to_scope
from discord_scanner.session.rest import make_client


def _write_scope(
    scopes_dir: Path,
    scope_id: str,
    intent_allowlist: list[str],
    *,
    guilds: list[str] | None = None,
) -> Path:
    """Write a minimal valid scope YAML."""
    payload = {
        "scope_id": scope_id,
        "guilds": guilds or [],
        "allowed_categories": ["model"],
        "allowed_pipeline_kinds": ["t2i"],
        "allowed_arch_families": ["flux"],
        "keep_threshold": 0.5,
        "folder_prefix": "",
        "tag_prefix": f"topic/{scope_id}",
        "nsfw_policy": "drop",
        "vault": "main",
        "intent_allowlist": intent_allowlist,
    }
    p = scopes_dir / f"{scope_id}.yaml"
    p.write_text(_yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return p


def _write_enriched_invites(
    invites_path: Path, records: list[dict]
) -> None:
    """Write the Stage 1.5 enriched form `{"metadata": {...}, "invites": [...]}`."""
    invites_path.parent.mkdir(parents=True, exist_ok=True)
    invites_path.write_text(
        json.dumps({"metadata": {"claude_model": "test"}, "invites": records}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# _scopes_for_intent
# ---------------------------------------------------------------------------


def test_scopes_for_intent_returns_empty_when_intent_none(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_scope(scopes, "alpha", ["workflows"])
    sm = load_scope_profiles(scopes)
    assert _scopes_for_intent(sm, None) == []


def test_scopes_for_intent_returns_unique_scope(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_scope(scopes, "alpha", ["workflows", "tutorials"])
    _write_scope(scopes, "bravo", ["paid_nsfw"])
    sm = load_scope_profiles(scopes)
    assert _scopes_for_intent(sm, "workflows") == ["alpha"]
    assert _scopes_for_intent(sm, "paid_nsfw") == ["bravo"]


def test_scopes_for_intent_returns_multiple(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_scope(scopes, "alpha", ["workflows", "tutorials"])
    _write_scope(scopes, "bravo", ["workflows", "paid_nsfw"])
    sm = load_scope_profiles(scopes)
    assert _scopes_for_intent(sm, "workflows") == ["alpha", "bravo"]


# ---------------------------------------------------------------------------
# add_guilds_to_scope (round-trip writer)
# ---------------------------------------------------------------------------


def test_add_guilds_appends_and_dedupes(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    p = _write_scope(scopes, "alpha", ["workflows"], guilds=["g_existing"])

    added, total = add_guilds_to_scope(p, ["g_new", "g_existing"])
    assert added == 1  # only "g_new" appended; "g_existing" deduped
    assert total == ["g_existing", "g_new"]

    # Verify on-disk contents
    raw = _yaml.safe_load(p.read_text(encoding="utf-8"))
    assert raw["guilds"] == ["g_existing", "g_new"]


def test_add_guilds_idempotent_on_repeat(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    p = _write_scope(scopes, "alpha", ["workflows"])

    added1, _ = add_guilds_to_scope(p, ["g1", "g2"])
    added2, _ = add_guilds_to_scope(p, ["g1", "g2"])
    assert added1 == 2
    assert added2 == 0


def test_add_guilds_rejects_filename_mismatch(tmp_path: Path) -> None:
    scopes = tmp_path / "scopes"
    scopes.mkdir()
    p = scopes / "alpha.yaml"
    p.write_text(
        _yaml.safe_dump(
            {
                "scope_id": "WRONG",
                "guilds": [],
                "allowed_pipeline_kinds": ["t2i"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mismatches filename"):
        add_guilds_to_scope(p, ["g1"])


# ---------------------------------------------------------------------------
# discover_guilds — full integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_groups_into_buckets(
    tmp_config_yaml_no_gateway: Path, tmp_path: Path
) -> None:
    """End-to-end: enriched feed → resolve → 4 buckets populated correctly."""
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)

    # Set up scopes: alpha owns "workflows", bravo owns "paid_nsfw" + "workflows"
    # (so workflows is AMBIGUOUS), charlie owns "tutorials", and a guild
    # already mapped to alpha to test ALREADY_MAPPED.
    scopes_dir = tmp_path / "scopes"
    scopes_dir.mkdir()
    _write_scope(scopes_dir, "alpha", ["tutorials"], guilds=["g_existing"])
    _write_scope(scopes_dir, "bravo", ["paid_nsfw", "workflows"])
    _write_scope(scopes_dir, "charlie", ["workflows"])

    # Enriched feed: 5 invites covering each bucket type.
    invites_path = tmp_path / "invites.enriched.json"
    cfg.discovery.invites_input = invites_path
    cfg.discovery.filter.intent_allowlist = []  # no-filter — let bucket logic decide
    cfg.discovery.filter.min_confidence = 0.0
    _write_enriched_invites(
        invites_path,
        [
            # Auto-route: "tutorials" → alpha only
            {"invite_code": "abcd1234", "intent": "tutorials", "confidence": 0.9},
            # Auto-route: "paid_nsfw" → bravo only
            {"invite_code": "ef567890", "intent": "paid_nsfw", "confidence": 0.9},
            # Ambiguous: "workflows" matches bravo + charlie
            {"invite_code": "ambig0001", "intent": "workflows", "confidence": 0.9},
            # No-match: "showcase" not in any allowlist
            {"invite_code": "noma0001", "intent": "showcase", "confidence": 0.9},
            # Already-mapped: resolves to g_existing which is in alpha
            {"invite_code": "alrd0001", "intent": "tutorials", "confidence": 0.9},
            # Unresolved: 404
            {"invite_code": "unre0001", "intent": "tutorials", "confidence": 0.9},
        ],
    )

    with respx.mock() as mock:
        mock.get(
            "https://discord.com/api/v10/invites/abcd1234?with_counts=true&with_expiration=true"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"code": "abcd1234", "guild": {"id": "g_a", "name": "AlphaGuild"}},
            )
        )
        mock.get(
            "https://discord.com/api/v10/invites/ef567890?with_counts=true&with_expiration=true"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"code": "ef567890", "guild": {"id": "g_b", "name": "BravoGuild"}},
            )
        )
        mock.get(
            "https://discord.com/api/v10/invites/ambig0001?with_counts=true&with_expiration=true"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"code": "ambig0001", "guild": {"id": "g_c", "name": "AmbigGuild"}},
            )
        )
        mock.get(
            "https://discord.com/api/v10/invites/noma0001?with_counts=true&with_expiration=true"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"code": "noma0001", "guild": {"id": "g_n", "name": "NoMatchGuild"}},
            )
        )
        mock.get(
            "https://discord.com/api/v10/invites/alrd0001?with_counts=true&with_expiration=true"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": "alrd0001",
                    "guild": {"id": "g_existing", "name": "ExistingGuild"},
                },
            )
        )
        mock.get(
            "https://discord.com/api/v10/invites/unre0001?with_counts=true&with_expiration=true"
        ).mock(return_value=httpx.Response(404))

        scope_map = load_scope_profiles(scopes_dir)
        client = make_client(cfg, SecretStr("TEST_TOKEN"), state_root=cfg.run.state_root)
        try:
            report = await discover_guilds(
                client, cfg, scope_map, state_root=cfg.run.state_root
            )
        finally:
            await client.aclose()

    # Assert all four buckets populated correctly
    assert "alpha" in report.auto_route
    assert len(report.auto_route["alpha"]) == 1
    assert report.auto_route["alpha"][0].guild_id == "g_a"

    assert "bravo" in report.auto_route
    assert len(report.auto_route["bravo"]) == 1
    assert report.auto_route["bravo"][0].guild_id == "g_b"

    assert len(report.ambiguous) == 1
    assert report.ambiguous[0].guild_id == "g_c"
    assert sorted(report.ambiguous[0].matching_scopes) == ["bravo", "charlie"]

    assert len(report.no_match) == 1
    assert report.no_match[0].guild_id == "g_n"

    assert len(report.already_mapped) == 1
    assert report.already_mapped[0].guild_id == "g_existing"
    assert report.already_mapped[0].already_mapped_to == "alpha"

    assert report.unresolved == ["unre0001"]


# ---------------------------------------------------------------------------
# apply_discover_report_to_scopes
# ---------------------------------------------------------------------------


def test_apply_writes_only_auto_route(tmp_path: Path) -> None:
    """apply_discover_report_to_scopes writes ONLY the auto_route bucket;
    ambiguous + no_match + already_mapped are never auto-applied."""
    from discord_scanner.scan.discover import DiscoveredGuild

    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_scope(scopes, "alpha", ["tutorials"])
    _write_scope(scopes, "bravo", ["paid_nsfw"])

    report = DiscoverReport()
    report.auto_route["alpha"].append(
        DiscoveredGuild(
            invite_code="x",
            guild_id="g_alpha_1",
            guild_name="A",
            intent="tutorials",
            confidence=0.9,
            matching_scopes=["alpha"],
            already_mapped_to=None,
        )
    )
    # Ambiguous + no_match guilds are present but should be ignored
    report.ambiguous.append(
        DiscoveredGuild(
            invite_code="y",
            guild_id="g_ambig",
            guild_name="?",
            intent="workflows",
            confidence=0.9,
            matching_scopes=["alpha", "bravo"],
            already_mapped_to=None,
        )
    )

    added = apply_discover_report_to_scopes(report, scopes, dry_run=False)
    assert added == {"alpha": 1}

    # Verify alpha got the guild; bravo is untouched
    alpha_raw = _yaml.safe_load((scopes / "alpha.yaml").read_text())
    assert alpha_raw["guilds"] == ["g_alpha_1"]
    bravo_raw = _yaml.safe_load((scopes / "bravo.yaml").read_text())
    assert bravo_raw["guilds"] == []


def test_apply_dry_run_does_not_write(tmp_path: Path) -> None:
    from discord_scanner.scan.discover import DiscoveredGuild

    scopes = tmp_path / "scopes"
    scopes.mkdir()
    _write_scope(scopes, "alpha", ["tutorials"])
    before = (scopes / "alpha.yaml").read_text()

    report = DiscoverReport()
    report.auto_route["alpha"].append(
        DiscoveredGuild(
            invite_code="x",
            guild_id="g1",
            guild_name="X",
            intent="tutorials",
            confidence=0.9,
            matching_scopes=["alpha"],
            already_mapped_to=None,
        )
    )
    added = apply_discover_report_to_scopes(report, scopes, dry_run=True)
    assert added == {"alpha": 1}  # would-add count

    after = (scopes / "alpha.yaml").read_text()
    assert before == after  # file unchanged


def test_apply_skips_missing_scope_yaml(
    tmp_path: Path, caplog
) -> None:
    """auto_route refers to a scope_id with no on-disk YAML — log + skip."""
    from discord_scanner.scan.discover import DiscoveredGuild

    scopes = tmp_path / "scopes"
    scopes.mkdir()
    # No alpha.yaml present.

    report = DiscoverReport()
    report.auto_route["alpha"].append(
        DiscoveredGuild(
            invite_code="x",
            guild_id="g1",
            guild_name="X",
            intent="tutorials",
            confidence=0.9,
            matching_scopes=["alpha"],
            already_mapped_to=None,
        )
    )

    with caplog.at_level("WARNING"):
        added = apply_discover_report_to_scopes(report, scopes, dry_run=False)
    assert added == {}  # nothing applied
