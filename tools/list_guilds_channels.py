"""One-shot ops tool: dump every joined guild + its scannable channels to CSV.

Given a configured ``discord-scanner`` environment (``config.live.yaml`` +
burner token in keyring or ``DISCORD_TOKEN`` env var), this tool calls
``GET /users/@me/guilds`` then ``GET /guilds/{id}/channels`` for every guild
and writes a pipe-delimited CSV with columns:

    guild_name | guild_id | channel_id | channel_name

Channel filter: ``SCANNABLE_CHANNEL_TYPES = {0, 5, 15}`` (text + announcement
+ forum). Threads are not returned by ``/guilds/{id}/channels``; Stage 2
discovers them later via per-channel ``/threads/active``.

The tool reuses the existing hardened client from ``session.rest.make_client``
(full Chrome-UA header set, http2, URL allowlist, structlog token redaction,
captcha hard-abort, 429 Retry-After).

Exit codes (mirror ``cli.py list-guilds``)::

    0 success
    1 config / token error
    2 captcha / 5xx exhausted
    3 401 (token invalid / banned)

Usage::

    python tools/list_guilds_channels.py \\
        --config config.live.yaml \\
        --output guilds_channels.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from discord_scanner.config import ConfigError, load_config
from discord_scanner.discovery.channels import list_channels
from discord_scanner.discovery.guilds import list_my_guilds
from discord_scanner.discovery.invite_resolve import TokenInvalid
from discord_scanner.logging_conf import configure_logging, get_logger
from discord_scanner.models.discord import SCANNABLE_CHANNEL_TYPES
from discord_scanner.session.auth import (
    PlaintextKeyringRefused,
    TokenNotFound,
    load_token,
)
from discord_scanner.session.captcha import CaptchaAborted
from discord_scanner.session.rest import make_client, persist_cookies


async def _run(config_path: Path, output_path: Path) -> int:
    try:
        settings = load_config(config_path)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    configure_logging(level=settings.run.log_level)
    log = get_logger("list_guilds_channels")

    try:
        token, source = load_token(settings)
    except (PlaintextKeyringRefused, TokenNotFound) as e:
        print(f"token error: {e}", file=sys.stderr)
        return 1

    log.info("start", token_source=source.value, output=str(output_path))

    client = make_client(settings, token)
    rows: list[tuple[str, str, str, str]] = []
    exit_code = 0

    try:
        try:
            guilds = await list_my_guilds(client)
        except TokenInvalid:
            print("401 Unauthorized — token invalid (possible ban).", file=sys.stderr)
            return 3
        except CaptchaAborted as e:
            print(f"captcha hit: {e}", file=sys.stderr)
            return 2

        log.info("guilds_listed", count=len(guilds))

        for g in guilds:
            try:
                channels = await list_channels(client, g.id)
            except CaptchaAborted as e:
                print(f"captcha hit on guild {g.id}: {e}", file=sys.stderr)
                exit_code = 2
                break

            scannable = [c for c in channels if c.type in SCANNABLE_CHANNEL_TYPES]
            log.info(
                "guild_channels",
                guild_id=g.id,
                total=len(channels),
                scannable=len(scannable),
            )
            for c in scannable:
                rows.append((g.name, g.id, c.id, c.name or ""))

    finally:
        await client.aclose()
        persist_cookies(client, settings)

    rows.sort(key=lambda r: (r[0].casefold(), r[1], (r[3] or "").casefold(), r[2]))

    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["guild_name", "guild_id", "channel_id", "channel_name"])
        writer.writerows(rows)

    log.info("done", rows=len(rows), exit_code=exit_code)
    print(f"wrote {len(rows)} rows to {output_path}")
    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    p.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to config.live.yaml",
    )
    p.add_argument(
        "--output",
        default=Path("guilds_channels.csv"),
        type=Path,
        help="CSV output path (default: guilds_channels.csv)",
    )
    args = p.parse_args()

    return asyncio.run(_run(args.config, args.output))


if __name__ == "__main__":
    sys.exit(main())
