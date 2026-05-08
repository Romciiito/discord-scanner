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
def store_token(
    ctx: typer.Context,
    burner: Annotated[
        str | None,
        typer.Option(
            "--burner",
            help=(
                "Multi-burner pool: store under this burner's keyring_username. "
                "Must match an entry in `auth.burners[].keyring_username` when set."
            ),
        ),
    ] = None,
) -> None:
    """Prompt for burner token via getpass and store in the OS keyring.

    SEC-P0-01: keyring is the preferred source.
    SEC-P0-02: uses getpass.getpass() — never echoes the token.
    SEC-P0-06: refuses to store if the detected keyring backend is plaintext.

    Multi-burner (M.1): `--burner <username>` writes under a specific
    burner's credential. The username MUST be whitelisted in
    `auth.burners[].keyring_username`; this protects against typos that
    would silently store a token under a stranger's keyring entry.
    """
    cli_ctx: _CliContext = ctx.obj
    service = "discord-scanner"
    username = "burner-1"
    if cli_ctx.config_path:
        try:
            s = load_config(cli_ctx.config_path)
            service = s.auth.keyring_service
            username = s.auth.keyring_username
            if burner is not None:
                whitelist = {b.keyring_username for b in s.auth.burners}
                if not whitelist:
                    console.print(
                        "[red]refusing:[/red] --burner is set but "
                        "auth.burners is empty in the config."
                    )
                    raise typer.Exit(1)
                if burner not in whitelist:
                    console.print(
                        f"[red]refusing:[/red] --burner {burner!r} is not in "
                        f"auth.burners (whitelist={sorted(whitelist)})."
                    )
                    raise typer.Exit(1)
                username = burner
                # Per-burner keyring_service override (falls back to global).
                for b in s.auth.burners:
                    if b.keyring_username == burner and b.keyring_service:
                        service = b.keyring_service
                        break
        except ConfigError as e:
            get_logger().warning("store_token_fallback_to_defaults", err=str(e))
            if burner is not None:
                username = burner

    try:
        import keyring as _kr
        from keyring.errors import KeyringError
    except ImportError as e:  # pragma: no cover — dev dep
        console.print(f"[red]keyring import failed:[/red] {e}")
        raise typer.Exit(1) from e

    # SEC-P0-06: detect + refuse plaintext backends — one predicate shared
    # with session/auth.py so read- and write-side checks cannot drift.
    from discord_scanner.session.auth import is_plaintext_keyring_backend

    backend = _kr.get_keyring()
    if is_plaintext_keyring_backend(backend):
        cls = type(backend)
        console.print(
            "[red]refusing:[/red] detected plaintext keyring backend "
            f"{cls.__module__}.{cls.__name__}. Install a platform-native backend "
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
        resolve_invites_input_path,
        select_invite_codes,
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
        # Prefer Stage 1.5 enriched feed over bare invites.json when both exist.
        invites_path = resolve_invites_input_path(settings.discovery.invites_input)
        enriched = load_enriched_invites(invites_path)
        codes.extend(
            select_invite_codes(
                enriched,
                intent_allowlist=settings.discovery.filter.intent_allowlist,
                min_confidence=settings.discovery.filter.min_confidence,
            )
        )
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
        from rich.markup import escape

        from discord_scanner.discovery.guilds import list_my_guilds
        from discord_scanner.logging_conf import _redact_string
        from discord_scanner.session.retry import request_with_retry

        client = make_client(settings, token)
        try:
            # 401 detection still goes through the raw response so we can map
            # to exit code 3 (detected-ban). Wrapped in request_with_retry for
            # SEC-P0-15 (Retry-After + 5xx backoff).
            try:
                resp = await request_with_retry(
                    client, "GET", "https://discord.com/api/v10/users/@me/guilds"
                )
            except Exception as e:  # noqa: BLE001 — surface to caller
                console.print(f"[red]http error:[/red] {escape(_redact_string(str(e)))}")
                return 2
            if resp.status_code == 401:
                console.print("[red]401 Unauthorized:[/red] token invalid (possible ban).")
                return 3
            if resp.status_code >= 400:
                snippet = _redact_string(resp.text[:200])
                console.print(
                    f"[red]HTTP {resp.status_code}:[/red] {escape(snippet)}",
                )
                return 2
            # Re-list via the typed discovery helper so we get pydantic coercion +
            # malformed-record skipping consistent with the rest of the pipeline.
            guilds = await list_my_guilds(client)
            for g in guilds:
                console.print(f"  {escape(g.id)}  {escape(g.name or '')}")
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
def discover(
    ctx: typer.Context,
    update_scopes: Annotated[
        bool,
        typer.Option(
            "--update-scopes",
            help="Auto-write unambiguous matches into scopes/*.yaml::guilds.",
        ),
    ] = False,
    scopes_dir: Annotated[
        Path | None,
        typer.Option(
            "--scopes-dir",
            help="Override scopes/ location. Default: <project_root>/scopes/",
        ),
    ] = None,
    dry_run_apply: Annotated[
        bool,
        typer.Option(
            "--dry-run-apply",
            help="With --update-scopes, log writes but do not modify files.",
        ),
    ] = False,
) -> None:
    """Resolve enriched invites → guild_ids → group by scope intent_allowlist.

    Bridges Stage 1 (`civit-hf-scanner`) → Stage 2 → `scopes/*.yaml`.
    Stage 1 cannot emit guild_ids (no Discord API by design); Stage 2's
    `resolve_invite()` does the lookup.

    Reports four buckets:
      - AUTO-ROUTE: invite's `intent` matches exactly ONE scope's
        `intent_allowlist`. Safe to auto-add with `--update-scopes`.
      - AMBIGUOUS: multiple scopes match. Operator chooses.
      - NO MATCH: no scope's intent_allowlist contains this intent.
      - ALREADY MAPPED: guild already in some scope's `guilds: []`.

    `--update-scopes` writes ONLY the AUTO-ROUTE bucket. Ambiguous and
    unmatched guilds are NEVER auto-written.
    """
    import asyncio

    from discord_scanner.scan import (
        apply_discover_report_to_scopes,
        discover_guilds,
        load_scope_profiles,
        render_discover_report_table,
    )
    from discord_scanner.session.auth import (
        PlaintextKeyringRefused,
        TokenNotFound,
        load_token,
    )
    from discord_scanner.session.rest import make_client

    settings = _require_config(ctx)
    cli_ctx: _CliContext = ctx.obj

    # Resolve scopes_dir: CLI override > project-root sibling of state_root > "scopes"
    if scopes_dir is None:
        candidate = settings.run.state_root.parent / "scopes"
        scopes_dir = candidate if candidate.exists() else Path("scopes")
    if not scopes_dir.exists():
        console.print(
            f"[yellow]scopes dir {scopes_dir} not found — nothing to map against.[/yellow]"
        )
        raise typer.Exit(1)

    if cli_ctx.dry_run:
        console.print(
            f"[bold]dry-run discover plan[/bold]\n"
            f"  invites_input  = {settings.discovery.invites_input}\n"
            f"  scopes_dir     = {scopes_dir}\n"
            f"  intent_filter  = {settings.discovery.filter.intent_allowlist}\n"
            f"  min_confidence = {settings.discovery.filter.min_confidence}\n"
            f"  update_scopes  = {update_scopes}"
        )
        raise typer.Exit(0)

    try:
        token, _source = load_token(settings)
    except (PlaintextKeyringRefused, TokenNotFound) as e:
        console.print(f"[red]token error:[/red] {e}")
        raise typer.Exit(1) from e

    scope_map = load_scope_profiles(scopes_dir)
    if not scope_map.profiles_by_id:
        console.print(f"[yellow]no scope profiles loaded from {scopes_dir}[/yellow]")
        raise typer.Exit(1)

    async def _run() -> None:
        client = make_client(settings, token, state_root=settings.run.state_root)
        try:
            report = await discover_guilds(
                client, settings, scope_map, state_root=settings.run.state_root
            )
        finally:
            await client.aclose()

        console.print(render_discover_report_table(report))

        if update_scopes:
            added = apply_discover_report_to_scopes(
                report, scopes_dir, dry_run=dry_run_apply
            )
            if added:
                summary = ", ".join(f"{s}: +{n}" for s, n in sorted(added.items()))
                prefix = "[cyan]would add[/cyan]" if dry_run_apply else "[green]added[/green]"
                console.print(f"\n{prefix} {summary}")
            else:
                console.print("\n[dim]no changes to apply (auto_route empty or all already mapped)[/dim]")

    asyncio.run(_run())
    raise typer.Exit(0)


@app.command()
def scan(
    ctx: typer.Context,
    guild: Annotated[str | None, typer.Option("--guild", help="Restrict to one guild_id")] = None,
    burner: Annotated[
        str | None,
        typer.Option(
            "--burner",
            help=(
                "Multi-burner pool: restrict the scan to ONE burner from "
                "`auth.burners[]`. Required for Topology 2 manual rotation."
            ),
        ),
    ] = None,
) -> None:
    """Full scan of all configured guilds. Supports `--dry-run` (plan only, no network).

    Live scan (A.0.5): delegates to `scan.orchestrator.run_one_pass`. Maps
    `FatalScanError.code` to CLI exit status (1 = generic fatal, 2 = captcha
    / SSRF, 3 = token invalid). Per-channel errors are isolated inside the
    orchestrator and don't affect exit status — operator inspects the logs
    + `discord-scanner status` to see per-channel skip reasons.

    Multi-burner (M.1): when `auth.burners[]` is non-empty, the orchestrator
    iterates burners sequentially. `--burner <username>` filters to one
    (Topology 2 manual rotation: operator switches network interface
    between burner runs). Token is loaded per-burner; the legacy single-
    burner token loader is bypassed.
    """
    import asyncio

    from discord_scanner.scan import FatalScanError, run_one_pass
    from discord_scanner.session.auth import (
        PlaintextKeyringRefused,
        TokenNotFound,
        load_token,
    )

    settings = _require_config(ctx)
    cli_ctx: _CliContext = ctx.obj
    if cli_ctx.dry_run:
        plan = _build_dry_run_plan(settings, guild)
        for line in plan:
            console.print(line)
        raise typer.Exit(0)

    multi_burner = bool(settings.auth.burners)
    if burner is not None and not multi_burner:
        console.print(
            "[red]refusing:[/red] --burner requires `auth.burners[]` "
            "to be populated in the config."
        )
        raise typer.Exit(1)
    if burner is not None:
        whitelist = {b.keyring_username for b in settings.auth.burners}
        if burner not in whitelist:
            console.print(
                f"[red]refusing:[/red] --burner {burner!r} not in "
                f"auth.burners (known: {sorted(whitelist)})."
            )
            raise typer.Exit(1)

    # Token load — single-burner mode loads upfront; multi-burner mode
    # defers to `run_one_pass`, which calls `load_token_for_burner`
    # per-iteration. The dummy SecretStr below is never used by the
    # multi-burner path (the orchestrator ignores it).
    if multi_burner:
        from pydantic import SecretStr

        token = SecretStr("multi-burner-placeholder")  # noqa: S106 — placeholder
    else:
        try:
            token, _source = load_token(settings)
        except (PlaintextKeyringRefused, TokenNotFound) as e:
            console.print(f"[red]token error:[/red] {e}")
            raise typer.Exit(1) from e

    try:
        results = asyncio.run(
            run_one_pass(
                settings, token, guild_filter=guild, burner_filter=burner
            )
        )
    except FatalScanError as e:
        console.print(f"[red]scan aborted ({e.reason}):[/red] {e}")
        raise typer.Exit(e.code) from e

    # Per-guild summary line for operator scan-after-scan visibility.
    total_msgs = sum(r.messages_fetched for r in results)
    total_channels_ok = sum(r.channels_scanned for r in results)
    total_channels_skipped = sum(r.channels_skipped for r in results)
    console.print(
        f"[green]scan complete[/green]: "
        f"{len(results)} guild(s), {total_channels_ok} channel(s), "
        f"{total_msgs} message(s)"
        + (
            f", [yellow]{total_channels_skipped} channel(s) skipped[/yellow]"
            if total_channels_skipped
            else ""
        )
    )
    raise typer.Exit(0)


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
    from discord_scanner.scan import FatalScanError, run_one_pass
    from discord_scanner.session.auth import (
        PlaintextKeyringRefused,
        TokenNotFound,
        load_token,
    )

    settings = _require_config(ctx)

    # M.1.e — daemon refuse for multi-burner pools. The daemon cannot
    # switch OS-level network interface between burners, so Topology 2
    # would all hit the same egress IP and defeat the entire point of
    # multiple burners. Operator must run `scan --burner <name>` manually
    # under each burner's network context.
    if len(settings.auth.burners) > 1:
        console.print(
            "[red]daemon mode disabled when auth.burners has >1 entry.[/red] "
            "Use scan --burner <name> manually for Topology 2."
        )
        raise typer.Exit(1)

    try:
        token, _source = load_token(settings)
    except (PlaintextKeyringRefused, TokenNotFound) as e:
        console.print(f"[red]token error:[/red] {e}")
        raise typer.Exit(1) from e

    async def _real_scan(s: Settings) -> None:
        """Per-cycle scan closure injected into DaemonLoop. FatalScanError
        propagates upward; DaemonLoop logs and continues to the next cycle
        (same recovery semantics as any other scan exception)."""
        try:
            await run_one_pass(s, token)
        except FatalScanError:
            # The daemon's own loop logs scan failures via its
            # `last_scan_error` field; let the exception propagate so it's
            # captured cleanly. The daemon does NOT exit on fatal — operator
            # decides via SIGTERM after seeing the log.
            raise

    loop = DaemonLoop(settings, _real_scan)
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
def status(
    ctx: typer.Context,
    show_backfill: bool = typer.Option(
        True,
        "--backfill/--no-backfill",
        help="Show v2 backfill frontier columns (oldest_seen, backfill_runs, complete).",
    ),
) -> None:
    """Print cursor state per channel. `--offline` makes this read-only.

    SEC-P0 adjacent (Phase 5): reads `state/cursor.sqlite` without acquiring
    the write lock, so a running scan is not disturbed.

    v2 (default ON): adds backfill frontier columns (oldest_seen,
    backfill_runs, backfill_complete). Pass `--no-backfill` to suppress
    them and get the legacy 4-column view.
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
        if show_backfill:
            frontiers = store.all_frontiers()
        else:
            frontiers = []
            rows = store.all_rows()
    if show_backfill:
        if not frontiers:
            console.print("[dim]cursor is empty.[/dim]")
            raise typer.Exit(0)
        table = Table(title=f"cursor frontiers ({len(frontiers)})")
        table.add_column("guild_id", overflow="fold")
        table.add_column("channel_id", overflow="fold")
        table.add_column("newest_seen", overflow="fold")
        table.add_column("oldest_seen", overflow="fold")
        table.add_column("backfill_runs", justify="right")
        table.add_column("complete", justify="center")
        n_complete = 0
        n_in_progress = 0
        n_unstarted = 0
        for g, c, f in frontiers:
            newest = f.newest_seen_message_id or f.last_message_id or "-"
            oldest = f.oldest_seen_message_id or "-"
            done = "✓" if f.backfill_complete else ("…" if f.backfill_runs > 0 else "-")
            if f.backfill_complete:
                n_complete += 1
            elif f.backfill_runs > 0:
                n_in_progress += 1
            else:
                n_unstarted += 1
            table.add_row(g, c, newest, oldest, str(f.backfill_runs), done)
        console.print(table)
        console.print(
            f"[dim]backfill: {n_complete} complete, "
            f"{n_in_progress} in-progress, {n_unstarted} unstarted.[/dim]"
        )
        raise typer.Exit(0)
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
