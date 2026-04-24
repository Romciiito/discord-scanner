"""Tests for the discovery layer: invite resolve + cache + guild/channel/role/forums.

Traces to: workplan.md Phase 4 tasks, SEC-P0-04 (invite redaction), MUST
"Pydantic tolerance", MUST "SQLite parameterisation".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from discord_scanner.discovery.channels import filter_channels, list_channels
from discord_scanner.discovery.forums import (
    list_active_threads,
    list_archived_public_threads,
)
from discord_scanner.discovery.guilds import (
    intersect_with_resolved_guild_ids,
    list_my_guilds,
)
from discord_scanner.discovery.invite_cache import TTL_SECONDS, InviteCache
from discord_scanner.discovery.invite_resolve import (
    load_enriched_invites,
    resolve_invite,
)
from discord_scanner.discovery.roles import list_roles
from discord_scanner.models.discord import (
    INVITE_CODE_REGEX,
    Channel,
    Guild,
    ResolvedInvite,
    is_valid_invite_code,
)

# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------


def test_invite_code_regex_accepts_common_forms() -> None:
    assert is_valid_invite_code("abcd")  # minimum 4
    assert is_valid_invite_code("StableDiffusion")
    assert is_valid_invite_code("ABC-123-def")
    assert is_valid_invite_code("abcdefghijklmnopqrst")  # maximum 20


def test_invite_code_regex_rejects_bad_forms() -> None:
    assert not is_valid_invite_code("abc")  # too short
    assert not is_valid_invite_code("a" * 21)  # too long
    assert not is_valid_invite_code("has space")
    assert not is_valid_invite_code("has/slash")
    assert not is_valid_invite_code("")


def test_invite_code_regex_pattern() -> None:
    assert INVITE_CODE_REGEX.pattern == r"^[A-Za-z0-9-]{4,20}$"


def test_guild_model_tolerates_extras() -> None:
    g = Guild.model_validate({"id": "1", "name": "X", "unknown_future_field": 42})
    assert g.id == "1"
    assert g.name == "X"


def test_channel_model_tolerates_extras() -> None:
    c = Channel.model_validate({"id": "1", "type": 0, "rate_limit_per_user": 60})
    assert c.id == "1"
    assert c.type == 0


# ----------------------------------------------------------------------
# Invite cache (sqlite)
# ----------------------------------------------------------------------


def test_cache_put_then_get(tmp_state_root: Path) -> None:
    with InviteCache(tmp_state_root) as cache:
        cache.put(ResolvedInvite(code="abcd", guild_id="g1", guild_name="Alpha"))
        hit = cache.get("abcd")
        assert hit is not None
        assert hit.guild_id == "g1"
        assert hit.guild_name == "Alpha"


def test_cache_miss_returns_none(tmp_state_root: Path) -> None:
    with InviteCache(tmp_state_root) as cache:
        assert cache.get("nothing") is None


def test_cache_expires_after_ttl(tmp_state_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale rows (> 7d) MUST be deleted on read and return None."""
    with InviteCache(tmp_state_root) as cache:
        # Insert with a cached_at timestamp that is already past TTL
        past = int(time.time()) - TTL_SECONDS - 1
        cache._conn.execute(
            "INSERT INTO invites (code, guild_id, guild_name, expires_at, "
            "member_count, cached_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("stale", "g", "Stale", None, None, past),
        )
        cache._conn.commit()

        assert cache.get("stale") is None
        # row was deleted
        row = cache._conn.execute("SELECT code FROM invites WHERE code = ?", ("stale",)).fetchone()
        assert row is None


