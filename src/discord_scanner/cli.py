"""Typer CLI entry point — all 8 commands with 4 global flags.

Traces to:
- workplan.md Phase 1 tasks (8 commands visible, store-token, version, scan --dry-run)
- seed-spec.md §6 (CLI surface), §3 exit codes (0 success, 1 user/config, 2 runtime, 3 ban)
- security-model.md §6 SEC-P0-01 / SEC-P0-02 / SEC-P0-06

Phase 1 implements the COMPLETE CLI shape:
- `resolve`, `list-guilds`, `scan` (without --dry-run), `daemon`, `status` — raise
  typer.Exit(2) with a friendly NotImplemented message (filled in Phases 2–9).
- `store-token`, `version`, `scan --dry-run`, `status --offline` — fully functional.

No network is ever contacted from this module.
"""

from __future__ import annotations

import getpass
import os
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from discord_scanner.config import ConfigError, Settings, load_config
from discord_scanner.logging_conf import configure_logging, get_logger

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


class _CliContext:
    """Per-invocation context carried via `typer.Context.obj` (not module-global).

    Avoids module-level mutable state per claude-rules MUST-NOT "No global mutable
    state besides the single shared httpx.AsyncClient, the configured structlog
    logger, and the cursor/cookie/keyring-backed stores".
    """

    __slots__ = ("config_path", "verbose", "dry_run", "offline")

    def __init__(
        self,
        *,
        config_path: Path | None,
        verbose: bool,
        dry_run: bool,
        offline: bool,
    ) -> None:
        self.config_path = config_path
        self.verbose = verbose
        self.dry_run = dry_run
        self.offline = offline


def _load_settings_or_exit(config: Path | None) -> Settings:
    if config is None:
        console.print(
            "[red]error:[/red] --config PATH is required for this command "
            "(or use `version` / `store-token`)."
        )
        raise typer.Exit(1)
    try:
        return load_config(config)
    except ConfigError as e:
        console.print(f"[red]config error:[/red] {e}")
        raise typer.Exit(1) from e


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to config.yaml (required for most commands)"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable debug logging")] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions and exit; make no HTTP calls"),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Read local state only; make no HTTP calls"),
    ] = False,
) -> None:
    """Configure logging + build per-invocation context object on `ctx.obj`."""
    configure_logging(level="debug" if verbose else "info")
    ctx.obj = _CliContext(
        config_path=config,
        verbose=verbose,
        dry_run=dry_run,
        offline=offline,
    )


@app.command()
def version() -> None:
    """Print the version + Python version + git SHA (if available) and exit 0."""
    try:
        v = pkg_version("discord-scanner")
    except PackageNotFoundError:
        v = "0.1.0-dev"
    console.print(v)
    console.print(f"python: {sys.version.split()[0]}")
    sha = os.environ.get("DISCORD_SCANNER_GIT_SHA", "")
    if sha:
        console.print(f"git: {sha[:12]}")


@app.command(name="store-token")
def store_token(ctx: typer.Context) -> None:
    """Prompt for burner token via getpass and store in the OS keyring.

    SEC-P0-01: keyring is the preferred source.
    SEC-P0-02: uses getpass.getpass() — never echoes the token.
    SEC-P0-06: refuses to store if the detected keyring backend is plaintext.
    """
    cli_ctx: _CliContext = ctx.obj
    service = "discord-scanner"
    username = "burner-1"
    if cli_ctx.config_path:
        try:
            s = load_config(cli_ctx.config_path)
            service = s.auth.keyring_service
            username = s.auth.keyring_username
        except ConfigError as e:
            get_logger().warning("store_token_fallback_to_defaults", err=str(e))

    try:
        import keyring as _kr
        from keyring.errors import KeyringError
    except ImportError as e:  # pragma: no cover — dev dep
        console.print(f"[red]keyring import failed:[/red] {e}")
        raise typer.Exit(1) from e

    # SEC-P0-06: detect + refuse plaintext backends.
    backend = _kr.get_keyring()
    cls = type(backend)
    cls_name = cls.__name__
    cls_module = cls.__module__
    if "Plaintext" in cls_name or "keyrings.alt" in cls_module:
        console.print(
            "[red]refusing:[/red] detected plaintext keyring backend "
            f"{cls_module}.{cls_name}. Install a platform-native backend "
            "(Windows Credential Manager / macOS Keychain / Linux Secret Service)."
        )
        raise typer.Exit(1)

    token = getpass.getpass("Discord burner token (input hidden): ")
    if not token or len(token.strip()) < 20:
        console.print("[red]refusing:[/red] token looks too short to be valid.")
        raise typer.Exit(1)
    try:
        _kr.set_password(service, username, token.strip())
    except KeyringError as e:
        console.print(f"[red]keyring error:[/red] {e}")
        raise typer.Exit(1) from e

    get_logger().info(
        "token_stored",
        service=service,
        username=username,
        # token redacted by _redact_processor
    )
    console.print(f"[green]ok[/green] token stored under {service}/{username}.")


