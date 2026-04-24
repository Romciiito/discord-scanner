"""Tests for fetch/messages, pinned, threads, jitter + models/message.

Traces to: workplan.md Phase 6, SEC-P0-14/15, claude-rules MUST "Pydantic
tolerance".
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from discord_scanner.config import load_config
from discord_scanner.fetch.jitter import daemon_next_sleep_seconds
from discord_scanner.fetch.messages import fetch_channel_messages
from discord_scanner.fetch.pinned import fetch_channel_pinned
from discord_scanner.fetch.threads import fetch_thread_messages
from discord_scanner.models.message import Attachment, Message, Reaction

# ----------------------------------------------------------------------
# Message model
# ----------------------------------------------------------------------


def test_message_from_api_happy_path() -> None:
    raw = {
        "id": "111",
        "author": {"id": "u1", "username": "alice", "discriminator": "0001"},
        "content": "hi",
        "timestamp": "2026-04-24T00:00:00.000000+00:00",
        "edited_timestamp": None,
        "pinned": False,
        "flags": 0,
        "attachments": [
            {
                "id": "a1",
                "filename": "x.png",
                "content_type": "image/png",
                "size": 123,
                "url": "https://cdn.discordapp.com/attachments/x.png",
            }
        ],
        "reactions": [{"emoji": {"name": "👍"}, "count": 3}],
        "mentions": [{"id": "u2"}],
        "mention_roles": ["r1"],
        "message_reference": {"message_id": "100"},
    }
    m = Message.from_api(raw, guild_id="g1", channel_id="c1", channel_name="general")
    assert m.message_id == "111"
    assert m.author_id == "u1"
    assert m.author_name == "alice"
    assert m.content == "hi"
    assert m.attachments[0].cdn_url.startswith("https://cdn.discordapp.com/")
    assert m.reactions[0].emoji == "👍"
    assert m.reactions[0].count == 3
    assert m.mentions.users == ["u2"]
    assert m.mentions.roles == ["r1"]
    assert m.reply_to_message_id == "100"


def test_message_from_api_tolerates_missing_optional_fields() -> None:
    """Pydantic tolerance + from_api defaults keep the scan running on sparse records."""
    raw = {
        "id": "222",
        "author": {"id": "u1", "username": "bob"},
        "timestamp": "2026-04-24T00:00:00+00:00",
    }
    m = Message.from_api(raw, guild_id="g", channel_id="c")
    assert m.message_id == "222"
    assert m.content == ""
    assert m.attachments == []
    assert m.reactions == []
    assert m.mentions.users == []


def test_message_from_api_skips_malformed_attachment() -> None:
    raw = {
        "id": "333",
        "author": {"id": "u1", "username": "c"},
        "timestamp": "2026-04-24T00:00:00+00:00",
        "attachments": [
            {"id": "a1", "filename": "ok.png", "url": "https://cdn.discordapp.com/ok.png"},
            {"totally": "broken"},  # missing id/filename
        ],
    }
    m = Message.from_api(raw, guild_id="g", channel_id="c")
    assert len(m.attachments) == 1
    assert m.attachments[0].filename == "ok.png"


def test_attachment_model_tolerates_extras() -> None:
    a = Attachment(id="1", filename="x.png", unknown_field=42)
    assert a.id == "1"


def test_reaction_model_tolerates_extras() -> None:
    r = Reaction(emoji="🔥", count=1, unknown="field")
    assert r.emoji == "🔥"


# ----------------------------------------------------------------------
# fetch_channel_messages — pagination + end-of-channel detection
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_messages_paginates_and_stops_on_short_page(
    tmp_config_yaml: Path,
) -> None:
    """100-msg page → 50-msg page → end."""
    cfg = load_config(tmp_config_yaml)
    # tighten jitter so the test doesn't wait seconds
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        first = [{"id": f"{i:020d}"} for i in range(1, 101)]
        second = [{"id": f"{i:020d}"} for i in range(101, 151)]
        mock.get("https://discord.com/api/v10/channels/c1/messages").mock(
            side_effect=[
                httpx.Response(200, json=first),
                httpx.Response(200, json=second),
            ]
        )
        ids: list[str] = []
        async for m in fetch_channel_messages(client, "c1", settings=cfg):
            ids.append(m["id"])
    assert len(ids) == 150
    # sorted ascending — snowflake string sort is timestamp order
    assert ids == sorted(ids)


@pytest.mark.asyncio
async def test_fetch_messages_respects_max_cap(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        full_page = [{"id": f"{i:020d}"} for i in range(1, 101)]
        mock.get("https://discord.com/api/v10/channels/c1/messages").mock(
            return_value=httpx.Response(200, json=full_page)
        )
        ids: list[str] = []
        async for m in fetch_channel_messages(client, "c1", settings=cfg, max_messages=50):
            ids.append(m["id"])
    assert len(ids) == 50


@pytest.mark.asyncio
async def test_fetch_messages_passes_after_cursor(
    tmp_config_yaml: Path,
) -> None:
    """Resume cursor `after=<id>` is forwarded to the first request."""
    cfg = load_config(tmp_config_yaml)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    captured: list[dict] = []

    async with httpx.AsyncClient() as client, respx.mock() as mock:
        route = mock.get("https://discord.com/api/v10/channels/c1/messages")

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(dict(request.url.params))
            return httpx.Response(200, json=[])

        route.mock(side_effect=handler)
        async for _ in fetch_channel_messages(client, "c1", settings=cfg, after="msg_resume_42"):
            pass

    assert captured[0]["after"] == "msg_resume_42"
    assert captured[0]["limit"] == "100"


@pytest.mark.asyncio
async def test_fetch_messages_empty_batch_ends(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/channels/c1/messages").mock(
            return_value=httpx.Response(200, json=[])
        )
        ids = [m async for m in fetch_channel_messages(client, "c1", settings=cfg)]
    assert ids == []


@pytest.mark.asyncio
async def test_fetch_messages_http_error_returns_cleanly(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/channels/c1/messages").mock(
            return_value=httpx.Response(404)
        )
        ids = [m async for m in fetch_channel_messages(client, "c1", settings=cfg)]
    assert ids == []


# ----------------------------------------------------------------------
# Pinned
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_pinned_happy_path(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/channels/c1/pins").mock(
            return_value=httpx.Response(200, json=[{"id": "2"}, {"id": "1"}, {"id": "3"}])
        )
        ids = [m["id"] async for m in fetch_channel_pinned(client, "c1", settings=cfg)]
    assert ids == ["1", "2", "3"]  # sorted ascending


@pytest.mark.asyncio
async def test_fetch_pinned_http_error_returns_empty(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/channels/c1/pins").mock(
            return_value=httpx.Response(403)
        )
        ids = [m async for m in fetch_channel_pinned(client, "c1", settings=cfg)]
    assert ids == []


# ----------------------------------------------------------------------
# Threads
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_thread_uses_same_endpoint_shape(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/channels/t1/messages").mock(
            return_value=httpx.Response(200, json=[{"id": "x"}])
        )
        ids = [m["id"] async for m in fetch_thread_messages(client, "t1", settings=cfg)]
    assert ids == ["x"]


# ----------------------------------------------------------------------
# Daemon jitter
# ----------------------------------------------------------------------


def test_daemon_next_sleep_seconds_within_bounds(tmp_config_yaml: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    low_bound = cfg.daemon.interval_hours * 3600.0 + cfg.daemon.jitter_hours[0] * 3600.0
    high_bound = cfg.daemon.interval_hours * 3600.0 + cfg.daemon.jitter_hours[1] * 3600.0
    for _ in range(50):
        secs = daemon_next_sleep_seconds(cfg)
        assert low_bound <= secs <= high_bound
