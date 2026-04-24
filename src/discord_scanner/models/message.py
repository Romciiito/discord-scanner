"""Message model for messages.jsonl.zst / pinned.jsonl / threads.jsonl.

Traces to: seed-spec.md §7.1 (Message schema).

All fields are `extra='allow'` per claude-rules "Pydantic tolerance" — Discord
field additions don't crash the scan; malformed records skip with WARNING.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)


class Attachment(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    filename: str
    content_type: str | None = None
    size: int | None = None
    cdn_url: str | None = None  # `url` in raw payload; renamed at ingest
    local_path: str | None = None


class Reaction(BaseModel):
    model_config = ConfigDict(extra="allow")
    emoji: str
    count: int
    animated: bool | None = None
    id: str | None = None


class Mentions(BaseModel):
    model_config = ConfigDict(extra="allow")
    users: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class Message(BaseModel):
    """One JSONL record per message (seed-spec §7.1).

    Populated from raw Discord API JSON via `from_api()` to rename + flatten
    a handful of fields (author → author_name/author_id; attachments.url →
    cdn_url; mention_roles → mentions.roles).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    guild_id: str
    channel_id: str
    channel_name: str | None = None
    channel_type: str | None = None
    parent_channel_id: str | None = None
    message_id: str
    author_id: str
    author_name: str
    author_discriminator: str | None = None
    content: str = ""
    timestamp: str
    edited_timestamp: str | None = None
    reactions: list[Reaction] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    mentions: Mentions = Field(default_factory=Mentions)
    reply_to_message_id: str | None = None
    thread_id: str | None = None
    pinned: bool = False
    flags: int = 0

    @classmethod
    def from_api(
        cls,
        raw: dict[str, Any],
        *,
        guild_id: str,
        channel_id: str,
        channel_name: str | None = None,
        channel_type: str | None = None,
        parent_channel_id: str | None = None,
        thread_id: str | None = None,
    ) -> Message:
        """Coerce a raw Discord message dict into a Stage 2 `Message` record.

        Raises `ValueError` with a stable message tag when the hard-contract
        fields `id`, `author.id`, or `timestamp` are missing — the fetch-layer
        caller (P9) catches this, emits a WARNING, and skips the record rather
        than writing a garbage row with empty author fields.
        """
        mid = raw.get("id")
        author = raw.get("author")
        timestamp = raw.get("timestamp")
        if not mid or not isinstance(author, dict) or not author.get("id") or not timestamp:
            raise ValueError("message_missing_required_fields")
        attachments_raw = raw.get("attachments") or []
        reactions_raw = raw.get("reactions") or []
        ref = raw.get("message_reference") or {}

        attachments: list[Attachment] = []
        for a in attachments_raw:
            if not isinstance(a, dict) or not a.get("id") or not a.get("filename"):
                continue  # malformed attachment skipped per Pydantic tolerance
            try:
                attachments.append(
                    Attachment(
                        id=str(a["id"]),
                        filename=str(a["filename"]),
                        content_type=a.get("content_type"),
                        size=a.get("size"),
                        cdn_url=a.get("url"),
                    )
                )
            except Exception as e:  # noqa: BLE001 — Pydantic tolerance
                logger.warning("attachment_coerce_failed", err=str(e))
                continue

        reactions: list[Reaction] = []
        for r in reactions_raw:
            emoji = r.get("emoji") or {}
            name = emoji.get("name") or ""
            try:
                reactions.append(
                    Reaction(
                        emoji=name,
                        count=int(r.get("count") or 0),
                        animated=emoji.get("animated"),
                        id=emoji.get("id"),
                    )
                )
            except Exception as e:  # noqa: BLE001 — Pydantic tolerance
                logger.warning("reaction_coerce_failed", err=str(e))
                continue

        mention_users = [
            str(m.get("id")) for m in (raw.get("mentions") or []) if isinstance(m, dict)
        ]
        mention_roles = [str(rid) for rid in (raw.get("mention_roles") or [])]

        return cls(
            guild_id=guild_id,
            channel_id=channel_id,
            channel_name=channel_name,
            channel_type=channel_type,
            parent_channel_id=parent_channel_id,
            message_id=str(mid),
            author_id=str(author["id"]),
            author_name=str(author.get("username") or author.get("global_name") or ""),
            author_discriminator=author.get("discriminator"),
            content=str(raw.get("content") or ""),
            timestamp=str(timestamp),
            edited_timestamp=raw.get("edited_timestamp"),
            reactions=reactions,
            attachments=attachments,
            mentions=Mentions(users=mention_users, roles=mention_roles),
            reply_to_message_id=(str(ref.get("message_id")) if ref.get("message_id") else None),
            thread_id=thread_id,
            pinned=bool(raw.get("pinned") or False),
            flags=int(raw.get("flags") or 0),
        )
