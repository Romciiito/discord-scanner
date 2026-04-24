"""Deterministic sort helpers for Stage 2 → Stage 3 on-disk contract.

Traces to:
- seed-spec.md §3 "Sort stability": within-file order is
  `(channel_id ASC, timestamp ASC, message_id ASC)`; JSON dict keys alphabetised.
- claude-rules.md MUST "Sort stability / determinism" — two cold runs on an
  unchanged cursor MUST produce byte-identical decompressed JSONL.
- REQ-NF-034 / acceptance criterion #9 (idempotence).

Used by `jsonl_writer` + `zstd_writer` to normalise every record before
serialisation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from discord_scanner.models.message import Message


def sort_messages(msgs: Iterable[Message]) -> list[Message]:
    """Return `msgs` sorted by `(channel_id, timestamp, message_id)`.

    Numeric snowflake sort for `message_id` — lex sort would break on
    mixed-length snowflake strings (see `fetch/messages.py`).
    """
    as_list = list(msgs)
    as_list.sort(
        key=lambda m: (
            m.channel_id,
            m.timestamp,
            _snowflake_int(m.message_id),
        )
    )
    return as_list


def _snowflake_int(message_id: str) -> int:
    return int(message_id) if message_id.isdigit() else -1


def canonical_json_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively sort dict keys alphabetically (seed-spec §3).

    Lists keep order (already sorted by the caller). Scalars unchanged.
    This drives byte-for-byte determinism between two runs.
    """
    if isinstance(data, dict):
        return {k: canonical_json_dict(data[k]) for k in sorted(data.keys())}
    if isinstance(data, list):
        return [canonical_json_dict(v) if isinstance(v, dict) else v for v in data]
    return data
