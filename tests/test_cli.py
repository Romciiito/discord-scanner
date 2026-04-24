"""Phase 0 smoke tests for the Typer CLI skeleton.

Traces to: workplan.md Phase 0 done definition, Phase 1 test stubs.
"""

from __future__ import annotations

from typer.testing import CliRunner

from discord_scanner.cli import app

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "discord-scanner" in result.output.lower()


def test_version_exits_zero() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "python" in result.output
