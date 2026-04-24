"""Tests for `session/captcha.py` — body inspection + CaptchaAborted.

Traces to: SEC-P0-16.
"""

from __future__ import annotations

import httpx
import pytest

from discord_scanner.session.captcha import CAPTCHA_KEYS, CaptchaAborted, check_response


def _resp(status: int, body: dict | str | None) -> httpx.Response:
    """Build a Response with a Request attached so `.json()` / `.text` work in tests."""
    request = httpx.Request("GET", "https://discord.com/api/v10/test")
    if isinstance(body, dict):
        return httpx.Response(status, json=body, request=request)
    if isinstance(body, str):
        return httpx.Response(status, text=body, request=request)
    return httpx.Response(status, request=request)


def test_captcha_key_in_403_body_raises_aborted() -> None:
    resp = _resp(403, {"captcha_key": ["captcha-required"]})
    with pytest.raises(CaptchaAborted) as e:
        check_response(resp)
    assert e.value.exit_code == 2
    assert e.value.matched_key == "captcha_key"


def test_captcha_sitekey_in_401_body_raises_aborted() -> None:
    resp = _resp(401, {"captcha_sitekey": "abc"})
    with pytest.raises(CaptchaAborted):
        check_response(resp)


def test_captcha_service_nested_in_errors_raises_aborted() -> None:
    resp = _resp(403, {"errors": {"captcha_service": "hcaptcha"}})
    with pytest.raises(CaptchaAborted):
        check_response(resp)


def test_plain_403_without_captcha_returns_silently() -> None:
    resp = _resp(403, {"code": 50001, "message": "Missing Access"})
    # should not raise
    check_response(resp)


def test_non_401_403_is_untouched() -> None:
    resp = _resp(500, {"captcha_key": "ignored because status != 401/403"})
    check_response(resp)  # MUST NOT raise


def test_non_json_403_is_tolerated() -> None:
    resp = _resp(403, "plain text body")
    check_response(resp)  # MUST NOT raise


def test_captcha_keys_constant() -> None:
    assert frozenset({"captcha_key", "captcha_sitekey", "captcha_service"}) == CAPTCHA_KEYS
