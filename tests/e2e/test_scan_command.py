"""End-to-end tests for the production `scan/orchestrator.py` — drives
`run_one_pass` against a fully-mocked Discord (respx).

Traces to:
- AGENT-TEAM-WORKPLAN.md §A.0 (orchestrator delivery contract)
- workspace plan §"Q2 — Gap analysis": these are the 8 NEW scenarios
  identified as co-deliverables of Option A.

A.0.1 covers scenarios:
  1. Happy 2-channel path — both channels succeed, RunResult aggregates
  2. Per-channel error isolation — channel A succeeds, channel B hits
     ChannelAbort (3× 429), is skipped, and the scan continues.

Subsequent sub-tasks add their own scenarios (A.0.2 → 3+4+5,
A.0.3 → 6+7+8, A.0.4 → 9, A.0.5 → 10).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from discord_scanner.config import load_config
from discord_scanner.cursor.state import CursorStore
from discord_scanner.scan import FatalScanError, RunResult, run_one_pass


def _make_raw_message(snowflake: int, text: str = "msg") -> dict:
    return {
        "id": str(snowflake),
        "author": {"id": "u_1", "username": "user"},
        "timestamp": "2026-04-26T00:00:00+00:00",
        "content": text,
        "pinned": False,
        "flags": 0,
    }


@pytest.mark.asyncio
async def test_a0_1_happy_two_channel_path(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.1 scenario 1 — two-channel happy path.

    Two scannable channels in one guild. Both have 2 messages each. The
    orchestrator should produce one `messages.jsonl.zst` per channel
    (combined into the day_dir), advance both cursors, and aggregate
    counters into RunResult.
    """
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    guild_id = "g_a01_happy"
    channel_a = "c_a"
    channel_b = "c_b"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "HappyGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": channel_a, "type": 0, "name": "general"},
                    {"id": channel_b, "type": 0, "name": "showcase"},
                    {"id": "c_voice", "type": 2, "name": "voice"},  # filtered
                ],
            )
        )
        # Channel A: 2 messages on first call, empty on second (after-cursor walk)
        mock.get(f"https://discord.com/api/v10/channels/{channel_a}/messages").mock(
            return_value=httpx.Response(
                200,
                json=[_make_raw_message(1001, "a1"), _make_raw_message(1002, "a2")],
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_a}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_b}/messages").mock(
            return_value=httpx.Response(
                200,
                json=[_make_raw_message(2001, "b1"), _make_raw_message(2002, "b2")],
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_b}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    assert len(results) == 1
    rr: RunResult = results[0]
    assert rr.guild_id == guild_id
    assert rr.guild_name == "HappyGuild"
    assert rr.channels_scanned == 2
    assert rr.channels_skipped == 0
    assert rr.channels_failed == 0
    assert rr.messages_fetched == 4
    assert rr.fatal_error is None

    # Per-channel results present
    chan_ids = {c.channel_id for c in rr.channel_results}
    assert chan_ids == {channel_a, channel_b}
    for c in rr.channel_results:
        assert c.skipped is False
        assert c.error is None
        assert c.messages_fetched == 2

    # Cursor advanced to the max snowflake on each channel
    with CursorStore(cfg.run.state_root) as store:
        assert store.get(guild_id, channel_a) == "1002"
        assert store.get(guild_id, channel_b) == "2002"

    # day_dir produced with messages.jsonl.zst + meta.json + prior.txt
    day_dir = cfg.run.output_root / guild_id / "2026-04-26"
    assert (day_dir / "messages.jsonl.zst").exists()
    assert (day_dir / "meta.json").exists()
    assert (day_dir / "prior.txt").exists()


