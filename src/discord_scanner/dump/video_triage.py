"""Video triage — silent GIF skip + non-GIF video metadata sidecar.

Stage 2 v3 / multi-burner addendum (operator decision 2026-04-26):

- **GIFs** in attachments are noise (memes, joke reactions). Silent-skip
  in the attachment downloader: not in `attachments/`, not in
  `video-triage.jsonl`, not in any log line. Stage 3 must NOT extract
  assets from `embeds[].type == "gifv"` either (handled by the curator
  judge prompt — see `discord-curator-bootstrap/foundation_prefill.md`).
- **Non-GIF videos** (`.mp4`, `.mov`, `.webm`, etc.) are triage-worthy
  evidence: workflow demos, reaction tutorials, behind-the-scenes camera
  clips. We never download the video file (CLAUDE.md MUST NOT "model-
  weight or binary-blob downloads") — instead we append a metadata-only
  record to `output/{guild_id}/{date}/video-triage.jsonl` so the Stage 3
  curator can decide whether to surface a `video_evidence` signal in the
  Obsidian vault.

The sidecar JSONL holds enough context (channel, message, reactions,
file size, content_type, CDN URL) for human or LLM triage without ever
reading the video bytes. A markdown view (`video-triage.md`) is generated
post-scan for at-a-glance operator review.

Design rules:
- `is_gif` and `is_video` are pure static functions — covered by unit
  tests with positive AND negative cases (the regex/string tests are
  cheap and protect against future content_type drift).
- Atomic JSONL append via `tmp + rename` to keep the file consistent if
  the scan crashes mid-write.
- Markdown view is grouped by `channel_name` then bucketed by signal
  level (`high` / `medium` / `low`) using a simple heuristic over
  reactions count + file size; tunable later without schema break.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord_scanner._paths import secure_mkdir
from discord_scanner.logging_conf import get_logger
from discord_scanner.models.discord import Channel
from discord_scanner.models.message import Attachment, Message

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# MIME / extension detection — pure functions
# ---------------------------------------------------------------------------


_GIF_EXTS = (".gif", ".gifv")
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v", ".mpeg4")


def is_gif(content_type: str | None, filename: str | None, cdn_url: str | None) -> bool:
    """Return True if the attachment is a GIF / GIFV in any of three forms.

    Discord serves GIFs in three different shapes:
      1. `content_type == "image/gif"` + `filename ends with .gif`
      2. `content_type == "video/mp4"` + `filename ends with .gifv`
         (Discord transcodes GIFs to mp4 for autoplay)
      3. CDN URL has `gifv` segment even when `content_type` claims
         `video/mp4` and the filename omits the extension entirely
         (mobile uploads sometimes do this)

    All three forms must be silent-skipped per operator decision
    2026-04-26 — they're memes/noise, not workflow evidence.
    """
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    url = (cdn_url or "").lower()
    if "image/gif" in ct:
        return True
    if any(name.endswith(e) for e in _GIF_EXTS):
        return True
    if "/gifv" in url or url.endswith(".gifv") or "/giphy" in url or "/tenor" in url:
        return True
    return False


def is_video(content_type: str | None, filename: str | None) -> bool:
    """Return True iff the attachment is a non-GIF video.

    GIFs are EXCLUDED here even if Discord's `content_type` is
    `video/mp4` — `is_gif` must be checked first. The orchestrator
    enforces this ordering: `is_gif` returns true → silent skip;
    otherwise check `is_video` → triage record.
    """
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    if any(name.endswith(e) for e in _VIDEO_EXTS):
        return True
    if ct.startswith("video/"):
        # Already past `is_gif` — `image/gif` would have returned earlier.
        return True
    return False


# ---------------------------------------------------------------------------
# JSONL appender
# ---------------------------------------------------------------------------


@dataclass
class VideoTriageRecord:
    """One row in `video-triage.jsonl`. All fields are optional metadata —
    we never store binary data."""

    schema_version: int
    guild_id: str
    channel_id: str
    channel_name: str | None
    message_id: str
    author_id: str
    author_name: str
    timestamp: str
    content_excerpt: str  # first 280 chars
    attachment_id: str
    filename: str
    content_type: str | None
    size_bytes: int | None
    cdn_url: str
    reactions_total: int
    pinned: bool

    def to_canonical(self) -> dict[str, Any]:
        """Return a dict with alphabetised keys for deterministic JSONL."""
        return {
            "attachment_id": self.attachment_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "cdn_url": self.cdn_url,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "content_excerpt": self.content_excerpt,
            "content_type": self.content_type,
            "filename": self.filename,
            "guild_id": self.guild_id,
            "message_id": self.message_id,
            "pinned": self.pinned,
            "reactions_total": self.reactions_total,
            "schema_version": self.schema_version,
            "size_bytes": self.size_bytes,
            "timestamp": self.timestamp,
        }


class VideoTriageWriter:
    """Append-only JSONL writer for non-GIF video metadata.

    One writer instance per scan run; flushed at scan teardown. The file is
    written atomically per record via tmp+rename — a mid-scan crash leaves
    the previous valid state intact.

    `output_path` is computed lazily on first record to avoid creating an
    empty file when no videos were found.
    """

    def __init__(self, output_root: Path, guild_id: str, date_str: str) -> None:
        self._output_root = output_root
        self._guild_id = guild_id
        self._date_str = date_str
        self._records: list[VideoTriageRecord] = []
        self._path: Path | None = None

    @property
    def path(self) -> Path:
        """Resolve the JSONL path on demand. Does not create the file."""
        if self._path is None:
            self._path = (
                self._output_root / self._guild_id / self._date_str / "video-triage.jsonl"
            )
        return self._path

    @property
    def record_count(self) -> int:
        return len(self._records)

    def append_record(
        self,
        *,
        message: Message,
        attachment: Attachment,
        channel: Channel,
        guild_id: str,
    ) -> None:
        """Buffer one record. Flushed via `flush()` at scan teardown."""
        reactions_total = sum(r.count for r in message.reactions)
        excerpt = (message.content or "")[:280]
        self._records.append(
            VideoTriageRecord(
                schema_version=1,
                guild_id=guild_id,
                channel_id=channel.id,
                channel_name=channel.name,
                message_id=message.message_id,
                author_id=message.author_id,
                author_name=message.author_name,
                timestamp=message.timestamp,
                content_excerpt=excerpt,
                attachment_id=attachment.id,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size_bytes=attachment.size,
                cdn_url=attachment.cdn_url or "",
                reactions_total=reactions_total,
                pinned=message.pinned,
            )
        )

    def flush(self) -> Path | None:
        """Write buffered records to disk atomically. No-op if empty.

        Returns the JSONL path on success, or None if nothing was written.
        """
        if not self._records:
            return None
        # Stable order: (channel_id, timestamp, message_id) — same key as
        # messages.jsonl.zst per CLAUDE.md MUST "sort stability".
        records_sorted = sorted(
            self._records,
            key=lambda r: (r.channel_id, r.timestamp, r.message_id, r.attachment_id),
        )
        path = self.path
        secure_mkdir(path.parent)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            for rec in records_sorted:
                line = json.dumps(
                    rec.to_canonical(),
                    allow_nan=False,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                fh.write(line + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as e:
                logger.debug("video_triage_fsync_failed", err=str(e))
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError as e:
            logger.debug("chmod_best_effort_failed", path=str(path), err=str(e))
        logger.info(
            "video_triage_written",
            path=str(path),
            count=len(records_sorted),
        )
        return path


# ---------------------------------------------------------------------------
# Markdown view
# ---------------------------------------------------------------------------


def _signal_bucket(record: dict[str, Any]) -> str:
    """Heuristic signal level for triage prioritisation.

    Tunable; current rule:
      - high: reactions_total >= 10 OR pinned
      - medium: reactions_total >= 3 OR size_bytes >= 5 MB
      - low: everything else
    """
    if record.get("pinned"):
        return "high"
    reactions = int(record.get("reactions_total") or 0)
    size = int(record.get("size_bytes") or 0)
    if reactions >= 10:
        return "high"
    if reactions >= 3 or size >= 5 * 1024 * 1024:
        return "medium"
    return "low"


def write_video_triage_md(triage_jsonl_path: Path, output_md_path: Path) -> int:
    """Generate a markdown view from the JSONL.

    Returns the number of records rendered. The markdown is grouped by
    `channel_name` (descending by record count) and bucketed
    high/medium/low using `_signal_bucket`. Operator-friendly: no JSON,
    no IDs unless they're the only identifying info.
    """
    if not triage_jsonl_path.exists():
        return 0
    records: list[dict[str, Any]] = []
    with triage_jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("video_triage_md_parse_failed", err=str(e))
                continue
    if not records:
        return 0

    by_channel: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        key = r.get("channel_name") or r.get("channel_id") or "<unknown>"
        by_channel.setdefault(key, []).append(r)

    buf = io.StringIO()
    buf.write("# Video triage\n\n")
    buf.write(f"_Total: {len(records)} non-GIF video(s)._\n\n")

    # Sort channels by descending record count for at-a-glance prioritisation.
    channel_order = sorted(by_channel.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for channel_name, items in channel_order:
        buf.write(f"## {channel_name}\n\n")
        bucketed: dict[str, list[dict[str, Any]]] = {"high": [], "medium": [], "low": []}
        for r in items:
            bucketed[_signal_bucket(r)].append(r)
        for bucket in ("high", "medium", "low"):
            entries = bucketed[bucket]
            if not entries:
                continue
            buf.write(f"### {bucket} signal ({len(entries)})\n\n")
            for r in entries:
                ts = r.get("timestamp", "")
                author = r.get("author_name", "")
                fname = r.get("filename", "")
                size = r.get("size_bytes")
                size_mb = f"{int(size) / (1024 * 1024):.1f}MB" if size else "?"
                rxn = r.get("reactions_total", 0)
                excerpt = (r.get("content_excerpt") or "").replace("\n", " ").strip()
                if len(excerpt) > 120:
                    excerpt = excerpt[:117] + "..."
                buf.write(
                    f"- `{ts}` **{author}** — {fname} ({size_mb}, {rxn} reaction(s))\n"
                )
                if excerpt:
                    buf.write(f"  > {excerpt}\n")
        buf.write("\n")

    secure_mkdir(output_md_path.parent)
    output_md_path.write_text(buf.getvalue(), encoding="utf-8")
    try:
        os.chmod(output_md_path, 0o600)
    except OSError as e:
        logger.debug("chmod_best_effort_failed", path=str(output_md_path), err=str(e))
    logger.info(
        "video_triage_md_written",
        path=str(output_md_path),
        count=len(records),
    )
    return len(records)
