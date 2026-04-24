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
    """Resolve invite codes to guild IDs + names. Uses sqlite cache (7-day TTL)."""
    import asyncio

    import httpx

    from discord_scanner.discovery.invite_cache import InviteCache
    from discord_scanner.discovery.invite_resolve import (
        TokenInvalid,
        load_enriched_invites,
        resolve_invite,
    )
    from discord_scanner.logging_conf import redact_invite_code
    from discord_scanner.session.auth import (
        PlaintextKeyringRefused,
        TokenNotFound,
        load_token,
    )
    from discord_scanner.session.rest import make_client, persist_cookies

    settings = _require_config(ctx)

    # Build invite-code list: --invite wins over config.
    codes: list[str] = []
    if invite:
        codes = [invite]
    else:
        codes.extend(settings.discovery.manual_invites)
        enriched = load_enriched_invites(settings.discovery.invites_input)
        for rec in enriched:
            c = rec.get("invite_code")
            if isinstance(c, str):
                codes.append(c)
    codes = sorted(set(codes))
    if not codes:
        console.print("[yellow]no invite codes to resolve.[/yellow]")
        raise typer.Exit(0)

    try:
        token, _source = load_token(settings)
    except (PlaintextKeyringRefused, TokenNotFound) as e:
        console.print(f"[red]token error:[/red] {e}")
        raise typer.Exit(1) from e

    async def _run() -> int:
        client = make_client(settings, token)
        cache = InviteCache(settings.run.state_root)
        try:
            n_ok = 0
            for code in codes:
                resolved = await resolve_invite(client, code, cache=cache)
                if resolved and resolved.guild_id:
                    console.print(
                        f"  {redact_invite_code(code)}  {resolved.guild_id}  "
                        f"{resolved.guild_name or '<no name>'}"
                    )
                    n_ok += 1
                else:
                    console.print(f"  {redact_invite_code(code)}  (not resolved)")
            console.print(f"[green]ok[/green] {n_ok}/{len(codes)} resolved.")
            return 0
        finally:
            cache.close()
            await client.aclose()
            persist_cookies(client, settings)

    try:
        exit_code = asyncio.run(_run())
    except TokenInvalid as e:
        console.print(f"[red]401 Unauthorized:[/red] {e}")
        raise typer.Exit(3) from e
    except httpx.HTTPError as e:
        console.print(f"[red]http error:[/red] {e}")
        raise typer.Exit(2) from e
    raise typer.Exit(exit_code)


@app.command(name="list-guilds")
def list_guilds(ctx: typer.Context) -> None:
    """List guilds the burner has joined.

    SEC-P0-15 / SEC-P0-16 / SEC-P0-17: uses the shared AsyncClient with
    rate-limit, captcha detection, and URL allowlist enforcement.
    Exit codes: 0 success; 1 config error; 2 runtime (captcha / 5xx exhausted);
    3 detected-ban (401 Unauthorized → suggests token invalid).
    """
    import asyncio

    import httpx

    from discord_scanner.session.auth import (
        PlaintextKeyringRefused,
        TokenNotFound,
        load_token,
    )
    from discord_scanner.session.captcha import CaptchaAborted
    from discord_scanner.session.rest import make_client, persist_cookies

    settings = _require_config(ctx)

    try:
        token, source = load_token(settings)
    except PlaintextKeyringRefused as e:
        console.print(f"[red]refusing:[/red] {e}")
        raise typer.Exit(1) from e
    except TokenNotFound as e:
        console.print(f"[red]no token:[/red] {e}")
        raise typer.Exit(1) from e

    get_logger().info("list_guilds_start", token_source=source.value)

    async def _run() -> int:
        client = make_client(settings, token)
        try:
            resp = await client.get("https://discord.com/api/v10/users/@me/guilds")
            if resp.status_code == 401:
                console.print("[red]401 Unauthorized:[/red] token invalid (possible ban).")
                return 3
            if resp.status_code >= 400:
                console.print(f"[red]HTTP {resp.status_code}:[/red] {resp.text[:200]}")
                return 2
            guilds = resp.json()
            for g in guilds:
                console.print(f"  {g.get('id')}  {g.get('name')}")
            console.print(f"[green]ok[/green] {len(guilds)} guilds.")
            return 0
        finally:
            await client.aclose()
            persist_cookies(client, settings)

    try:
        exit_code = asyncio.run(_run())
    except CaptchaAborted as e:
        console.print(f"[red]captcha:[/red] {e}")
        raise typer.Exit(2) from e
    except httpx.HTTPError as e:
        console.print(f"[red]http error:[/red] {e}")
        raise typer.Exit(2) from e
    raise typer.Exit(exit_code)


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
def daemon(
    ctx: typer.Context,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run one scan iteration then exit (for smoke tests)"),
    ] = False,
    max_iterations: Annotated[
        int | None,
        typer.Option("--max-iterations", help="Cap the loop after N iterations (tests only)"),
    ] = None,
) -> None:
    """Long-running scan loop. Retention prune → scan → sleep(jitter) → repeat.

    SIGINT / SIGTERM trigger a clean shutdown after the current iteration.
    `--once` forces exit after the first iteration — used by smoke tests
    and operators who prefer external schedulers (cron, systemd timer).
    """
    import asyncio

    from discord_scanner.daemon import DaemonLoop

    settings = _require_config(ctx)

    async def _stub_scan(_s: Settings) -> None:
        # P10 wiring replaces this with the full per-guild scan closure.
        # For P9 smoke, the daemon loop is exercised in tests with a
        # synthetic scan function; production wiring lands with P10.
        get_logger().info("daemon_scan_stub", note="real scan wired in P10")

    loop = DaemonLoop(settings, _stub_scan)
    cap = 1 if once else max_iterations
    try:
        asyncio.run(loop.run_forever(max_iterations=cap))
    except KeyboardInterrupt:
        loop.request_shutdown()
    stats = loop.stats
    console.print(
        f"[green]daemon[/green] exited after {stats.iterations} iteration(s); "
        f"last_error={stats.last_scan_error or 'none'}"
    )
    raise typer.Exit(0)


@app.command()
def status(ctx: typer.Context) -> None:
    """Print cursor state per channel. `--offline` makes this read-only.

    SEC-P0 adjacent (Phase 5): reads `state/cursor.sqlite` without acquiring
    the write lock, so a running scan is not disturbed.
    """
    from rich.table import Table

    from discord_scanner.cursor.state import CursorStore

    settings = _require_config(ctx)
    cli_ctx: _CliContext = ctx.obj
    state_root = settings.run.state_root
    cursor_path = state_root / "cursor.sqlite"
    console.print("[bold]discord-scanner status[/bold]")
    console.print(f"  state_root     = {state_root}")
    console.print(f"  cursor.sqlite  = {cursor_path} (exists={cursor_path.exists()})")
    console.print(f"  offline mode   = {cli_ctx.offline}")
    if not cursor_path.exists():
        console.print("[yellow]no cursor state yet — run a scan first.[/yellow]")
        raise typer.Exit(0)
    with CursorStore(state_root) as store:
        rows = store.all_rows()
    if not rows:
        console.print("[dim]cursor is empty.[/dim]")
        raise typer.Exit(0)
    table = Table(title=f"cursor rows ({len(rows)})")
    table.add_column("guild_id", overflow="fold")
    table.add_column("channel_id", overflow="fold")
    table.add_column("last_message_id", overflow="fold")
    table.add_column("updated_at")
    for g, c, lmid, ts in rows:
        table.add_row(g, c, lmid or "-", ts)
    console.print(table)
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