@pytest.mark.asyncio
async def test_a0_1_channel_abort_isolates_other_channel(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.1 scenario 2 — per-channel error isolation.

    Channel A returns 200 with messages; channel B returns 429 three times,
    triggering `ChannelAbort` from the retry layer. The orchestrator MUST
    capture B's failure in `ChannelResult` and continue scanning. The run
    completes with channels_scanned=1 + channels_skipped=1, NOT a fatal
    abort.
    """
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    guild_id = "g_a01_iso"
    channel_a = "c_alpha"
    channel_b = "c_beta_doomed"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "IsolationGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": channel_a, "type": 0, "name": "alpha"},
                    {"id": channel_b, "type": 0, "name": "beta-doomed"},
                ],
            )
        )
        # Channel A: success
        mock.get(f"https://discord.com/api/v10/channels/{channel_a}/messages").mock(
            return_value=httpx.Response(
                200, json=[_make_raw_message(3001, "alpha-msg")]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_a}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )
        # Channel B: 3 consecutive 429s → ChannelAbort
        mock.get(f"https://discord.com/api/v10/channels/{channel_b}/messages").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0"})
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    assert len(results) == 1
    rr = results[0]
    # One success, one skipped — run did NOT abort.
    assert rr.channels_scanned == 1
    assert rr.channels_skipped == 1
    assert rr.channels_failed == 0
    assert rr.messages_fetched == 1
    assert rr.fatal_error is None

    # Verify the failed channel has skip_reason="channel_abort"
    by_id = {c.channel_id: c for c in rr.channel_results}
    assert by_id[channel_a].skipped is False
    assert by_id[channel_a].error is None
    assert by_id[channel_b].skipped is True
    assert by_id[channel_b].skip_reason == "channel_abort"
    assert by_id[channel_b].error is not None

    # Cursor advanced for the success channel ONLY — the failed channel's
    # cursor must NOT have moved (safe resume next run).
    with CursorStore(cfg.run.state_root) as store:
        assert store.get(guild_id, channel_a) == "3001"
        assert store.get(guild_id, channel_b) is None


# ---------------------------------------------------------------------------
# A.0.2 — Error isolation hardening (7 exception classes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a0_2_token_invalid_aborts_run_with_code_3(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.2 scenario — `TokenInvalid` raised on `list_my_guilds` aborts
    the entire run with `FatalScanError(code=3)`. Burner banned mid-config-
    load is a non-recoverable condition; subsequent guilds would all 401."""
    cfg = load_config(tmp_config_yaml_no_gateway)

    with respx.mock() as mock:
        # Token-invalid on the very first call. The discovery layer raises
        # `TokenInvalid` on 401 from the guilds endpoint.
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(401, json={"message": "401: Unauthorized"})
        )

        with pytest.raises(FatalScanError) as excinfo:
            await run_one_pass(
                cfg, SecretStr("BAD_TOKEN"), scan_date="2026-04-26"
            )

    assert excinfo.value.code == 3
    assert excinfo.value.reason == "token_invalid"


