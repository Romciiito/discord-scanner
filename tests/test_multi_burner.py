"""Multi-burner pool tests — M.1 / M.2 / M.3 acceptance.

Covers:
- Token loading (`load_token_for_burner`) prefers per-burner env var, then
  falls back to plain `DISCORD_TOKEN`, then config.
- `run_one_pass` iterates `auth.burners[]` sequentially when populated.
- `--burner` filter restricts to one burner.
- Daemon mode refuses when `auth.burners` has > 1 entries.
- Cursor `burner_id` column populated correctly per-burner row.
- `http_overrides` produces different `X-Super-Properties` per burner
  (anti-clustering invariant — SEC-P0-25 byte-for-byte parity per snapshot).
- Scope→burner ownership: a guild owned by burner-1 is silently skipped
  on burner-2's pass.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import keyring as real_kr
import pytest
import respx
import yaml
from pydantic import SecretStr
from typer.testing import CliRunner
from unittest.mock import MagicMock

from discord_scanner.cli import app
from discord_scanner.config import load_config
from discord_scanner.cursor.state import CursorStore
from discord_scanner.scan import run_one_pass
from discord_scanner.session.auth import (
    TokenSource,
    load_token_for_burner,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_two_burner_config(
    base_yaml: Path,
    *,
    scopes_dir: Path | None = None,
    burner_1_overrides: dict | None = None,
    burner_2_overrides: dict | None = None,
) -> Path:
    """Mutate the shared `tmp_config_yaml` to add two burners + optional
    http_overrides + optional scopes pointer. Returns the modified path."""
    data = yaml.safe_load(base_yaml.read_text(encoding="utf-8"))
    burners = [
        {
            "keyring_username": "burner-1",
            "keyring_service": "discord-scanner-test",
            "schedule_offset_hours": 0,
        },
        {
            "keyring_username": "burner-2",
            "keyring_service": "discord-scanner-test",
            "schedule_offset_hours": 12,
        },
    ]
    if burner_1_overrides:
        burners[0]["http_overrides"] = burner_1_overrides
    if burner_2_overrides:
        burners[1]["http_overrides"] = burner_2_overrides
    data["auth"]["burners"] = burners
    base_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")
    return base_yaml


def _raw_msg(snowflake: int, text: str = "msg") -> dict:
    return {
        "id": str(snowflake),
        "author": {"id": "u_1", "username": "user"},
        "timestamp": "2026-04-26T00:00:00+00:00",
        "content": text,
        "pinned": False,
        "flags": 0,
    }


# ---------------------------------------------------------------------------
# load_token_for_burner — env-var precedence
# ---------------------------------------------------------------------------


def test_load_token_for_burner_per_burner_env_var(
    tmp_config_yaml: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DISCORD_TOKEN_BURNER_1` should win over `DISCORD_TOKEN`."""
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_1", "B1_SPECIFIC")
    monkeypatch.setenv("DISCORD_TOKEN", "GENERIC")
    cfg = load_config(tmp_config_yaml)
    tok, src = load_token_for_burner(
        cfg, keyring_username="burner-1", keyring_service="discord-scanner-test"
    )
    assert tok.get_secret_value() == "B1_SPECIFIC"
    assert src is TokenSource.ENV


def test_load_token_for_burner_falls_back_to_generic_env(
    tmp_config_yaml: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "GENERIC_FALLBACK")
    cfg = load_config(tmp_config_yaml)
    tok, src = load_token_for_burner(
        cfg, keyring_username="burner-1", keyring_service="discord-scanner-test"
    )
    assert tok.get_secret_value() == "GENERIC_FALLBACK"
    assert src is TokenSource.ENV


