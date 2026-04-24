"""zstandard-compressed JSONL writer for messages.jsonl.zst.

Traces to:
- seed-spec.md §2.8 (messages.jsonl.zst)
- claude-rules.md MUST "Sort stability / determinism" — comparison is on the
  **decompressed** stream (zstd internal dictionary state is not deterministic
  across runs at the compressed-bytes level).

The plaintext JSONL content IS byte-identical between two cold runs with
unchanged cursor + records; the `.zst` file may differ byte-for-byte due to
zstd internal state, which is why acceptance criterion #9 compares
decompressed.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterable
from pathlib import Path

import zstandard as zstd

from discord_scanner.dump.sort import canonical_json_dict, sort_messages
from discord_scanner.logging_conf import get_logger
from discord_scanner.models.message import Message

logger = get_logger(__name__)


def write_jsonl_zst(path: Path, records: Iterable[Message], level: int = 3) -> int:
    """Write `records` as zstandard-compressed JSONL.

    Returns the number of lines written. Uses a FIXED compressor level so the
    compressed bytes are at least algorithmically deterministic given the same
    input stream.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_records = sort_messages(records)
    count = 0

    # Build plaintext in memory so we can compress in one pass — keeps the
    # frame structure simple and sidesteps streaming-state nondeterminism.
    buffer = io.StringIO()
    for rec in sorted_records:
        payload = canonical_json_dict(rec.model_dump(mode="json"))
        line = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        buffer.write(line + "\n")
        count += 1

    plaintext = buffer.getvalue().encode("utf-8")
    cctx = zstd.ZstdCompressor(level=level)
    compressed = cctx.compress(plaintext)
    path.write_bytes(compressed)
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.debug("chmod_best_effort_failed", path=str(path), err=str(e))
    logger.info("jsonl_zst_written", path=str(path), count=count, size=len(compressed))
    return count


def read_jsonl_zst(path: Path) -> list[dict[str, object]]:
    """Decompress + parse a zstd JSONL file. Used by tests for idempotence."""
    raw = path.read_bytes()
    dctx = zstd.ZstdDecompressor()
    plaintext = dctx.decompress(raw).decode("utf-8")
    return [json.loads(line) for line in plaintext.splitlines() if line]
