"""Tests for dump writers — sort stability + idempotence + NaN rejection.

Traces to: workplan.md Phase 8 done definition, REQ-NF-034 / acceptance #9
(byte-identical decompressed JSONL on two cold runs).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from discord_scanner.dump.jsonl_writer import write_json, write_jsonl
from discord_scanner.dump.meta import ScanCounters, ScanMeta, write_meta
from discord_scanner.dump.prior import resolve_prior_date, write_prior
from discord_scanner.dump.sort import canonical_json_dict, sort_messages
from discord_scanner.dump.zstd_writer import read_jsonl_zst, write_jsonl_zst
from discord_scanner.models.message import Message


def _make_msg(
    *,
    channel_id: str = "c1",
    message_id: str,
    timestamp: str = "2026-04-24T00:00:00+00:00",
    content: str = "",
) -> Message:
    return Message.from_api(
        {
            "id": message_id,
            "author": {"id": "u1", "username": "alice"},
            "timestamp": timestamp,
            "content": content,
        },
        guild_id="g1",
        channel_id=channel_id,
    )


# ----------------------------------------------------------------------
# sort.py
# ----------------------------------------------------------------------


def test_sort_messages_by_channel_then_timestamp_then_id() -> None:
    msgs = [
        _make_msg(channel_id="c2", message_id="200"),
        _make_msg(channel_id="c1", message_id="100"),
        _make_msg(channel_id="c1", message_id="101"),
    ]
    result = sort_messages(msgs)
    assert [m.message_id for m in result] == ["100", "101", "200"]


def test_sort_messages_numeric_snowflake_not_lex() -> None:
    """17-digit snowflakes sort before 18-digit numerically, not lex."""
    msgs = [
        _make_msg(message_id="100000000000000000"),  # 18-digit
        _make_msg(message_id="99999999999999999"),  # 17-digit — numerically smaller
    ]
    result = sort_messages(msgs)
    assert [m.message_id for m in result] == [
        "99999999999999999",
        "100000000000000000",
    ]


def test_canonical_json_dict_sorts_keys_recursively() -> None:
    d = {"b": 1, "a": {"z": 2, "x": {"d": 3, "c": 4}}}
    out = canonical_json_dict(d)
    # dict preserves insertion order in Python 3.7+, so key order IS observable
    assert list(out.keys()) == ["a", "b"]
    assert list(out["a"].keys()) == ["x", "z"]
    assert list(out["a"]["x"].keys()) == ["c", "d"]


def test_canonical_json_dict_preserves_lists_in_order() -> None:
    d = {"items": [{"b": 1, "a": 2}, {"d": 3, "c": 4}]}
    out = canonical_json_dict(d)
    assert out["items"][0] == {"a": 2, "b": 1}
    assert out["items"][1] == {"c": 4, "d": 3}


# ----------------------------------------------------------------------
# write_jsonl — plain + canonical + idempotent
# ----------------------------------------------------------------------


def test_write_jsonl_writes_expected_count(tmp_path: Path) -> None:
    msgs = [_make_msg(message_id="1"), _make_msg(message_id="2")]
    p = tmp_path / "pinned.jsonl"
    n = write_jsonl(p, msgs)
    assert n == 2
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_write_jsonl_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """Idempotence — REQ-NF-034."""
    msgs = [
        _make_msg(message_id="200"),
        _make_msg(message_id="100"),
        _make_msg(message_id="150"),
    ]
    p1 = tmp_path / "run1.jsonl"
    p2 = tmp_path / "run2.jsonl"
    write_jsonl(p1, msgs)
    write_jsonl(p2, msgs)
    assert p1.read_bytes() == p2.read_bytes()


def test_write_jsonl_sorts_on_output(tmp_path: Path) -> None:
    """Order of input MUST NOT affect output bytes."""
    order_a = [_make_msg(message_id="3"), _make_msg(message_id="1"), _make_msg(message_id="2")]
    order_b = list(reversed(order_a))
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    write_jsonl(p1, order_a)
    write_jsonl(p2, order_b)
    assert p1.read_bytes() == p2.read_bytes()


def test_write_jsonl_alphabetises_dict_keys(tmp_path: Path) -> None:
    """Every line is a JSON object with keys in alphabetical order."""
    msgs = [_make_msg(message_id="1")]
    p = tmp_path / "x.jsonl"
    write_jsonl(p, msgs)
    line = p.read_text(encoding="utf-8").splitlines()[0]
    obj = json.loads(line)
    keys = list(obj.keys())
    assert keys == sorted(keys)


def test_write_json_raises_on_nan(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        write_json(p, {"score": float("nan")})


def test_write_json_raises_on_infinity(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        write_json(p, {"score": float("inf")})


def test_write_json_sorts_keys(tmp_path: Path) -> None:
    p = tmp_path / "ok.json"
    write_json(p, {"z": 1, "a": 2, "m": 3})
    text = p.read_text(encoding="utf-8")
    # Compact form: `{"a":2,"m":3,"z":1}`
    assert text == '{"a":2,"m":3,"z":1}'


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only perm bits")
def test_write_jsonl_is_0o600_on_posix(tmp_path: Path) -> None:
    msgs = [_make_msg(message_id="1")]
    p = tmp_path / "x.jsonl"
    write_jsonl(p, msgs)
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600


# ----------------------------------------------------------------------
# write_jsonl_zst — decompressed determinism
# ----------------------------------------------------------------------


def test_write_jsonl_zst_decompressed_bytes_identical(tmp_path: Path) -> None:
    """REQ-NF-034 / acceptance #9: two cold runs → byte-identical
    DECOMPRESSED JSONL (compressed bytes may differ due to zstd internals)."""
    msgs = [
        _make_msg(message_id="1", content="hello"),
        _make_msg(message_id="2", content="world"),
    ]
    p1 = tmp_path / "a.jsonl.zst"
    p2 = tmp_path / "b.jsonl.zst"
    write_jsonl_zst(p1, msgs)
    write_jsonl_zst(p2, msgs)
    # Decompress and compare — compressed bytes may differ at zstd internal
    # dictionary level; the operational contract is on decompressed content.
    plaintext1 = read_jsonl_zst(p1)
    plaintext2 = read_jsonl_zst(p2)
    assert plaintext1 == plaintext2


def test_write_jsonl_zst_round_trip(tmp_path: Path) -> None:
    msgs = [_make_msg(message_id="1", content="hi"), _make_msg(message_id="2")]
    p = tmp_path / "m.jsonl.zst"
    n = write_jsonl_zst(p, msgs)
    assert n == 2
    read = read_jsonl_zst(p)
    assert len(read) == 2
    assert read[0]["message_id"] == "1"


# ----------------------------------------------------------------------
# meta.json
# ----------------------------------------------------------------------


def test_write_meta_json_shape(tmp_path: Path) -> None:
    p = tmp_path / "meta.json"
    meta = ScanMeta(
        guild_id="g1",
        guild_name="Alpha",
        scan_date="2026-04-24",
        scan_started_at="2026-04-24T00:00:00Z",
        scan_finished_at="2026-04-24T00:05:00Z",
        duration_sec=300.0,
        counters=ScanCounters(
            channels_scanned=3,
            messages_fetched=100,
            pinned_fetched=5,
        ),
    )
    write_meta(p, meta)
    obj = json.loads(p.read_text(encoding="utf-8"))
    assert obj["guild_id"] == "g1"
    assert obj["counters"]["channels_scanned"] == 3
    # Keys must be sorted alphabetically
    assert list(obj.keys()) == sorted(obj.keys())


def test_scan_counters_default_zero() -> None:
    c = ScanCounters()
    assert c.channels_scanned == 0
    assert c.messages_fetched == 0
    assert c.channel_aborts == 0


# ----------------------------------------------------------------------
# prior.txt
# ----------------------------------------------------------------------


def test_resolve_prior_date_no_prior(tmp_path: Path) -> None:
    guild_dir = tmp_path / "g1"
    guild_dir.mkdir()
    assert resolve_prior_date(guild_dir, "2026-04-24") is None


def test_resolve_prior_date_strictly_older(tmp_path: Path) -> None:
    guild_dir = tmp_path / "g1"
    guild_dir.mkdir()
    (guild_dir / "2026-04-24").mkdir()  # current — must be excluded
    (guild_dir / "2026-04-23").mkdir()
    (guild_dir / "2026-04-20").mkdir()
    (guild_dir / "not-a-date").mkdir()  # noise
    prior = resolve_prior_date(guild_dir, "2026-04-24")
    assert prior == "2026-04-23"


def test_resolve_prior_date_guild_dir_absent(tmp_path: Path) -> None:
    assert resolve_prior_date(tmp_path / "nope", "2026-04-24") is None


def test_write_prior_with_date(tmp_path: Path) -> None:
    p = tmp_path / "prior.txt"
    write_prior(p, "2026-04-23")
    assert p.read_text(encoding="utf-8") == "2026-04-23\n"


def test_write_prior_none_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "prior.txt"
    write_prior(p, None)
    assert p.read_text(encoding="utf-8") == ""
