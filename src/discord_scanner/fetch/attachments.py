"""Streaming attachment download: MIME sniff → size cap → sanitised path → chmod.

Traces to:
- seed-spec.md §2.4 (attachments)
- security-model.md §6 SEC-P0-19 (MIME sniff), SEC-P0-20 (size cap),
  SEC-P0-21 (filename sanitise + realpath), SEC-P0-22 (chmod 0o600)
- claude-rules.md MUST "Attachment security"

Protocol:
1. Validate the declared filename has an allowed image extension
2. Open a streaming response
3. Read first 16 bytes; run `detect_image_type`; if mismatch → discard and
   record URL only (WARNING)
4. Continue reading up to `max_size_mb * 1.1` bytes; abort on overflow and
   delete the partial file
5. Sanitize filename via `sanitise_filename(raw_name, msg_id)`
6. Write under `output/{guild_id}/{date}/attachments/` with the sanitised name
7. `assert_within_output_root` on the final realpath
8. `os.chmod(path, 0o600)` best-effort

Authorization MUST NOT be sent to `cdn.discordapp.com` — the REST client's
`_strip_auth_on_cdn` hook enforces this.
"""

from __future__ import annotations

import io  # noqa: F401 — used in type annotation via string
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from discord_scanner.fetch.filename import (
    PathEscapeError,
    assert_within_output_root,
    sanitise_filename,
)
from discord_scanner.fetch.mime import (
    declared_extension_matches,
    detect_image_type,
)
from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)

