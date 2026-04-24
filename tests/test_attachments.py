"""Tests for attachment security — SEC-P0-19 (MIME), SEC-P0-20 (size),
SEC-P0-21 (filename + realpath), SEC-P0-22 (chmod).

Floor: ≥70% on `fetch/attachments.py`, `fetch/mime.py`, `fetch/filename.py`.
These modules are high-risk (external bytes → disk) so we over-cover on
edge cases: wrong MIME, oversize, traversal, reserved chars, null bytes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
import respx

from discord_scanner.fetch.attachments import (
    MIME_SNIFF_BYTES,
    DownloadResult,
    download_attachment,
)
from discord_scanner.fetch.filename import (
    MAX_FILENAME_LEN,
    PathEscapeError,
    assert_within_output_root,
    sanitise_filename,
)
from discord_scanner.fetch.mime import (
    declared_extension_matches,
    detect_image_type,
)

# ----------------------------------------------------------------------
# Magic-byte sniffing (SEC-P0-19)
# ----------------------------------------------------------------------

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 12
GIF87 = b"GIF87a" + b"\x00" * 10
GIF89 = b"GIF89a" + b"\x00" * 10
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 "
PE_HEADER = b"MZ\x90\x00" + b"\x00" * 12  # Windows executable


def test_detect_png() -> None:
    assert detect_image_type(PNG_HEADER) == ".png"


def test_detect_jpeg() -> None:
    assert detect_image_type(JPEG_HEADER) == ".jpg"


def test_detect_gif87() -> None:
    assert detect_image_type(GIF87) == ".gif"


def test_detect_gif89() -> None:
    assert detect_image_type(GIF89) == ".gif"


def test_detect_webp() -> None:
    assert detect_image_type(WEBP) == ".webp"


def test_detect_unknown_returns_none() -> None:
    assert detect_image_type(PE_HEADER) is None
    assert detect_image_type(b"random bytes 123456") is None


def test_detect_too_short_returns_none() -> None:
    assert detect_image_type(b"ab") is None
    assert detect_image_type(b"") is None


def test_declared_extension_matches_jpeg_alias() -> None:
    """`.jpeg` and `.jpg` are equivalent."""
    assert declared_extension_matches("photo.jpeg", ".jpg")
    assert declared_extension_matches("photo.JPG", ".jpg")


def test_declared_extension_mismatch() -> None:
    assert not declared_extension_matches("malware.png", ".jpg")
    assert not declared_extension_matches("no_ext", ".png")
    assert not declared_extension_matches("photo.png", None)


# ----------------------------------------------------------------------
# Filename sanitisation (SEC-P0-21)
# ----------------------------------------------------------------------


def test_sanitise_happy_path() -> None:
    assert sanitise_filename("photo.png", "123") == "123_photo.png"


def test_sanitise_null_byte_stripped() -> None:
    assert sanitise_filename("bad\x00file.png", "42") == "42_badfile.png"


def test_sanitise_traversal_rejected() -> None:
    """Malicious `../../etc/passwd` → `{msg_id}_passwd` in attachments dir."""
    out = sanitise_filename("../../etc/passwd", "42")
    assert out == "42_passwd"


def test_sanitise_windows_backslash_traversal() -> None:
    """Backslash path separators get replaced with `_` (forbidden char rule).

    PurePosixPath.name treats the whole string as one name (no `/` split);
    then the forbidden-char pass replaces `\\` with `_`. The `..` substrings
    that remain are harmless — they are inside the filename, not a path
    separator — and the realpath containment check downstream guarantees
    the write never escapes `output_root`.
    """
    out = sanitise_filename("..\\..\\Windows\\notepad.exe", "42")
    assert "\\" not in out
    assert out.startswith("42_")
    # No separator chars — the file cannot escape the attachments dir.
    assert "/" not in out
    assert "\\" not in out


def test_sanitise_forbidden_chars_replaced() -> None:
    out = sanitise_filename('evil<>:"|?*name.png', "42")
    for ch in '<>:"|?*':
        assert ch not in out
    assert out.startswith("42_")
    assert out.endswith(".png")


def test_sanitise_control_chars_replaced() -> None:
    out = sanitise_filename("ctrl\x01\x02\x1fchar.png", "42")
    assert out == "42_ctrl___char.png"


def test_sanitise_empty_name_becomes_attachment() -> None:
    out = sanitise_filename("", "42")
    assert out == "42_attachment"


def test_sanitise_length_cap_respected() -> None:
    long_name = "a" * 300 + ".png"
    out = sanitise_filename(long_name, "42")
    assert len(out) <= MAX_FILENAME_LEN


def test_sanitise_leading_dot_stripped() -> None:
    """`.hidden` on POSIX would make the file hidden; strip leading dot."""
    out = sanitise_filename(".hidden_config", "42")
    assert not out.startswith("42_.")


# ----------------------------------------------------------------------
# Realpath containment (SEC-P0-21)
# ----------------------------------------------------------------------


def test_assert_within_output_root_accepts_inside(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    target = root / "g" / "2026-01-01" / "attachments" / "x.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    assert_within_output_root(target, root)  # must not raise


def test_assert_within_output_root_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    outside = tmp_path / "etc" / "passwd"
    outside.parent.mkdir()
    outside.write_bytes(b"root:x:0:0")
    with pytest.raises(PathEscapeError):
        assert_within_output_root(outside, root)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require perms on Windows")
def test_assert_within_output_root_rejects_symlink_escape(tmp_path: Path) -> None:
    """Symlink inside output_root pointing outside → realpath rejects it."""
    root = tmp_path / "output"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret"
    outside_file.write_bytes(b"secret")
    sym = root / "attach"
    sym.symlink_to(outside_file)
    with pytest.raises(PathEscapeError):
        assert_within_output_root(sym, root)


# ----------------------------------------------------------------------
# download_attachment — end-to-end with respx
# ----------------------------------------------------------------------


def _make_output_root(tmp_path: Path) -> Path:
    p = tmp_path / "output"
    p.mkdir()
    return p


@pytest.mark.asyncio
async def test_download_happy_path_png(tmp_path: Path) -> None:
    """Valid PNG → saved, 0o600 on POSIX, reason == ok."""
    body = PNG_HEADER + b"rest-of-png-bytes" * 10
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/photo.png").mock(
            return_value=httpx.Response(200, content=body)
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/photo.png",
            raw_filename="photo.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
            max_size_mb=1,
        )
    assert result.reason == "ok"
    assert result.saved_path is not None
    assert result.saved_path.exists()
    assert result.saved_path.name == "42_photo.png"
    assert result.saved_path.read_bytes() == body


@pytest.mark.asyncio
async def test_download_mime_mismatch_discarded(tmp_path: Path) -> None:
    """SEC-P0-19: `.png` extension but PE (MZ) body → discarded, URL-only."""
    body = PE_HEADER + b"rest"
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/trojan.png").mock(
            return_value=httpx.Response(200, content=body)
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/trojan.png",
            raw_filename="trojan.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
            max_size_mb=1,
        )
    assert result.reason == "mime_mismatch"
    assert result.saved_path is None
    # No file landed in attachments/
    leftover = list((out_root / "g1" / "2026-04-24" / "attachments").glob("*"))
    assert leftover == []


@pytest.mark.asyncio
async def test_download_oversize_aborts_and_cleans(tmp_path: Path) -> None:
    """SEC-P0-20: stream overflows size cap → aborted, partial deleted."""
    # Build a 2 MB PNG body; cap at max_size_mb=1 → cap = 1.1 MB
    body = PNG_HEADER + b"\x00" * (2 * 1024 * 1024)
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/huge.png").mock(
            return_value=httpx.Response(200, content=body)
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/huge.png",
            raw_filename="huge.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
            max_size_mb=1,
        )
    assert result.reason == "oversize"
    assert result.saved_path is None
    # Partial file MUST be cleaned up
    assert not (out_root / "g1" / "2026-04-24" / "attachments" / "42_huge.png").exists()


@pytest.mark.asyncio
async def test_download_http_404(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/gone.png").mock(
            return_value=httpx.Response(404)
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/gone.png",
            raw_filename="gone.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
        )
    assert result.reason == "http_error"


@pytest.mark.asyncio
async def test_download_forbidden_extension(tmp_path: Path) -> None:
    """`.exe` (or any non-image) is rejected BEFORE network."""
    async with httpx.AsyncClient() as client, respx.mock(assert_all_called=False) as mock:
        _ = mock
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/bad.exe",
            raw_filename="bad.exe",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
        )
    assert result.reason == "ext_forbidden"
    # Zero HTTP calls made (we rejected pre-network)
    assert mock.calls.call_count == 0


@pytest.mark.asyncio
async def test_download_traversal_name_is_sanitised_not_escaping(
    tmp_path: Path,
) -> None:
    """Malicious filename `../../etc/passwd.png` saved as `{msg_id}_passwd.png`
    inside attachments dir, not at `/etc/passwd.png`."""
    body = PNG_HEADER + b"rest"
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/evil.png").mock(
            return_value=httpx.Response(200, content=body)
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/evil.png",
            raw_filename="../../etc/passwd.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
        )
    assert result.reason == "ok"
    assert result.saved_path is not None
    assert result.saved_path.name == "42_passwd.png"
    # And it's physically inside out_root
    assert str(result.saved_path).startswith(str(out_root))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only perm bits")
@pytest.mark.asyncio
async def test_download_sets_0o600_on_posix(tmp_path: Path) -> None:
    body = PNG_HEADER + b"rest"
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/photo.png").mock(
            return_value=httpx.Response(200, content=body)
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/photo.png",
            raw_filename="photo.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
        )
    assert result.saved_path is not None
    mode = os.stat(result.saved_path).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_mime_sniff_bytes_constant() -> None:
    assert MIME_SNIFF_BYTES == 16


@pytest.mark.asyncio
async def test_download_transport_error_cleans_up(tmp_path: Path) -> None:
    """httpx.ConnectError mid-stream → no leftover file."""
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/boom.png").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/boom.png",
            raw_filename="boom.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
        )
    assert result.reason == "http_error"
    assert result.saved_path is None
    leftover = list((out_root / "g1" / "2026-04-24" / "attachments").glob("*"))
    assert leftover == []


@pytest.mark.asyncio
async def test_download_tiny_body_under_16_bytes_mime_mismatch(tmp_path: Path) -> None:
    """Stream ends before 16 bytes: sniff runs on the partial buffer. If it
    doesn't match (or too-short), → mime_mismatch + no file."""
    body = b"\x89PN"  # 3 bytes, not enough to match PNG
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/tiny.png").mock(
            return_value=httpx.Response(200, content=body)
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/tiny.png",
            raw_filename="tiny.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
        )
    assert result.reason == "mime_mismatch"
    assert result.saved_path is None


@pytest.mark.asyncio
async def test_download_empty_body(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/empty.png").mock(
            return_value=httpx.Response(200, content=b"")
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/empty.png",
            raw_filename="empty.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
        )
    assert result.reason == "mime_mismatch"


@pytest.mark.asyncio
async def test_download_bytes_read_counter(tmp_path: Path) -> None:
    """Happy-path `bytes_read` reflects actual payload size."""
    body = PNG_HEADER + b"X" * 1000
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://cdn.discordapp.com/attach/small.png").mock(
            return_value=httpx.Response(200, content=body)
        )
        out_root = _make_output_root(tmp_path)
        result = await download_attachment(
            client,
            cdn_url="https://cdn.discordapp.com/attach/small.png",
            raw_filename="small.png",
            msg_id="42",
            guild_id="g1",
            date_str="2026-04-24",
            output_root=out_root,
        )
    assert result.reason == "ok"
    assert result.bytes_read == len(body)


def test_download_result_dataclass_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    r = DownloadResult(saved_path=None, reason="ok", bytes_read=10, cdn_url="x")
    with pytest.raises(FrozenInstanceError):
        r.reason = "mime_mismatch"  # type: ignore[misc]