# ----------------------------------------------------------------------
# Invite resolve
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_invite_happy_path(tmp_state_root: Path) -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get(
            "https://discord.com/api/v10/invites/abcd?with_counts=true&with_expiration=true"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "code": "abcd",
                    "guild": {"id": "111", "name": "Demo"},
                    "expires_at": None,
                    "approximate_member_count": 300,
                },
            )
        )
        with InviteCache(tmp_state_root) as cache:
            r = await resolve_invite(client, "abcd", cache=cache)
            assert r is not None
            assert r.guild_id == "111"
            assert r.guild_name == "Demo"
            # second call hits cache, makes no network request
            with respx.mock(assert_all_called=False) as mock2:
                _ = mock2  # ensure respx is not re-mocking this URL
                r2 = await resolve_invite(client, "abcd", cache=cache)
                assert r2 is not None
                assert r2.guild_id == "111"


@pytest.mark.asyncio
async def test_resolve_invalid_code_skips_pre_network() -> None:
    async with httpx.AsyncClient() as client, respx.mock(assert_all_called=False) as mock:
        r = await resolve_invite(client, "bad!code", cache=None)
        assert r is None
        assert mock.calls.call_count == 0  # never left the tool


@pytest.mark.asyncio
async def test_resolve_404_returns_none() -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get(
            "https://discord.com/api/v10/invites/abcd?with_counts=true&with_expiration=true"
        ).mock(return_value=httpx.Response(404, json={"message": "Unknown Invite"}))
        r = await resolve_invite(client, "abcd", cache=None)
        assert r is None


@pytest.mark.asyncio
async def test_resolve_403_returns_none() -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get(
            "https://discord.com/api/v10/invites/abcd?with_counts=true&with_expiration=true"
        ).mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
        r = await resolve_invite(client, "abcd", cache=None)
        assert r is None


@pytest.mark.asyncio
async def test_resolve_401_raises_token_invalid() -> None:
    """Reviewer MAJOR: 401 propagates as TokenInvalid so the CLI can exit 3."""
    from discord_scanner.discovery.invite_resolve import TokenInvalid

    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get(
            "https://discord.com/api/v10/invites/abcd?with_counts=true&with_expiration=true"
        ).mock(return_value=httpx.Response(401, json={"message": "Unauthorized"}))
        with pytest.raises(TokenInvalid):
            await resolve_invite(client, "abcd", cache=None)


@pytest.mark.asyncio
async def test_resolve_never_logs_raw_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SEC-P0-04: raw invite codes must not appear in any log record."""
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get(
            "https://discord.com/api/v10/invites/aBcD1234?with_counts=true&with_expiration=true"
        ).mock(return_value=httpx.Response(404))
        await resolve_invite(client, "aBcD1234", cache=None)
    for rec in caplog.records:
        assert "aBcD1234" not in rec.getMessage()


# ----------------------------------------------------------------------
# load_enriched_invites
# ----------------------------------------------------------------------


def test_load_enriched_invites_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "invites.enriched.json"
    p.write_text(
        json.dumps(
            [
                {"invite_code": "aaaa", "score_pct": 90, "intent": "prompt_sharing"},
                {"invite_code": "bbbb", "score_pct": 50, "unknown_field": "ok"},
            ]
        )
    )
    records = load_enriched_invites(p)
    assert len(records) == 2
    assert records[0]["invite_code"] == "aaaa"


def test_load_enriched_invites_missing_returns_empty(tmp_path: Path) -> None:
    assert load_enriched_invites(tmp_path / "nope.json") == []


def test_load_enriched_invites_size_cap(tmp_path: Path) -> None:
    p = tmp_path / "big.json"
    p.write_text(json.dumps([{"invite_code": "a" * 100}] * 20000))
    # default cap 1 MB — this file is many MB
    assert load_enriched_invites(p, max_bytes=1_000_000) == []


def test_load_enriched_invites_not_a_list(tmp_path: Path) -> None:
    p = tmp_path / "wrong.json"
    p.write_text(json.dumps({"not": "a list"}))
    assert load_enriched_invites(p) == []


def test_load_enriched_invites_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "corrupt.json"
    p.write_text("{not json")
    assert load_enriched_invites(p) == []


# ----------------------------------------------------------------------
# Guilds / channels / roles / forums
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_my_guilds_happy_path() -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200, json=[{"id": "1", "name": "Alpha"}, {"id": "2", "name": "Beta"}]
            )
        )
        guilds = await list_my_guilds(client)
        assert len(guilds) == 2
        assert guilds[0].id == "1"


@pytest.mark.asyncio
async def test_list_my_guilds_tolerates_malformed_record() -> None:
    """Pydantic tolerance: one bad record doesn't sink the whole list."""
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "1", "name": "Alpha"},
                    {"missing_required_fields": True},
                    {"id": "2", "name": "Beta"},
                ],
            )
        )
        guilds = await list_my_guilds(client)
        assert len(guilds) == 2
        assert {g.id for g in guilds} == {"1", "2"}