MIME_SNIFF_BYTES = 16


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of a single attachment download."""

    saved_path: Path | None  # populated only on full success
    reason: str  # "ok", "mime_mismatch", "oversize", "http_error", "ext_forbidden", "path_escape"
    bytes_read: int
    cdn_url: str


async def download_attachment(
    client: httpx.AsyncClient,
    *,
    cdn_url: str,
    raw_filename: str,
    msg_id: str,
    guild_id: str,
    date_str: str,
    output_root: Path,
    max_size_mb: int = 20,
    allowed_extensions: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".gif"),
) -> DownloadResult:
    """Download one attachment with full SEC-P0-19/20/21/22 enforcement.

    Returns a `DownloadResult` describing the outcome. Never raises for
    expected security rejections (mime mismatch, oversize, path escape) —
    the caller records the URL-only fallback and moves on.
    """
    size_cap = int(max_size_mb * 1024 * 1024 * 1.1)  # SEC-P0-20: 1.1x cap
    declared_lower = raw_filename.lower()
    if not any(declared_lower.endswith(ext) for ext in allowed_extensions):
        logger.warning("attachment_ext_forbidden", filename=raw_filename, msg_id=msg_id)
        return DownloadResult(
            saved_path=None, reason="ext_forbidden", bytes_read=0, cdn_url=cdn_url
        )

    attachments_dir = output_root / guild_id / date_str / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(attachments_dir, 0o700)
    except OSError as e:
        logger.debug("chmod_dir_best_effort_failed", path=str(attachments_dir), err=str(e))

    safe_name = sanitise_filename(raw_filename, msg_id)
    target_path = attachments_dir / safe_name

    # Best-effort realpath containment BEFORE any bytes hit disk, so even
    # a sanitisation bug can't write outside output_root.
    try:
        assert_within_output_root(target_path, output_root)
    except PathEscapeError as e:
        logger.error("attachment_path_escape", err=str(e), msg_id=msg_id)
        return DownloadResult(saved_path=None, reason="path_escape", bytes_read=0, cdn_url=cdn_url)

    try:
        async with client.stream("GET", cdn_url) as resp:
            if resp.status_code >= 400:
                logger.warning(
                    "attachment_http_error",
                    status=resp.status_code,
                    msg_id=msg_id,
                    cdn_url=cdn_url,
                )
                return DownloadResult(
                    saved_path=None,
                    reason="http_error",
                    bytes_read=0,
                    cdn_url=cdn_url,
                )
            return await _stream_to_disk(
                resp,
                target_path=target_path,
                raw_filename=raw_filename,
                size_cap=size_cap,
                msg_id=msg_id,
                cdn_url=cdn_url,
            )
    except httpx.HTTPError as e:
        logger.warning("attachment_transport_error", err=str(e), msg_id=msg_id)
        _delete_partial(target_path)
        return DownloadResult(saved_path=None, reason="http_error", bytes_read=0, cdn_url=cdn_url)


async def _stream_to_disk(
    resp: httpx.Response,
    *,
    target_path: Path,
    raw_filename: str,
    size_cap: int,
    msg_id: str,
    cdn_url: str,
) -> DownloadResult:
    """Inner loop — MIME-sniff first 16 bytes, then stream with size cap."""
    bytes_read = 0
    sniff_buffer = b""
    sniff_done = False

    # Open file only AFTER we have a valid MIME sniff — avoids creating an
    # empty file on mismatch. Keep ONE handle open across all chunks (reviewer
    # MAJOR: per-chunk reopen defeats OS buffering).
    fh: Path | None = None
    out_fh: io.BufferedWriter | None = None
    try:
        async for chunk in resp.aiter_bytes():
            if not sniff_done:
                sniff_buffer += chunk
                if len(sniff_buffer) >= MIME_SNIFF_BYTES:
                    sniffed = detect_image_type(sniff_buffer[:MIME_SNIFF_BYTES])
                    if not declared_extension_matches(raw_filename, sniffed):
                        logger.warning(
                            "attachment_mime_mismatch",
                            declared=raw_filename,
                            sniffed=sniffed,
                            msg_id=msg_id,
                        )
                        return DownloadResult(
                            saved_path=None,
                            reason="mime_mismatch",
                            bytes_read=len(sniff_buffer),
                            cdn_url=cdn_url,
                        )
                    sniff_done = True
                    bytes_read = len(sniff_buffer)
                    # SEC-P0-20: cap check MUST cover the first chunk too —
                    # the stream can deliver the entire body in one chunk
                    # (respx does this by default), so we can't only check
                    # on subsequent chunks.
                    if bytes_read > size_cap:
                        logger.warning(
                            "attachment_oversize",
                            bytes_read=bytes_read,
                            cap=size_cap,
                            msg_id=msg_id,
                        )
                        return DownloadResult(
                            saved_path=None,
                            reason="oversize",
                            bytes_read=bytes_read,
                            cdn_url=cdn_url,
                        )
                    # Flush the buffered sniff data out to the real file.
                    # We keep the handle open to append later chunks.
                    out_fh = target_path.open("wb")
                    out_fh.write(sniff_buffer)
                    fh = target_path
                    sniff_buffer = b""
                    continue
                # not enough bytes yet — keep buffering
                continue

            # Past the sniff gate — append each chunk, enforce cap
            bytes_read += len(chunk)
            if bytes_read > size_cap:
                logger.warning(
                    "attachment_oversize",
                    bytes_read=bytes_read,
                    cap=size_cap,
                    msg_id=msg_id,
                )
                if out_fh is not None:
                    out_fh.close()
                    out_fh = None
                _delete_partial(target_path)
                return DownloadResult(
                    saved_path=None,
                    reason="oversize",
                    bytes_read=bytes_read,
                    cdn_url=cdn_url,
                )
            if out_fh is not None:
                out_fh.write(chunk)

        # Stream ended. If we never reached sniff gate (tiny image < 16 B),
        # run a best-effort sniff on whatever we have.
        if not sniff_done:
            if not sniff_buffer:
                logger.warning("attachment_empty_body", msg_id=msg_id)
                return DownloadResult(
                    saved_path=None,
                    reason="mime_mismatch",
                    bytes_read=0,
                    cdn_url=cdn_url,
                )
            sniffed = detect_image_type(sniff_buffer)
            if not declared_extension_matches(raw_filename, sniffed):
                return DownloadResult(
                    saved_path=None,
                    reason="mime_mismatch",
                    bytes_read=len(sniff_buffer),
                    cdn_url=cdn_url,
                )
            with target_path.open("wb") as out:
                out.write(sniff_buffer)
            fh = target_path
            bytes_read = len(sniff_buffer)

        if fh is not None:
            try:
                os.chmod(fh, 0o600)
            except OSError as e:
                logger.debug("chmod_best_effort_failed", path=str(fh), err=str(e))
        return DownloadResult(saved_path=fh, reason="ok", bytes_read=bytes_read, cdn_url=cdn_url)
    except Exception:
        _delete_partial(target_path)
        raise


def _delete_partial(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        logger.debug("partial_delete_failed", path=str(path), err=str(e))