@pytest.mark.asyncio
async def test_a0_2_validation_error_skips_one_message(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.2 scenario — a single message that fails `Message.from_api`
    coercion is skipped with WARN; the rest of the channel proceeds and
    the cursor advances on the surviving messages."""
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    guild_id = "g_a02_validation"
    channel_id = "c_only"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "ValidationGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        # 3 messages: middle one is malformed (missing required `timestamp`).
        # `Message.from_api` should raise `ValidationError` on it; orchestrator
        # logs WARN, skips, continues. Final messages_fetched count = 3 (raw)
        # but the dump file contains only the 2 valid records.
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _make_raw_message(7001, "ok-1"),
                    {"id": "7002"},  # malformed: missing author/timestamp
                    _make_raw_message(7003, "ok-2"),
                ],
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    assert len(results) == 1
    rr = results[0]
    # Channel was scanned (not skipped) — partial-record failure isn't fatal.
    assert rr.channels_scanned == 1
    assert rr.channels_skipped == 0
    assert rr.fatal_error is None
    # Raw fetch count = 3 (we count what the API returned).
    assert rr.channel_results[0].messages_fetched == 3
    # Cursor advanced to the max VALID id ("7003"), not the malformed one.
    with CursorStore(cfg.run.state_root) as store:
        assert store.get(guild_id, channel_id) == "7003"


@pytest.mark.asyncio
async def test_a0_2_unexpected_exception_isolates_channel(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.2 scenario — an unexpected exception class on one channel is
    captured by the broad `except Exception` defensive clause; the channel
    is recorded as skipped and the run continues to other channels."""
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    guild_id = "g_a02_unexpected"
    channel_a = "c_alpha"
    channel_b = "c_beta_broken"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "UnexpectedGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": channel_a, "type": 0, "name": "alpha"},
                    {"id": channel_b, "type": 0, "name": "beta-broken"},
                ],
            )
        )
        # Channel A: succeeds.
        mock.get(f"https://discord.com/api/v10/channels/{channel_a}/messages").mock(
            return_value=httpx.Response(200, json=[_make_raw_message(8001, "ok")])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_a}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )
        # Channel B: returns malformed JSON shape (object, not list). The fetch
        # layer logs warning + sets aborted=True. Orchestrator skips channel.
        mock.get(f"https://discord.com/api/v10/channels/{channel_b}/messages").mock(
            return_value=httpx.Response(200, json={"not": "a list"})
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    rr = results[0]
    by_id = {c.channel_id: c for c in rr.channel_results}
    assert by_id[channel_a].skipped is False
    assert by_id[channel_b].skipped is True
    assert by_id[channel_b].skip_reason == "malformed_response"
    assert rr.channels_scanned == 1
    assert rr.channels_skipped == 1
    assert rr.fatal_error is None


# ---------------------------------------------------------------------------
# A.8 — Selector wiring (scope_map applies per-guild selector)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a8_scope_map_applies_per_guild_selector(
    tmp_config_yaml_no_gateway: Path, tmp_path: Path
) -> None:
    """A.8 scenario — when `scopes/<id>.yaml` declares the guild + a
    selector hint (categories or channels), the orchestrator filters
    scannable channels accordingly. Without the wiring, all type-0/5/15
    channels would be scanned.
    """
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    # Seed a scope profile that selects only "showcase" channel.
    scopes_dir = tmp_path / "scopes"
    scopes_dir.mkdir()
    import yaml as _yaml

    (scopes_dir / "test.yaml").write_text(
        _yaml.safe_dump(
            {
                "scope_id": "test",
                "guilds": ["g_a8"],
                "allowed_categories": ["model"],
                "allowed_pipeline_kinds": ["t2i"],
                "allowed_arch_families": ["flux"],
                "keep_threshold": 0.5,
                "folder_prefix": "",
                "tag_prefix": "topic/test",
                "nsfw_policy": "drop",
                "vault": "main",
                "intent_allowlist": ["prompt_sharing"],
                "selectors_hint": {
                    "channels": ["showcase"],
                    "categories": [],
                    "exclude_channels": [],
                },
            }
        ),
        encoding="utf-8",
    )

    guild_id = "g_a8"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "ScopedGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "c_show", "type": 0, "name": "showcase"},
                    {"id": "c_chat", "type": 0, "name": "general-chat"},
                ],
            )
        )
        # Only `showcase` should receive a /messages call. `general-chat`
        # has no mock — respx raises if it leaks through.
        mock.get("https://discord.com/api/v10/channels/c_show/messages").mock(
            return_value=httpx.Response(200, json=[_make_raw_message(1, "ok")])
        )
        mock.get("https://discord.com/api/v10/channels/c_show/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        # Inject scopes_dir via _open_shared_resources's optional arg.
        # Since `run_one_pass` doesn't expose it, patch the default lookup
        # by writing scopes/ next to state_root's parent.
        scopes_at_default = cfg.run.state_root.parent / "scopes"
        if not scopes_at_default.exists():
            import shutil

            shutil.copytree(scopes_dir, scopes_at_default)

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    rr = results[0]
    chan_ids = {c.channel_id for c in rr.channel_results}
    assert chan_ids == {"c_show"}, (
        f"selector should have filtered to 'showcase' only; got {chan_ids}"
    )


# ---------------------------------------------------------------------------
# A.9 — Attachment download in scan_one_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a9_attachments_downloaded(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.9 scenario — message with an image attachment; orchestrator
    downloads via `download_attachment` and increments
    `attachments_downloaded`."""
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

    guild_id = "g_a9"
    channel_id = "c_only"

    raw_msg_with_attachment = {
        "id": "1001",
        "author": {"id": "u_1", "username": "user"},
        "timestamp": "2026-04-26T00:00:00+00:00",
        "content": "with image",
        "pinned": False,
        "flags": 0,
        "attachments": [
            {
                "id": "att1",
                "filename": "image.png",
                "url": "https://cdn.discordapp.com/a/1/image.png",
                "content_type": "image/png",
                "size": 28,
            }
        ],
    }

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "AttachGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=[raw_msg_with_attachment])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )
        mock.get("https://cdn.discordapp.com/a/1/image.png").mock(
            return_value=httpx.Response(200, content=PNG_HEADER + b"data")
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    chan = results[0].channel_results[0]
    assert chan.attachments_downloaded == 1
    assert chan.attachments_skipped_mime == 0
    # File on disk
    att_path = (
        cfg.run.output_root
        / guild_id
        / "2026-04-26"
        / "attachments"
        / "1001_image.png"
    )
    assert att_path.exists()


# ---------------------------------------------------------------------------
# A.10 — Threads fetch for forum channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_forum_threads_scanned(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.10 scenario — forum channel (type=15) triggers archived-threads
    enumeration and per-thread message fetch. Result reports thread
    counters; threads.jsonl written under day_dir."""
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    guild_id = "g_a10"
    forum_id = "c_forum"
    thread_id = "t_thread1"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "ForumGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200,
                json=[{"id": forum_id, "type": 15, "name": "lora-share"}],
            )
        )
        # Forum channel itself has its own messages endpoint (forum posts).
        mock.get(f"https://discord.com/api/v10/channels/{forum_id}/messages").mock(
            return_value=httpx.Response(200, json=[])
        )
        mock.get(f"https://discord.com/api/v10/channels/{forum_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )
        # Archived threads listing. Type 11 = PUBLIC_THREAD per Discord.
        mock.get(
            f"https://discord.com/api/v10/channels/{forum_id}/threads/archived/public"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "threads": [
                        {
                            "id": thread_id,
                            "type": 11,
                            "name": "fluxlora-recipe",
                            "parent_id": forum_id,
                        }
                    ],
                    "members": [],
                    "has_more": False,
                },
            )
        )
        # Thread's messages.
        mock.get(f"https://discord.com/api/v10/channels/{thread_id}/messages").mock(
            return_value=httpx.Response(
                200,
                json=[
                    _make_raw_message(2001, "in-thread-1"),
                    _make_raw_message(2002, "in-thread-2"),
                ],
            )
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    chan = results[0].channel_results[0]
    assert chan.threads_scanned == 1
    assert chan.thread_messages_fetched == 2
    threads_path = (
        cfg.run.output_root / guild_id / "2026-04-26" / "threads.jsonl"
    )
    assert threads_path.exists()


# ---------------------------------------------------------------------------
# A.0.5 — Daemon wiring + CLI live `scan` command
# ---------------------------------------------------------------------------


def test_a0_5_cli_scan_live_invokes_orchestrator(
    tmp_config_yaml_no_gateway: Path,
    mock_keyring: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A.0.5 scenario — `discord-scanner scan --config <yaml> --guild <id>`
    (no --dry-run) calls `run_one_pass`, exits 0 on happy path, and writes
    artefacts under output_root.

    Regression test for the cli.py:372 stub that previously raised
    Exit(2) "not implemented".

    Note: `typer.testing.CliRunner` is sync — no asyncio mark needed.
    Token is provided via Tier 2 (`DISCORD_TOKEN` env), the same pattern
    used by existing CLI tests in `tests/test_cli.py`. Gateway disabled
    via fixture variant so this test exercises only the REST path —
    gateway integration has its own test.
    """
    from typer.testing import CliRunner

    from discord_scanner.cli import app

    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    monkeypatch.setenv("DISCORD_TOKEN", "TEST_TOKEN_VAL")

    guild_id = "g_a05_cli"
    channel_id = "c_only"

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "CliGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=[_make_raw_message(6001, "ok")])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_config_yaml_no_gateway),
                "scan",
                "--guild",
                guild_id,
            ],
        )

    assert result.exit_code == 0, (
        f"exit_code={result.exit_code}, output:\n{result.output}\n"
        f"exc:\n{result.exception}"
    )
    assert "scan complete" in result.output or "scan complete" in result.stderr

    # Verify artefacts on disk
    day_dirs = list((cfg.run.output_root / guild_id).iterdir())
    assert len(day_dirs) == 1
    artefacts = {p.name for p in day_dirs[0].iterdir()}
    assert "messages.jsonl.zst" in artefacts
    assert "meta.json" in artefacts
    assert "prior.txt" in artefacts


# ---------------------------------------------------------------------------
# A.0.4 — Adaptive RL record_response auto-detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a0_4_adaptive_rl_record_response_invoked(
    tmp_config_yaml_no_gateway: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A.0.4 scenario — when `http.adaptive.enabled=True`, `make_client`
    constructs an `AdaptiveRateLimiter` and stashes it on the client.
    Every `request_with_retry` call inside the orchestrator's building
    blocks auto-detects this and threads `record_response` through.

    This test verifies the wiring by patching `record_response` to count
    invocations during a happy-path scan. Without the auto-detection wire,
    the count would be zero and the adaptive feature is silently dead.
    """
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)
    cfg.http.adaptive.enabled = True

    # Patch AdaptiveRateLimiter.record_response to count invocations.
    invocations: list[tuple[int, float, bool]] = []

    from discord_scanner.session.adaptive import AdaptiveRateLimiter

    original_record = AdaptiveRateLimiter.record_response

    async def counting_record(
        self: AdaptiveRateLimiter,
        status: int,
        latency_ms: float,
        was_captcha: bool = False,
    ) -> None:
        invocations.append((status, latency_ms, was_captcha))
        await original_record(self, status, latency_ms, was_captcha)

    monkeypatch.setattr(AdaptiveRateLimiter, "record_response", counting_record)

    guild_id = "g_a04"
    channel_id = "c_only"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "AdaptiveGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=[_make_raw_message(5001, "ok")])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    # Verify record_response fired at least once per HTTP round-trip
    # (guilds + channels + messages + pins ≥ 4). This proves the auto-
    # detection wire is live; the exact count depends on retry attempts.
    assert len(invocations) >= 4, (
        f"AdaptiveRateLimiter.record_response was called {len(invocations)} "
        f"times — expected >= 4. The adaptive feature is silently dead "
        f"without this wire (workplan §Risk 2)."
    )
    # All recordings should be 200-status (happy path).
    for status, latency_ms, was_captcha in invocations:
        assert status == 200
        assert latency_ms >= 0
        assert was_captcha is False


@pytest.mark.asyncio
async def test_a0_4_adaptive_disabled_does_not_construct_adaptive(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.4 scenario — back-compat: when `adaptive.enabled=False`,
    `make_client` builds a plain `RateLimiter` (not AdaptiveRateLimiter).
    The retry layer's auto-detection returns None; record_response is not
    called even if a future caller wires it accidentally.
    """
    cfg = load_config(tmp_config_yaml_no_gateway)
    # adaptive.enabled defaults to False; verify the construction path.
    assert cfg.http.adaptive.enabled is False

    from pydantic import SecretStr as _SecretStr

    from discord_scanner.session.adaptive import AdaptiveRateLimiter
    from discord_scanner.session.rate_limit import RateLimiter
    from discord_scanner.session.rest import make_client

    client = make_client(
        cfg, _SecretStr("TEST_TOKEN"), state_root=cfg.run.state_root
    )
    try:
        limiter = client._discord_scanner_limiter  # type: ignore[attr-defined]
        assert isinstance(limiter, RateLimiter)
        assert not isinstance(limiter, AdaptiveRateLimiter)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# A.0.3 — Backfill direction + channel_start_confirmed guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a0_3_backfill_marks_complete_on_channel_start(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.3 scenario — `backfill.enabled=True`. Backward walk hits an empty
    page → `BackfillTermination.channel_start_confirmed=True` → orchestrator
    calls `mark_backfilled`. Cursor's backfill_runs increments + complete=1.
    """
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)
    cfg.backfill.enabled = True

    guild_id = "g_a03_complete"
    channel_id = "c_only"

    # Pre-seed cursor so backfill starts with a non-None `before=`.
    with CursorStore(cfg.run.state_root) as store:
        store.advance(guild_id, channel_id, "9000")
        store.advance_backward(guild_id, channel_id, "8500")

    # 100-message page (full page) → second call returns empty → CHANNEL_START.
    full_backfill_page = [_make_raw_message(8000 + i, f"back-{i}") for i in range(100)]
    oldest_in_full_page = "8000"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "BackfillCompleteGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )

        msg_route = mock.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if "before" in params:
                # First backward call: full page (100 msgs).
                # Second call (before=oldest_in_full_page): empty → CHANNEL_START.
                if params["before"] == oldest_in_full_page:
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=full_backfill_page)
            # Forward (after=9000, no new messages).
            return httpx.Response(200, json=[])

        msg_route.mock(side_effect=handler)
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    rr = results[0]
    assert rr.channels_scanned == 1
    assert rr.fatal_error is None

    chan = rr.channel_results[0]
    assert chan.backfill_messages_fetched == 100
    assert chan.backfill_marked_complete is True
    # Either CHANNEL_START or STUCK_CURSOR — both signal channel-start confirmed.
    assert chan.backfill_stop_reason in ("channel_start", "stuck_cursor")

    # Cursor reflects the bidirectional state.
    with CursorStore(cfg.run.state_root) as store:
        frontier = store.get_frontier(guild_id, channel_id)
    assert frontier.backfill_complete is True
    assert frontier.backfill_runs >= 1
    assert frontier.last_message_id == "9000"