def test_intersect_with_resolved_guild_ids() -> None:
    joined = [Guild(id="1", name="A"), Guild(id="2", name="B"), Guild(id="3", name="C")]
    filtered = intersect_with_resolved_guild_ids(joined, {"1", "3"})
    assert [g.id for g in filtered] == ["1", "3"]


@pytest.mark.asyncio
async def test_list_channels_happy_path() -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/guilds/1/channels").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "c1", "type": 0, "name": "general"},
                    {"id": "c2", "type": 2, "name": "voice"},
                    {"id": "c3", "type": 15, "name": "forum"},
                ],
            )
        )
        chans = await list_channels(client, "1")
        assert len(chans) == 3


def test_filter_channels_type_filter() -> None:
    channels = [
        Channel(id="c1", type=0, name="general"),
        Channel(id="c2", type=2, name="voice"),
        Channel(id="c3", type=15, name="forum"),
        Channel(id="c4", type=5, name="announcements"),
    ]
    result = filter_channels(channels)
    assert {c.id for c in result} == {"c1", "c3", "c4"}


def test_filter_channels_include_overrides_type() -> None:
    channels = [
        Channel(id="c1", type=0, name="general"),
        Channel(id="c2", type=2, name="voice"),  # type 2 normally rejected
    ]
    result = filter_channels(channels, include_channels=["c2"])
    assert [c.id for c in result] == ["c2"]


def test_filter_channels_exclude_wins_over_include() -> None:
    channels = [Channel(id="c1", type=0, name="general")]
    result = filter_channels(channels, include_channels=["c1"], exclude_channels=["c1"])
    assert result == []


def test_filter_channels_by_name() -> None:
    channels = [Channel(id="c1", type=0, name="general"), Channel(id="c2", type=0, name="spam")]
    result = filter_channels(channels, exclude_channels=["spam"])
    assert [c.id for c in result] == ["c1"]


@pytest.mark.asyncio
async def test_list_roles_happy_path() -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/guilds/1/roles").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "r1", "name": "everyone"},
                    {"id": "r2", "name": "mod"},
                ],
            )
        )
        roles = await list_roles(client, "1")
        assert len(roles) == 2


@pytest.mark.asyncio
async def test_list_active_threads_happy_path() -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/channels/c1/threads/active").mock(
            return_value=httpx.Response(
                200,
                json={
                    "threads": [
                        {"id": "t1", "type": 11, "name": "T1"},
                        {"id": "t2", "type": 11, "name": "T2"},
                    ]
                },
            )
        )
        threads = await list_active_threads(client, "c1")
        assert len(threads) == 2


@pytest.mark.asyncio
async def test_list_archived_public_threads_happy_path() -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/channels/c1/threads/archived/public?limit=50").mock(
            return_value=httpx.Response(
                200, json={"threads": [{"id": "a1", "type": 11, "name": "old"}]}
            )
        )
        threads = await list_archived_public_threads(client, "c1", limit=50)
        assert len(threads) == 1


@pytest.mark.asyncio
async def test_list_guilds_http_error_returns_empty() -> None:
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/users/@me/guilds").mock(
            return_value=httpx.Response(500)
        )
        guilds = await list_my_guilds(client)
        assert guilds == []
