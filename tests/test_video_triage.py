"""Unit + integration tests for the v3 video triage module.

Covers:
- `is_gif` positive cases for all three Discord delivery shapes + negatives
- `is_video` positive (.mp4/.mov/.webm/video/* content types) + negatives
- `VideoTriageWriter` JSONL append + atomic write semantics
- `write_video_triage_md` markdown rendering with high/medium/low buckets
- Integration: scan one channel with PNG + GIF + MP4 → only PNG downloaded,
  GIF silent-skipped, MP4 in triage JSONL but NOT in attachments/.

Traces to operator decision 2026-04-26 (silent GIF skip + non-GIF video
metadata sidecar) — see HANDOFF.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from discord_scanner.config import load_config
from discord_scanner.dump.video_triage import (
    VideoTriageWriter,
    is_gif,
    is_video,
    write_video_triage_md,
)
from discord_scanner.models.discord import Channel
from discord_scanner.models.message import Attachment, Message
from discord_scanner.scan import run_one_pass


# ---------------------------------------------------------------------------
# is_gif — all three Discord delivery shapes plus a negative case
# ---------------------------------------------------------------------------


def test_is_gif_image_gif_content_type() -> None:
    assert is_gif("image/gif", "meme.gif", "https://cdn.discordapp.com/a/1/meme.gif")


def test_is_gif_video_mp4_with_gifv_filename() -> None:
    # Discord transcodes uploaded GIFs to mp4 but keeps `.gifv`.
    assert is_gif("video/mp4", "ffmotion.gifv", "https://cdn.discordapp.com/a/1/ff.gifv")


def test_is_gif_video_mp4_with_gifv_url_segment() -> None:
    # Mobile uploads sometimes lose the extension; URL still says `gifv`.
    assert is_gif("video/mp4", "loop", "https://cdn.discordapp.com/a/1/clip.gifv")


def test_is_gif_giphy_url() -> None:
    # Tenor/Giphy embeds: video/mp4 mime, no .gif filename, but URL host gives it away.
    assert is_gif("video/mp4", "loop.mp4", "https://i.giphy.com/media/abc/giphy.mp4")


def test_is_gif_negative_real_mp4_video() -> None:
    assert not is_gif(
        "video/mp4", "tutorial.mp4", "https://cdn.discordapp.com/a/1/tutorial.mp4"
    )


def test_is_gif_negative_png() -> None:
    assert not is_gif("image/png", "screenshot.png", "https://cdn.discordapp.com/a/1/x.png")


def test_is_gif_handles_none_inputs() -> None:
    # All-None must not crash; returns False.
    assert not is_gif(None, None, None)


# ---------------------------------------------------------------------------
# is_video — positives + negatives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ct,name",
    [
        ("video/mp4", "tutorial.mp4"),
        ("video/quicktime", "demo.mov"),
        ("video/webm", "screen.webm"),
        ("application/octet-stream", "x.mp4"),  # filename overrides bad MIME
        (None, "x.m4v"),
    ],
)
def test_is_video_positive(ct: str | None, name: str) -> None:
    assert is_video(ct, name)


def test_is_video_negative_png() -> None:
    assert not is_video("image/png", "x.png")


def test_is_video_negative_text_file() -> None:
    assert not is_video("text/plain", "readme.txt")


def test_is_video_negative_none_inputs() -> None:
    assert not is_video(None, None)


# ---------------------------------------------------------------------------
# VideoTriageWriter
# ---------------------------------------------------------------------------


def _make_message(msg_id: str = "m1", reactions: int = 0, pinned: bool = False) -> Message:
    return Message(
        guild_id="g1",
        channel_id="c1",
        channel_name="general",
        message_id=msg_id,
        author_id="u1",
        author_name="alice",
        content="check this clip",
        timestamp="2026-04-26T00:00:00+00:00",
        reactions=[
            *([] if reactions == 0 else [{"emoji": "fire", "count": reactions}]),
        ],
        pinned=pinned,
    )


def _make_attachment(att_id: str = "a1", size: int = 1234) -> Attachment:
    return Attachment(
        id=att_id,
        filename="clip.mp4",
        content_type="video/mp4",
        size=size,
        cdn_url="https://cdn.discordapp.com/a/1/clip.mp4",
    )


def _make_channel() -> Channel:
    return Channel(id="c1", type=0, name="general", guild_id="g1", position=0)


def test_writer_append_and_flush(tmp_path: Path) -> None:
    writer = VideoTriageWriter(output_root=tmp_path / "output", guild_id="g1", date_str="2026-04-26")
    writer.append_record(
        message=_make_message(),
        attachment=_make_attachment(),
        channel=_make_channel(),
        guild_id="g1",
    )
    assert writer.record_count == 1

    path = writer.flush()
    assert path is not None
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    # Schema sanity
    assert record["schema_version"] == 1
    assert record["guild_id"] == "g1"
    assert record["channel_id"] == "c1"
    assert record["filename"] == "clip.mp4"
    assert record["content_type"] == "video/mp4"
    # Confirm canonical key order (alphabetical)
    keys = list(record.keys())
    assert keys == sorted(keys), keys


def test_writer_flush_empty_returns_none(tmp_path: Path) -> None:
    writer = VideoTriageWriter(output_root=tmp_path / "output", guild_id="g1", date_str="2026-04-26")
    assert writer.flush() is None
    # No JSONL file created when nothing was buffered.
    assert not (tmp_path / "output" / "g1" / "2026-04-26" / "video-triage.jsonl").exists()


def test_writer_flush_atomic_via_tmp_rename(tmp_path: Path) -> None:
    writer = VideoTriageWriter(output_root=tmp_path / "output", guild_id="g1", date_str="2026-04-26")
    writer.append_record(
        message=_make_message(),
        attachment=_make_attachment(),
        channel=_make_channel(),
        guild_id="g1",
    )
    path = writer.flush()
    assert path is not None
    # No leftover .tmp after rename.
    assert not path.with_suffix(path.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# Markdown view
# ---------------------------------------------------------------------------


def test_markdown_renders_high_medium_low_buckets(tmp_path: Path) -> None:
    writer = VideoTriageWriter(output_root=tmp_path / "output", guild_id="g1", date_str="2026-04-26")
    # high — pinned
    writer.append_record(
        message=_make_message(msg_id="m_pinned", pinned=True),
        attachment=_make_attachment(att_id="a_pinned"),
        channel=_make_channel(),
        guild_id="g1",
    )
    # high — many reactions
    writer.append_record(
        message=_make_message(msg_id="m_many_rxn", reactions=15),
        attachment=_make_attachment(att_id="a_many_rxn"),
        channel=_make_channel(),
        guild_id="g1",
    )
    # medium — moderate reactions
    writer.append_record(
        message=_make_message(msg_id="m_mid_rxn", reactions=3),
        attachment=_make_attachment(att_id="a_mid_rxn"),
        channel=_make_channel(),
        guild_id="g1",
    )
    # medium — large file
    writer.append_record(
        message=_make_message(msg_id="m_big"),
        attachment=_make_attachment(att_id="a_big", size=10 * 1024 * 1024),
        channel=_make_channel(),
        guild_id="g1",
    )
    # low — nothing notable
    writer.append_record(
        message=_make_message(msg_id="m_low"),
        attachment=_make_attachment(att_id="a_low"),
        channel=_make_channel(),
        guild_id="g1",
    )
    jsonl_path = writer.flush()
    assert jsonl_path is not None

    md_path = jsonl_path.with_suffix(".md")
    n = write_video_triage_md(jsonl_path, md_path)
    assert n == 5

    md_text = md_path.read_text(encoding="utf-8")
    assert "# Video triage" in md_text
    assert "## general" in md_text
    assert "### high signal (2)" in md_text
    assert "### medium signal (2)" in md_text
    assert "### low signal (1)" in md_text


def test_markdown_skips_when_no_jsonl(tmp_path: Path) -> None:
    n = write_video_triage_md(tmp_path / "missing.jsonl", tmp_path / "out.md")
    assert n == 0
    assert not (tmp_path / "out.md").exists()


# ---------------------------------------------------------------------------
# Integration: scan one channel with PNG + GIF + MP4
# ---------------------------------------------------------------------------


def _raw_msg_with_attachments(msg_id: str, attachments: list[dict]) -> dict:
    return {
        "id": msg_id,
        "author": {"id": "u1", "username": "alice"},
        "timestamp": "2026-04-26T00:00:00+00:00",
        "content": "mixed media",
        "pinned": False,
        "flags": 0,
        "attachments": attachments,
    }


@pytest.mark.asyncio
async def test_integration_png_downloaded_gif_skipped_mp4_triaged(
    tmp_config_yaml_no_gateway: Path,
) -> None:
    cfg = load_config(tmp_config_yaml_no_gateway)
    cfg.http.per_channel_delay_sec = (0.001, 0.002)
    cfg.http.burst_pause_sec = (0.001, 0.002)

    PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

    guild_id = "g_v3_mix"
    channel_id = "c1"

    msg = _raw_msg_with_attachments(
        "9001",
        [
            {
                "id": "att_png",
                "filename": "snap.png",
                "url": "https://cdn.discordapp.com/a/1/snap.png",
                "content_type": "image/png",
                "size": 64,
            },
            {
                "id": "att_gif",
                "filename": "meme.gif",
                "url": "https://cdn.discordapp.com/a/1/meme.gif",
                "content_type": "image/gif",
                "size": 4096,
            },
            {
                "id": "att_mp4",
                "filename": "tutorial.mp4",
                "url": "https://cdn.discordapp.com/a/1/tutorial.mp4",
                "content_type": "video/mp4",
                "size": 1024 * 1024,
            },
        ],
    )

    with respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(200, json=[{"id": guild_id, "name": "G"}])
        )
        mock.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels").mock(
            return_value=httpx.Response(
                200, json=[{"id": channel_id, "type": 0, "name": "general"}]
            )
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/messages").mock(
            return_value=httpx.Response(200, json=[msg])
        )
        mock.get(f"https://discord.com/api/v10/channels/{channel_id}/pins").mock(
            return_value=httpx.Response(200, json=[])
        )
        mock.get("https://cdn.discordapp.com/a/1/snap.png").mock(
            return_value=httpx.Response(200, content=PNG_HEADER + b"data")
        )
        # GIF + MP4 URLs are NEVER fetched — assert by absence (respx will
        # report unmatched if they're called).

        results = await run_one_pass(
            cfg, SecretStr("TEST_TOKEN"), scan_date="2026-04-26"
        )

    chan = results[0].channel_results[0]
    assert chan.attachments_downloaded == 1, "only PNG should download"
    assert chan.video_attachments_triaged == 1, "MP4 in triage"
    # GIF: silent-skipped, NOT counted in any field.

    out_root = cfg.run.output_root / guild_id / "2026-04-26"

    # PNG present in attachments/
    png_path = out_root / "attachments" / "9001_snap.png"
    assert png_path.exists()

    # GIF NOT downloaded
    gif_path = out_root / "attachments" / "9001_meme.gif"
    assert not gif_path.exists()

    # MP4 NOT downloaded
    mp4_path = out_root / "attachments" / "9001_tutorial.mp4"
    assert not mp4_path.exists()

    # MP4 in triage JSONL
    triage_jsonl = out_root / "video-triage.jsonl"
    assert triage_jsonl.exists()
    lines = triage_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["filename"] == "tutorial.mp4"
    assert rec["content_type"] == "video/mp4"

    # Markdown sidecar generated
    triage_md = out_root / "video-triage.md"
    assert triage_md.exists()
    md = triage_md.read_text(encoding="utf-8")
    assert "tutorial.mp4" in md