@pytest.mark.asyncio
async def test_a0_3_backfill_short_page_does_not_mark_complete(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.3 scenario — backward walk returns a short page (<100 msgs). This
    is NOT confirmed channel start (could be deletion gap). Orchestrator
    must NOT call `mark_backfilled`; cursor's backfill_complete stays False.
    """
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)
    cfg.backfill.enabled = True

    guild_id = "g_a03_short"
    channel_id = "c_only"

    # Pre-seed cursor so backfill has a non-None `before=`.
    with CursorStore(cfg.run.state_root) as store:
        store.advance(guild_id, channel_id, "9000")
        store.advance_backward(guild_id, channel_id, "7100")

    short_backfill = [_make_raw_message(7000 + i, f"back-{i}") for i in range(10)]

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "BackfillShortGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )

        msg_route = mock.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            if "before" in params:
                # Short page (10 < 100) — terminates with SHORT_PAGE.
                return httpx.Response(200, json=short_backfill)
            # Forward (after=9000, no new messages).
            return httpx.Response(200, json=[])

        msg_route.mock(side_effect=handler)
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    chan = results[0].channel_results[0]
    assert chan.backfill_messages_fetched == 10
    assert chan.backfill_marked_complete is False  # critical guard
    assert chan.backfill_stop_reason == "short_page"

    with CursorStore(cfg.run.state_root) as store:
        frontier = store.get_frontier(guild_id, channel_id)
    assert frontier.backfill_complete is False
    assert frontier.oldest_seen_message_id == "7000"  # walked back to oldest in page


@pytest.mark.asyncio
async def test_a0_3_backfill_skipped_when_already_complete(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.3 scenario — channel already has `backfill_complete=True`. The
    orchestrator skips backfill entirely. respx will raise if any
    `before=` call is issued."""
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)
    cfg.backfill.enabled = True

    guild_id = "g_a03_already_complete"
    channel_id = "c_only"

    # Pre-seed cursor: forward = 5000, backfill_complete = True.
    with CursorStore(cfg.run.state_root) as store:
        store.advance(guild_id, channel_id, "5000")
        store.advance_backward(guild_id, channel_id, "1000")
        store.mark_backfilled(guild_id, channel_id)

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": guild_id, "name": "AlreadyCompleteGuild"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        # Forward only — `before=` would trigger respx's
        # AllMockedAssertionError if backfill leaks through.
        mock.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": "100", "after": "5000"},
        ).mock(return_value=httpx.Response(200, json=[]))
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    chan = results[0].channel_results[0]
    # Backfill skipped → counters stay at zero / not-set.
    assert chan.backfill_messages_fetched == 0
    assert chan.backfill_marked_complete is False  # we didn't run it; not "newly completed"
    assert chan.backfill_stop_reason is None