def test_load_token_for_burner_keyring_first(
    tmp_config_yaml: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyring entry for burner-2 trumps both env vars."""
    real_kr.set_password("discord-scanner-test", "burner-2", "KR_B2")
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_2", "ENV_B2")
    cfg = load_config(tmp_config_yaml)
    tok, src = load_token_for_burner(
        cfg, keyring_username="burner-2", keyring_service="discord-scanner-test"
    )
    assert tok.get_secret_value() == "KR_B2"
    assert src is TokenSource.KEYRING


# ---------------------------------------------------------------------------
# Multi-burner orchestrator — sequential iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_burner_iterates_each_with_own_token(
    tmp_config_yaml_no_gateway: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_one_pass with two burners loads each burner's token in turn."""
    cfg_path = _make_two_burner_config(tmp_config_yaml_no_gateway)
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_1", "B1_TOKEN")
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_2", "B2_TOKEN")
    cfg = load_config(cfg_path)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    guild_id = "g_mb"
    channel_id = "c_mb"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(200, json=[{"id": guild_id, "name": "MB"}])
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=[_raw_msg(7001), _raw_msg(7002)])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        # Pass a placeholder; the orchestrator ignores it in multi-burner mode.
        results = await run_one_pass(
            cfg, SecretStr("placeholder"), scan_date="2026-04-26"
        )

    # Two burners × 1 guild = 2 RunResults.
    assert len(results) == 2
    assert all(r.guild_id == guild_id for r in results)


@pytest.mark.asyncio
async def test_multi_burner_burner_filter_restricts_to_one(
    tmp_config_yaml_no_gateway: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = _make_two_burner_config(tmp_config_yaml_no_gateway)
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_1", "B1_TOKEN")
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_2", "B2_TOKEN")
    cfg = load_config(cfg_path)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    guild_id = "g_filter"
    channel_id = "c_filter"
    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(200, json=[{"id": guild_id, "name": "F"}])
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=[_raw_msg(8001)])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        results = await run_one_pass(
            cfg,
            SecretStr("placeholder"),
            scan_date="2026-04-26",
            burner_filter="burner-2",
        )

    assert len(results) == 1


# ---------------------------------------------------------------------------
# Cursor burner_id column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_burner_id_recorded(
    tmp_config_yaml_no_gateway: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor.burner_id column should reflect which burner advanced
    the cursor most recently. Single-burner mode uses keyring_username."""
    cfg_path = _make_two_burner_config(tmp_config_yaml_no_gateway)
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_1", "B1")
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_2", "B2")
    cfg = load_config(cfg_path)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    guild_id = "g_burner_col"
    channel_id = "c_burner_col"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "B"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=[_raw_msg(5050)])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        await run_one_pass(
            cfg,
            SecretStr("placeholder"),
            scan_date="2026-04-26",
            burner_filter="burner-2",
        )

    # Inspect the row directly.
    with CursorStore(cfg.run.state_root) as store:
        row = store._conn.execute(  # noqa: SLF001 — internal probe ok in tests
            "SELECT burner_id FROM cursor WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ).fetchone()
    assert row is not None
    assert row[0] == "burner-2", f"expected burner-2, got {row[0]!r}"


# ---------------------------------------------------------------------------
# http_overrides produces different X-Super-Properties per burner
# ---------------------------------------------------------------------------


def test_http_overrides_produce_different_x_super_properties(
    tmp_config_yaml: Path,
) -> None:
    """SEC-P0-25 hold per-burner: with different http_overrides, the
    blob differs across burners but matches itself within one burner."""
    from discord_scanner.config import BurnerHttpOverrides
    from discord_scanner.session.headers import build_x_super_properties
    from discord_scanner.session.rest import _apply_http_overrides

    cfg = load_config(tmp_config_yaml)
    base = build_x_super_properties(cfg)

    overrides_macos = BurnerHttpOverrides(
        user_agent_chrome_version="148.0.7778.56",
        fake_os_platform="macOS",
        fake_os="Macintosh; Intel Mac OS X 10_15_7",
        locale="en-US",
        client_build_number=349_863,
    )
    overrides_windows = BurnerHttpOverrides(
        user_agent_chrome_version="148.0.7778.56",
        fake_os_platform="Windows",
        fake_os="Windows NT 10.0; Win64; x64",
        locale="en-US",
        client_build_number=350_215,
    )
    macos_settings = _apply_http_overrides(cfg, overrides_macos)
    windows_settings = _apply_http_overrides(cfg, overrides_windows)
    macos_blob = build_x_super_properties(macos_settings)
    windows_blob = build_x_super_properties(windows_settings)

    assert macos_blob != windows_blob, "burners must NOT cluster on the same fingerprint"
    # Decode and verify the OS field actually differs.
    macos_decoded = json.loads(base64.b64decode(macos_blob).decode("utf-8"))
    windows_decoded = json.loads(base64.b64decode(windows_blob).decode("utf-8"))
    assert macos_decoded["os"] == "macOS"
    assert windows_decoded["os"] == "Windows"
    # Default config was Windows; new merged settings should differ on `os`.
    assert macos_decoded["os"] != windows_decoded["os"]
    # Build numbers should be the configured override values.
    assert macos_decoded["client_build_number"] == 349_863
    assert windows_decoded["client_build_number"] == 350_215


# ---------------------------------------------------------------------------
# Scope→burner ownership: filter applied at orchestrator level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_owned_by_other_burner_is_skipped(
    tmp_config_yaml_no_gateway: Path,
    tmp_path: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope file declares `burner: burner-1`. burner-2's scan should
    skip the guild owned by that scope; burner-1 should pick it up."""
    cfg_path = _make_two_burner_config(tmp_config_yaml_no_gateway)
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_1", "B1")
    monkeypatch.setenv("DISCORD_TOKEN_BURNER_2", "B2")

    # Place a scopes/ dir at the project-root sibling of state_root so the
    # default lookup path in _open_shared_resources finds it.
    cfg = load_config(cfg_path)
    scopes_at_default = cfg.run.state_root.parent / "scopes"
    scopes_at_default.mkdir(parents=True, exist_ok=True)
    guild_id = "g_owned_by_b1"
    scope_yaml = scopes_at_default / "burner-1-only.yaml"
    scope_yaml.write_text(
        yaml.safe_dump(
            {
                "scope_id": "burner-1-only",
                "burner": "burner-1",
                "guilds": [guild_id],
                "allowed_categories": ["model"],
                "allowed_pipeline_kinds": ["t2i"],
                "allowed_arch_families": ["sdxl"],
                "keep_threshold": 0.55,
                "folder_prefix": "",
                "tag_prefix": "topic/burner-1-only",
                "nsfw_policy": "drop",
                "vault": "main",
                "intent_allowlist": ["prompt_sharing"],
            }
        ),
        encoding="utf-8",
    )

    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    channel_id = "c_owned"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "OwnedByB1"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=[_raw_msg(6000)])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        # burner-2 only — guild is owned by burner-1, so we expect
        # zero RunResults (burner-2 sees the guild from list_my_guilds
        # but the scope filter drops it).
        b2_results = await run_one_pass(
            cfg,
            SecretStr("placeholder"),
            scan_date="2026-04-26",
            burner_filter="burner-2",
        )
        assert b2_results == []

        # burner-1 — should scan it.
        b1_results = await run_one_pass(
            cfg,
            SecretStr("placeholder"),
            scan_date="2026-04-26",
            burner_filter="burner-1",
        )
        assert len(b1_results) == 1
        assert b1_results[0].guild_id == guild_id


