"""Phase 0 tests for redact helpers.

Traces to: security-model.md §6 SEC-P0-03, seed-spec.md §3.
"""

from __future__ import annotations

from discord_scanner.logging_conf import (
    _redact_processor,
    redact_invite_code,
    redact_token,
)


def test_redact_token_format() -> None:
    tok = "MTI0NTY3ODkwMTIzNDU2Nzg5.GabcdE.fghijklmnopqrstuvwxyz1234567"
    r = redact_token(tok)
    assert r.startswith(tok[:6])
    assert r.endswith(tok[-4:])
    assert "***" in r
    assert len(r) < len(tok)


def test_redact_token_short() -> None:
    assert redact_token("abc") == "***"
    assert redact_token("") == "***"


def test_redact_invite_code_format() -> None:
    assert redact_invite_code("aBcDeFgH") == "aB***gH"
    assert redact_invite_code("xy") == "***"


def test_processor_redacts_invite_url_in_event_dict() -> None:
    event = {"event": "join", "url": "visit discord.gg/aBcDeFgH now"}
    out = _redact_processor(None, "info", event)
    assert "aBcDeFgH" not in out["url"]
    assert "discord.gg/aB***gH" in out["url"]


def test_processor_redacts_invite_url_in_discord_com_variant() -> None:
    event = {"url": "click https://discord.com/invite/ZYXWabcd here"}
    out = _redact_processor(None, "info", event)
    assert "ZYXWabcd" not in out["url"]
    assert "ZY***cd" in out["url"]


def test_processor_redacts_token_in_event_dict() -> None:
    tok = "MTI0NTY3ODkwMTIzNDU2Nzg5.GabcdE.fghijklmnopqrstuvwxyz1234567"
    event = {"event": "auth", "header": f"Authorization: {tok}"}
    out = _redact_processor(None, "info", event)
    assert tok not in out["header"]
    assert "***" in out["header"]


def test_processor_preserves_non_string_values() -> None:
    event = {"count": 5, "ok": True, "note": "no secrets here"}
    out = _redact_processor(None, "info", event)
    assert out["count"] == 5
    assert out["ok"] is True
    assert out["note"] == "no secrets here"


def test_processor_redacts_inside_list() -> None:
    event = {"urls": ["discord.gg/aBcDeFgH", "https://example.com"]}
    out = _redact_processor(None, "info", event)
    assert "aBcDeFgH" not in out["urls"][0]
    assert "aB***gH" in out["urls"][0]
    assert out["urls"][1] == "https://example.com"
