from typer.testing import CliRunner
from src.cli import app

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Stage 2 — burner-token Discord scanner producing raw JSONL dumps for Stage 3 curator. Rebuild of dsc-smartscraper." in result.output or "Usage" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0


def test_run_no_args() -> None:
    result = runner.invoke(app, ["run"])
    # Should not crash with no input file
    assert result.exit_code == 0