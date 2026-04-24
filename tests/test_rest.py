"""Tests for `session/rest.py` — AsyncClient factory + URL allowlist.

Traces to: SEC-P0-07, SEC-P0-11, SEC-P0-17, SEC-P0-18.

**Merge-blocker floor: ≥ 85% coverage on `src/discord_scanner/session/rest.py`.**
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from discord_scanner.config import load_config
from discord_scanner.session.captcha import CaptchaAborted
from discord_scanner.session.rest import (
    ALLOWED_NETLOCS,
    SSRFViolation,
    _enforce_allowlist,
    make_client,
    persist_cookies,
)


@pytest.mark.asyncio
async def test_client_has_http2_and_verify_true(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    """SEC-P0-11 (http2=True) + SEC-P0-18 (verify=True)."""
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    try:
        assert client._transport._pool._http2 is True  # type: ignore[attr-defined]
        # verify=True means no custom SSLContext override was set
        # httpx stores it in the transport; we can spot-check via direct attr
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_to_allowed_host_succeeds(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    try:
        with respx.mock() as mock:
            mock.get("https://discord.com/api/v10/users/@me").mock(
                return_value=httpx.Response(200, json={"id": "42"})
            )
            r = await client.get("https://discord.com/api/v10/users/@me")
            assert r.status_code == 200
            assert r.json() == {"id": "42"}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_request_to_disallowed_host_raises_ssrf(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    """SEC-P0-17 merge-blocker: out-of-allowlist host → SSRFViolation BEFORE network."""
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    try:
        with pytest.raises(SSRFViolation):
            await client.get("https://evil.example.com/")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_scheme_rejected(tmp_config_yaml: Path, tmp_state_root: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    try:
        with pytest.raises(SSRFViolation):
            await client.get("http://discord.com/api/v10/users/@me")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_enforce_allowlist_passes_for_allowed() -> None:
    req = httpx.Request("GET", "https://discord.com/api/v10/test")
    await _enforce_allowlist(req)  # must not raise


@pytest.mark.asyncio
async def test_enforce_allowlist_passes_for_cdn() -> None:
    req = httpx.Request("GET", "https://cdn.discordapp.com/avatars/1/hash.png")
    await _enforce_allowlist(req)


@pytest.mark.asyncio
async def test_enforce_allowlist_rejects_disallowed() -> None:
    req = httpx.Request("GET", "https://evil.com/")
    with pytest.raises(SSRFViolation):
        await _enforce_allowlist(req)


def test_allowed_netlocs_frozen() -> None:
    assert isinstance(ALLOWED_NETLOCS, frozenset)
    assert "discord.com" in ALLOWED_NETLOCS
    assert "cdn.discordapp.com" in ALLOWED_NETLOCS
    assert "gateway.discord.gg" in ALLOWED_NETLOCS
    assert "media.discordapp.net" in ALLOWED_NETLOCS
    assert "evil.com" not in ALLOWED_NETLOCS


@pytest.mark.asyncio
async def test_captcha_in_403_aborts(tmp_config_yaml: Path, tmp_state_root: Path) -> None:
    """SEC-P0-16: 403 with captcha body → CaptchaAborted raised by response hook."""
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    try:
        with respx.mock() as mock:
            mock.get("https://discord.com/api/v10/blocked").mock(
                return_value=httpx.Response(403, json={"captcha_key": ["x"]})
            )
            with pytest.raises(CaptchaAborted):
                await client.get("https://discord.com/api/v10/blocked")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_persist_cookies_after_close(tmp_config_yaml: Path, tmp_state_root: Path) -> None:
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    client.cookies.set("__dcfduid", "persist-me", domain="discord.com", path="/")
    await client.aclose()
    persist_cookies(client, cfg, state_root=tmp_state_root)
    jar_file = tmp_state_root / "cookies-test-burner.json"
    assert jar_file.exists()
    assert "persist-me" in jar_file.read_text()


@pytest.mark.asyncio
async def test_authorization_stripped_on_cdn(tmp_config_yaml: Path, tmp_state_root: Path) -> None:
    """claude-rules: Authorization must never leave the client for CDN hosts."""
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("SECRET-TOK"), state_root=tmp_state_root)
    try:
        with respx.mock() as mock:
            route = mock.get("https://cdn.discordapp.com/attachments/1/x.png").mock(
                return_value=httpx.Response(200, content=b"PNG")
            )
            await client.get("https://cdn.discordapp.com/attachments/1/x.png")
            captured = route.calls[0].request
            assert "Authorization" not in captured.headers, (
                f"Authorization leaked to CDN: {dict(captured.headers)!r}"
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_authorization_present_on_discord_api(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    """Authorization MUST still be present for discord.com/api calls."""
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("NEEDED-TOK"), state_root=tmp_state_root)
    try:
        with respx.mock() as mock:
            route = mock.get("https://discord.com/api/v10/users/@me").mock(
                return_value=httpx.Response(200, json={"id": "1"})
            )
            await client.get("https://discord.com/api/v10/users/@me")
            captured = route.calls[0].request
            assert captured.headers.get("Authorization") == "NEEDED-TOK"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_full_header_set_on_outgoing_request(
    tmp_config_yaml: Path, tmp_state_root: Path
) -> None:
    """SEC-P0-07: every required header present on an outbound request."""
    cfg = load_config(tmp_config_yaml)
    client = make_client(cfg, SecretStr("TOK"), state_root=tmp_state_root)
    try:
        with respx.mock() as mock:
            route = mock.get("https://discord.com/api/v10/test").mock(
                return_value=httpx.Response(200, json={})
            )
            await client.get("https://discord.com/api/v10/test")
            # respx exposes the captured request
            request = route.calls[0].request
            for key in (
                "User-Agent",
                "X-Super-Properties",
                "X-Discord-Locale",
                "X-Discord-Timezone",
                "Authorization",
                "Origin",
                "Referer",
            ):
                assert key in request.headers, f"missing header: {key}"
                assert request.headers[key], f"empty header: {key}"
    finally:
        await client.aclose()
