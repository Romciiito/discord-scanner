"""Plain JSONL writer for pinned + threads + meta-adjacent files.

Traces to:
- seed-spec.md §2.8 (output artefacts: pinned.jsonl, threads.jsonl)
- claude-rules.md MUST "No NaN / Infinity in JSON output" — raise instead

Canonical output:
- Dict keys alphabetised per `canonical_json_dict`
- `json.dumps(obj, allow_nan=False, separators=(",", ":"))` to forbid
  `NaN` / `Infinity` (ValueError would surface) and produce compact form
- `\\n`-terminated line per record
- `os.chmod(path, 0o600)` on create (SEC-P0-22)
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from discord_scanner.dump.sort import canonical_json_dict, sort_messages
from discord_scanner.logging_conf import get_logger
from discord_scanner.models.message import Message

logger = get_logger(__name__)


def write_jsonl(path: Path, records: Iterable[Message]) -> int:
    """Write `records` to `path` as deterministic plain JSONL.

    Returns the number of lines written. Sort is applied before serialisation.
    `allow_nan=False` raises `ValueError` on any `NaN` / `Infinity` —
    claude-rules MUST.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_records = sort_messages(records)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        for rec in sorted_records:
            payload = canonical_json_dict(rec.model_dump(mode="json"))
            line = json.dumps(payload, allow_nan=False, separators=(",", ":"))
            fh.write(line + "\n")
            count += 1
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.debug("chmod_best_effort_failed", path=str(path), err=str(e))
    logger.info("jsonl_written", path=str(path), count=count)
    return count


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a single canonical JSON object to `path` (used by meta.json / prior).

    Sorted keys, compact separators, no NaN.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_dict(data)
    path.write_text(
        json.dumps(payload, allow_nan=False, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.debug("chmod_best_effort_failed", path=str(path), err=str(e))