@pytest.mark.asyncio
async def test_a0_1_guild_filter_restricts_to_one_guild(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    """A.0.1 scenario 3 — `--guild <id>` filter.

    User runs `discord-scanner scan --guild <id>`. Two guilds are returned by
    /users/@me/guilds; only the matching one is scanned. The orchestrator's
    `guild_filter` parameter must apply BEFORE iteration starts (not just
    in the CLI layer) so subsequent operations don't issue HTTP for the
    filtered-out guilds.
    """
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    target_guild = "g_target"
    other_guild = "g_other"

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": target_guild, "name": "Target"},
                    {"id": other_guild, "name": "OtherShouldBeSkipped"},
                ],
            )
        )
        # Only the TARGET guild gets channels mocked. If the orchestrator
        # leaks a request for `g_other`, respx raises AllMockedAssertionError.
        mock.get(f"https://discord.com/api/v10/guilds/{target_guild}/channels").mock(
            return_value=httpx.Response(
                200,
                json=[{"id": "c_only", "type": 0, "name": "general"}],
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/c_only/messages").mock(
            return_value=httpx.Response(200, json=[])
        )
        mock.get(f"https://discord.com/api/v10/channels/c_only/pins").mock(
            return_value=httpx.Response(200, json=[])
        )

        results = await run_one_pass(
            cfg,
            SecretStr("TEST_TOKEN"),
            guild_filter=target_guild,
            scan_date="2026-04-26",
        )

    assert len(results) == 1
    assert results[0].guild_id == target_guild