def _require_config(ctx: typer.Context) -> Settings:
    cli_ctx: _CliContext = ctx.obj
    return _load_settings_or_exit(cli_ctx.config_path)


@app.command()
def resolve(
    ctx: typer.Context,
    invite: Annotated[str | None, typer.Option("--invite", help="Single invite code")] = None,
) -> None:
    """Resolve invite codes to guild IDs (Phase 4)."""
    _ = _require_config(ctx)
    _ = invite
    console.print(
        "[yellow]not implemented:[/yellow] `resolve` is delivered in Phase 4. See workplan.md."
    )
    raise typer.Exit(2)


@app.command(name="list-guilds")
def list_guilds(ctx: typer.Context) -> None:
    """List guilds the burner has joined (Phase 2)."""
    _ = _require_config(ctx)
    console.print(
        "[yellow]not implemented:[/yellow] `list-guilds` is delivered in Phase 2. See workplan.md."
    )
    raise typer.Exit(2)


@app.command()
def scan(
    ctx: typer.Context,
    guild: Annotated[str | None, typer.Option("--guild", help="Restrict to one guild_id")] = None,
) -> None:
    """Full scan of all configured guilds. Supports `--dry-run` (plan only, no network)."""
    settings = _require_config(ctx)
    cli_ctx: _CliContext = ctx.obj
    if cli_ctx.dry_run:
        plan = _build_dry_run_plan(settings, guild)
        for line in plan:
            console.print(line)
        raise typer.Exit(0)
    console.print(
        "[yellow]not implemented:[/yellow] `scan` (live) is delivered in Phases 4–9. "
        "Use `--dry-run` to preview the plan. See workplan.md."
    )
    raise typer.Exit(2)


def _build_dry_run_plan(settings: Settings, guild_filter: str | None) -> list[str]:
    manual_n = len(settings.discovery.manual_invites)
    invites_path = settings.discovery.invites_input
    invites_input_exists = invites_path.exists()
    guild_clause = f" (guild filter: {guild_filter})" if guild_filter else ""
    return [
        "[bold]dry-run plan[/bold]" + guild_clause,
        f"  config.output_root = {settings.run.output_root}",
        f"  config.state_root  = {settings.run.state_root}",
        f"  invites.input      = {invites_path} (exists={invites_input_exists})",
        f"  manual invites     = {manual_n}",
        f"  filter.min_score   = {settings.discovery.filter.min_score_pct}",
        f"  filter.intents     = {settings.discovery.filter.intent_allowlist}",
        f"  gateway.enabled    = {settings.gateway.enabled}",
        f"  http.rate_limits   = {settings.http.per_host_rate_per_sec}",
        "[dim]no network calls made (--dry-run).[/dim]",
    ]


@app.command()
def daemon(ctx: typer.Context) -> None:
    """Long-running scan loop (Phase 9)."""
    _ = _require_config(ctx)
    console.print(
        "[yellow]not implemented:[/yellow] `daemon` is delivered in Phase 9. See workplan.md."
    )
    raise typer.Exit(2)


@app.command()
def status(ctx: typer.Context) -> None:
    """Print cursor state per channel. `--offline` makes this read-only."""
    settings = _require_config(ctx)
    cli_ctx: _CliContext = ctx.obj
    state_root = settings.run.state_root
    cursor = state_root / "cursor.sqlite"
    console.print("[bold]discord-scanner status[/bold]")
    console.print(f"  state_root     = {state_root}")
    console.print(f"  cursor.sqlite  = {cursor} (exists={cursor.exists()})")
    console.print(f"  offline mode   = {cli_ctx.offline}")
    if not cli_ctx.offline:
        console.print(
            "[yellow]note:[/yellow] live `status` with network probes is "
            "delivered in Phase 5; currently behaves the same as --offline."
        )
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
