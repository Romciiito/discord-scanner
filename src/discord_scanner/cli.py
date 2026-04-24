"""Typer CLI entry point.

Traces to: spec.md §9 (CLI surface), workplan.md Phase 1.

Phase 0: minimal stubs for `version` + `--help`. All feature commands
(`resolve`, `list-guilds`, `scan`, `daemon`, `status`, `store-token`) land
in Phase 1+ with `NotImplementedError` placeholders.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

import typer
from rich.console import Console

from discord_scanner.logging_conf import configure_logging

app = typer.Typer(
    name="discord-scanner",
    help=(
        "Stage 2 — burner-token Discord scanner producing raw JSONL dumps "
        "for Stage 3 curator. Rebuild of dsc-smartscraper."
    ),
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


@app.callback()
def main(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging"
    ),
) -> None:
    """Configure logging before any subcommand runs."""
    configure_logging(level="debug" if verbose else "info")


@app.command()
def version() -> None:
    """Print the version and exit 0."""
    try:
        v = pkg_version("discord-scanner")
    except PackageNotFoundError:
        v = "0.1.0-dev"
    console.print(v)
    console.print(f"python: {sys.version.split()[0]}")


if __name__ == "__main__":
    app()
