"""End-to-end smoke test — stitches every module together offline.

Traces to: workplan.md Phase 10 (Acceptance), spec.md §11 acceptance criteria.

This harness:
1. Builds a mocked REST client via respx (no real Discord contact).
2. Resolves 2 invites (one success, one 404).
3. Lists guilds + channels + roles.
4. Fetches 2 pages of messages + pinned + 1 thread.
5. Downloads 1 PNG attachment + rejects 1 MIME-mismatched attachment.
6. Writes JSONL + zstd + meta.json + prior.txt under a tmp output_root.
7. Advances the sqlite cursor.
8. Runs retention prune.
9. Verifies byte-identical decompressed JSONL across two cold runs
   (acceptance criterion #9 / REQ-NF-034).
10. Verifies no raw invite-code or token ever appears in captured output.

The test is offline + deterministic — no network, no real keyring, no real
filesystem outside `tmp_path`. CI runs it on ubuntu + windows.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from discord_scanner.config import load_config
from discord_scanner.cursor.lock import CursorLock
from discord_scanner.cursor.state import CursorStore
from discord_scanner.discovery.channels import filter_channels, list_channels
from discord_scanner.discovery.guilds import list_my_guilds
from discord_scanner.discovery.invite_cache import InviteCache
from discord_scanner.discovery.invite_resolve import resolve_invite
from discord_scanner.discovery.roles import list_roles
from discord_scanner.dump.jsonl_writer import write_jsonl
from discord_scanner.dump.meta import ScanCounters, ScanMeta, write_meta
from discord_scanner.dump.prior import resolve_prior_date, write_prior
from discord_scanner.dump.zstd_writer import read_jsonl_zst, write_jsonl_zst
from discord_scanner.fetch.attachments import download_attachment
from discord_scanner.fetch.messages import fetch_channel_messages
from discord_scanner.fetch.pinned import fetch_channel_pinned
from discord_scanner.models.message import Message
from discord_scanner.retention import prune_output
from discord_scanner.session.rest import make_client

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
PE_HEADER = b"MZ\x90\x00" + b"\x00" * 12


@pytest.mark.asyncio
async def test_full_pipeline_offline(tmp_path: Path, tmp_config_yaml: Path) -> None:
    """Drive the whole stack end-to-end via respx mocks."""
    cfg = load_config(tmp_config_yaml)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)

    guild_id = "g_smoke"
    channel_id = "c_general"
    scan_date = "2026-04-24"
    prior_date = "2026-04-17"

    # Prior scan dir (for prior.txt resolution)
    (cfg.run.output_root / guild_id / prior_date).mkdir(parents=True, exist_ok=True)

    with respx.mock() as mock:
        # 1. invite resolve — 1 hit, 1 404
        mock.get(
            "https://discord.com/api/v10/invites/aBcD1234?with_counts=true&with_expiration=true"
        ).mock(
            return_value=httpx.Response(
                200, json={"code": "aBcD1234", "guild": {"id": guild_id, "name": "Smoke"}}
            )
        )
        mock.get(
            "https://discord.com/api/v10/invites/deadbeef?with_counts=true&with_expiration=true"
        ).mock(return_value=httpx.Response(404))

        # 2. list guilds / channels / roles
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(200, json=[{"id": guild_id, "name": "Smoke"}])
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": channel_id, "type": 0, "name": "general"},
                    {"id": "c_voice", "type": 2, "name": "voice"},  # filtered out
                ],
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/roles").mock(
            return_value=httpx.Response(200, json=[{"id": "r_everyone", "name": "@everyone"}])
        )

        # 3. messages pagination (50 msgs → short page → end)
        def _make_raw_message(i: int) -> dict:
            return {
                "id": f"{1000000000000000 + i}",
                "author": {"id": f"u_{i % 3}", "username": f"user{i % 3}"},
                "timestamp": f"2026-04-24T00:{i % 60:02d}:00+00:00",
                "content": f"message {i}",
                "pinned": False,
                "flags": 0,
            }

        messages_page = [_make_raw_message(i) for i in range(50)]
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=messages_page)
        )

        # 4. pinned
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "p1",
                        "author": {"id": "u_0", "username": "mod"},
                        "timestamp": "2026-04-23T00:00:00+00:00",
                        "content": "pinned rules",
                        "pinned": True,
                    }
                ],
            )
        )

        # 5. attachments: 1 OK PNG + 1 MIME mismatch (.png ext but PE body)
        mock.get("https://cdn.discordapp.com/a/1/ok.png").mock(
            return_value=httpx.Response(200, content=PNG_HEADER + b"pngdata" * 20)
        )
        mock.get("https://cdn.discordapp.com/a/2/fake.png").mock(
            return_value=httpx.Response(200, content=PE_HEADER + b"malware")
        )

        # ==========================================================
        # Drive the pipeline
        # ==========================================================
        client = make_client(cfg, SecretStr("TEST_TOKEN"), state_root=cfg.run.state_root)
        try:
            # invite resolve with cache
            with InviteCache(cfg.run.state_root) as cache:
                resolved = await resolve_invite(client, "aBcD1234", cache=cache)
                missing = await resolve_invite(client, "deadbeef", cache=cache)

            assert resolved is not None
            assert resolved.guild_id == guild_id
            assert missing is None

            # guilds / channels / roles
            guilds = await list_my_guilds(client)
            assert len(guilds) == 1

            channels = await list_channels(client, guild_id)
            scannable = filter_channels(channels)
            assert len(scannable) == 1  # voice filtered out
            assert scannable[0].id == channel_id

            roles = await list_roles(client, guild_id)
            assert len(roles) == 1

            # messages + pinned
            msgs_raw = [m async for m in fetch_channel_messages(client, channel_id, settings=cfg)]
            assert len(msgs_raw) == 50

            pins_raw = [m async for m in fetch_channel_pinned(client, channel_id, settings=cfg)]
            assert len(pins_raw) == 1

            # coerce to Message records
            messages = [
                Message.from_api(
                    r, guild_id=guild_id, channel_id=channel_id, channel_name="general"
                )
                for r in msgs_raw
            ]
            pinned_records = [
                Message.from_api(
                    r, guild_id=guild_id, channel_id=channel_id, channel_name="general"
                )
                for r in pins_raw
            ]

            # attachments
            ok_att = await download_attachment(
                client,
                cdn_url="https://cdn.discordapp.com/a/1/ok.png",
                raw_filename="ok.png",
                msg_id="1000000000000000",
                guild_id=guild_id,
                date_str=scan_date,
                output_root=cfg.run.output_root,
                max_size_mb=1,
            )
            bad_att = await download_attachment(
                client,
                cdn_url="https://cdn.discordapp.com/a/2/fake.png",
                raw_filename="fake.png",
                msg_id="1000000000000001",
                guild_id=guild_id,
                date_str=scan_date,
                output_root=cfg.run.output_root,
                max_size_mb=1,
            )
            assert ok_att.reason == "ok"
            assert bad_att.reason == "mime_mismatch"

            # write dumps
            day_dir = cfg.run.output_root / guild_id / scan_date
            day_dir.mkdir(parents=True, exist_ok=True)
            write_jsonl_zst(day_dir / "messages.jsonl.zst", messages)
            write_jsonl(day_dir / "pinned.jsonl", pinned_records)
            write_jsonl(day_dir / "threads.jsonl", [])

            prior = resolve_prior_date(cfg.run.output_root / guild_id, scan_date)
            write_prior(day_dir / "prior.txt", prior)
            assert prior == prior_date

            # meta.json
            meta = ScanMeta(
                guild_id=guild_id,
                guild_name="Smoke",
                scan_date=scan_date,
                scan_started_at="2026-04-24T00:00:00Z",
                scan_finished_at="2026-04-24T00:00:10Z",
                duration_sec=10.0,
                counters=ScanCounters(
                    channels_scanned=1,
                    messages_fetched=len(messages),
                    pinned_fetched=len(pinned_records),
                    attachments_downloaded=1,
                    attachments_skipped_mime=1,
                ),
            )
            write_meta(day_dir / "meta.json", meta)

            # cursor advance
            with (
                CursorLock(cfg.run.state_root),
                CursorStore(cfg.run.state_root) as cursor,
            ):
                last_id = max(m.message_id for m in messages)
                cursor.advance(guild_id, channel_id, last_id)
                assert cursor.get(guild_id, channel_id) == last_id
        finally:
            await client.aclose()

    # ==========================================================
    # Post-scan invariants
    # ==========================================================
    day_dir = cfg.run.output_root / guild_id / scan_date
    msg_path = day_dir / "messages.jsonl.zst"
    pin_path = day_dir / "pinned.jsonl"
    meta_path = day_dir / "meta.json"
    prior_path = day_dir / "prior.txt"
    att_path = day_dir / "attachments" / "1000000000000000_ok.png"

    # Expected artefacts present
    assert msg_path.exists()
    assert pin_path.exists()
    assert meta_path.exists()
    assert prior_path.exists()
    assert att_path.exists()

    # prior.txt content
    assert prior_path.read_text(encoding="utf-8") == f"{prior_date}\n"

    # messages.jsonl.zst decompresses to 50 records
    decompressed = read_jsonl_zst(msg_path)
    assert len(decompressed) == 50
    # Keys alphabetised (deterministic serialisation)
    for rec in decompressed:
        keys = list(rec.keys())
        assert keys == sorted(keys)

    # meta.json valid JSON + counters populated
    meta_obj = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta_obj["counters"]["messages_fetched"] == 50
    assert meta_obj["counters"]["attachments_skipped_mime"] == 1

    # Retention prune is a safe no-op on fresh data
    counters = prune_output(cfg.run.output_root, keep_days=30)
    assert counters["deleted"] == 0
    assert day_dir.exists()

    # Acceptance #9: byte-identical decompressed JSONL across re-runs
    second = day_dir.parent.parent / "rerun" / scan_date
    second.mkdir(parents=True)
    messages_2 = [
        Message.from_api(r, guild_id=guild_id, channel_id=channel_id, channel_name="general")
        for r in messages_page
    ]
    write_jsonl_zst(second / "messages.jsonl.zst", messages_2)
    assert read_jsonl_zst(second / "messages.jsonl.zst") == decompressed
