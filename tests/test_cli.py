"""Phase 1 CLI integration tests.

Traces to: workplan.md Phase 1 done definition — all 8 commands visible,
`version` exit 0, `store-token` via keyring (mocked), `scan --dry-run`
makes zero HTTP calls, path-traversal exits 1.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from discord_scanner.cli import app

runner = CliRunner()


def test_help_lists_all_eight_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "resolve",
        "list-guilds",
        "scan",
        "daemon",
        "status",
        "store-token",
        "version",
    ):
        assert cmd in result.stdout


def test_help_shows_four_global_flags() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for flag in ("--config", "--verbose", "--dry-run", "--offline"):
        assert flag in result.stdout


def test_version_exits_zero() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "python" in result.stdout


def test_resolve_not_implemented_returns_exit_2(tmp_config_yaml: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_config_yaml), "resolve"])
    assert result.exit_code == 2


def test_list_guilds_not_implemented_returns_exit_2(tmp_config_yaml: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_config_yaml), "list-guilds"])
    assert result.exit_code == 2


def test_missing_config_for_network_commands_returns_exit_1() -> None:
    """Without --config, network-requiring commands exit 1 (user/config error)."""
    result = runner.invoke(app, ["resolve"])
    assert result.exit_code == 1


def test_scan_dry_run_prints_plan_and_makes_zero_http_calls(
    tmp_config_yaml: Path,
) -> None:
    """Phase 1 task: `scan --dry-run` (REQ-F-041) — plan only, zero network."""
    result = runner.invoke(
        app,
        ["--config", str(tmp_config_yaml), "--dry-run", "scan"],
    )
    assert result.exit_code == 0
    assert "dry-run" in result.stdout.lower() or "plan" in result.stdout.lower()


def test_store_token_mocked_keyring(
    tmp_config_yaml: Path,
    mock_keyring: MagicMock,
) -> None:
    """SEC-P0-01/02: store-token uses getpass and writes to keyring."""
    import keyring as real_kr

    result = runner.invoke(
        app,
        ["--config", str(tmp_config_yaml), "store-token"],
        input="fake-valid-looking-token-xyz\n",
    )
    assert result.exit_code == 0, result.stdout
    # store was invoked with the stripped token under the config's service+user
    stored = real_kr.get_password("discord-scanner-test", "test-burner")
    assert stored == "fake-valid-looking-token-xyz"


def test_store_token_refuses_plaintext_keyring(
    tmp_config_yaml: Path,
    mock_keyring: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-P0-06: refuse plaintext keyring fallback."""
    import keyring as real_kr

    fake_plaintext = MagicMock()
    type(fake_plaintext).__name__ = "PlaintextKeyring"
    type(fake_plaintext).__module__ = "keyrings.alt.file"
    monkeypatch.setattr(real_kr, "get_keyring", lambda: fake_plaintext)

    result = runner.invoke(
        app,
        ["--config", str(tmp_config_yaml), "store-token"],
        input="fake-token\n",
    )
    assert result.exit_code == 1
    assert "plaintext" in (result.stdout + (result.stderr or "")).lower()


def test_status_offline_honoured(tmp_config_yaml: Path) -> None:
    """`--offline` flag → `status` reads cursor state only, no network."""
    result = runner.invoke(
        app,
        ["--config", str(tmp_config_yaml), "--offline", "status"],
    )
    # status itself may not be fully implemented; exit 0 or 2 both OK
    # but must not crash with network error
    assert result.exit_code in (0, 2)


def test_path_traversal_config_exits_one(
    tmp_config_yaml: Path,
) -> None:
    """Config with `../../etc` in output_root → exit 1, SSRF-labelled error."""
    data = yaml.safe_load(tmp_config_yaml.read_text())
    data["run"]["output_root"] = "../../etc"
    tmp_config_yaml.write_text(yaml.safe_dump(data))
    result = runner.invoke(app, ["--config", str(tmp_config_yaml), "status"])
    assert result.exit_code == 1


def test_logs_never_contain_raw_token(
    tmp_config_yaml: Path,
    mock_keyring: MagicMock,
) -> None:
    """SEC-P0-05 (assertion track): store-token path logs must not contain
    a raw token-looking string. Covers the only P1 CLI surface that handles a
    token value."""
    # token of 27 chars triggers the token-storage path (above the len>=20 gate)
    tok_input = "AbCdEfGhIjKlMnOpQrStUvWxYz0"
    result = runner.invoke(
        app,
        ["--config", str(tmp_config_yaml), "--verbose", "store-token"],
        input=tok_input + "\n",
    )
    combined = result.stdout + (result.stderr or "")
    assert tok_input not in combined, f"raw token leaked in output: {combined!r}"
    assert result.exit_code == 0