# ---------------------------------------------------------------------------
# CLI: daemon refuse + --burner whitelisting
# ---------------------------------------------------------------------------


def test_cli_daemon_refuses_when_burners_gt_1(
    tmp_config_yaml_no_gateway: Path,
    mock_keyring: MagicMock,
) -> None:
    cfg_path = _make_two_burner_config(tmp_config_yaml_no_gateway)
    runner = CliRunner()
    result = runner.invoke(
        app, ["--config", str(cfg_path), "daemon", "--once"]
    )
    assert result.exit_code == 1
    assert "burners has >1" in result.stdout or "Topology 2" in result.stdout


def test_cli_scan_burner_flag_whitelisted(
    tmp_config_yaml_no_gateway: Path,
    mock_keyring: MagicMock,
) -> None:
    """`--burner unknown-name` is rejected by the CLI before any I/O."""
    cfg_path = _make_two_burner_config(tmp_config_yaml_no_gateway)
    runner = CliRunner()
    result = runner.invoke(
        app, ["--config", str(cfg_path), "scan", "--burner", "totally-unknown"]
    )
    assert result.exit_code == 1
    assert "totally-unknown" in result.stdout
    assert "auth.burners" in result.stdout


def test_cli_store_token_burner_whitelisted(
    tmp_config_yaml_no_gateway: Path,
    mock_keyring: MagicMock,
) -> None:
    cfg_path = _make_two_burner_config(tmp_config_yaml_no_gateway)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(cfg_path), "store-token", "--burner", "totally-unknown"],
    )
    assert result.exit_code == 1
    assert "totally-unknown" in result.stdout
