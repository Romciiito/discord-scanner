"""Phase 1 config tests.

Traces to: workplan.md Phase 1 tasks (config loader), SEC-P0-17 (URL allowlist),
SEC-P0-23 (path-traversal), SEC-P0-25 (fingerprint single source).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from discord_scanner.config import ConfigError, load_config


def test_load_config_happy_path(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    assert cfg.run.log_level == "info"
    assert cfg.auth.token_source == "keyring"  # noqa: S105 — literal is a config mode, not a password
    assert cfg.http.http2 is True
    assert cfg.http.user_agent_chrome_version.startswith("134.")


def test_load_config_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "no-such.yaml"
    with pytest.raises(ConfigError):
        load_config(missing)


def test_load_config_rejects_path_traversal_in_output_root(
    tmp_config_yaml: Path,
) -> None:
    data = yaml.safe_load(tmp_config_yaml.read_text())
    data["run"]["output_root"] = "../../etc"
    tmp_config_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match=r"path|traversal|SSRF|outside"):
        load_config(tmp_config_yaml)


def test_load_config_rejects_absolute_escape(tmp_config_yaml: Path) -> None:
    data = yaml.safe_load(tmp_config_yaml.read_text())
    data["run"]["output_root"] = "C:/Windows" if Path("C:/").exists() else "/etc"
    tmp_config_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(tmp_config_yaml)


def test_load_config_rejects_stale_chrome_ua(tmp_config_yaml: Path) -> None:
    """SEC-P0-08: Chrome UA staleness check."""
    data = yaml.safe_load(tmp_config_yaml.read_text())
    data["http"]["user_agent_chrome_version"] = "100.0.0.0"  # very old
    tmp_config_yaml.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match=r"stale|chrome|version"):
        load_config(tmp_config_yaml)


def test_config_fingerprint_single_source(tmp_config_yaml: Path) -> None:
    """SEC-P0-25: one fingerprint config source drives both REST + gateway."""
    cfg = load_config(tmp_config_yaml)
    fp = cfg.http.fingerprint()
    assert fp["os"]
    assert fp["browser"] == "Chrome"
    assert fp["browser_version"] == cfg.http.user_agent_chrome_version
    assert fp["system_locale"] == cfg.http.locale
    assert isinstance(fp["client_build_number"], int)


def test_config_client_build_number_plausible(tmp_config_yaml: Path) -> None:
    """SEC-P0-26: client_build_number must be plausible (>= 300000)."""
    cfg = load_config(tmp_config_yaml)
    assert cfg.http.client_build_number >= 300_000


def test_token_never_in_repr(tmp_config_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-P0-05: token never written to any artefact — repr() must redact."""
    tok = "MTI0NTY3ODkwMTIzNDU2Nzg5.GabcdE.fghijklmnopqrstuvwxyz1234567"
    monkeypatch.setenv("DISCORD_TOKEN", tok)
    cfg = load_config(tmp_config_yaml)
    repr_str = repr(cfg)
    assert tok not in repr_str
