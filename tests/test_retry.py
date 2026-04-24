"""Tests for `session/retry.py` — 429 handling + ChannelAbort.

Traces to: SEC-P0-15.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from discord_scanner.session.retry import (
    MAX_429_RETRIES,
    ChannelAbort,
    request_with_retry,
)


@pytest.mark.asyncio
async def test_three_consecutive_429s_raise_channel_abort() -> None:
    """SEC-P0-15: MAX_429_RETRIES=3 consecutive 429s → ChannelAbort."""
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/test").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0.01"})
        )
        with pytest.raises(ChannelAbort):
            await request_with_retry(
                client,
                "GET",
                "https://discord.com/api/v10/test",
                attempts=10,
                backoff_initial_sec=0.001,
                backoff_max_sec=0.01,
            )


@pytest.mark.asyncio
async def test_429_then_200_succeeds() -> None:
    """One 429 with Retry-After → retry → 200 → success."""
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        route = mock.get("https://discord.com/api/v10/ok")
        route.mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0.01"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        resp = await request_with_retry(
            client,
            "GET",
            "https://discord.com/api/v10/ok",
            attempts=5,
            backoff_initial_sec=0.001,
            backoff_max_sec=0.01,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_non_retry_status_returns_immediately() -> None:
    """401 is not in RETRY_STATUSES → first response returned directly."""
    async with httpx.AsyncClient() as client, respx.mock() as mock:
        mock.get("https://discord.com/api/v10/nope").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        resp = await request_with_retry(
            client,
            "GET",
            "https://discord.com/api/v10/nope",
            attempts=3,
            backoff_initial_sec=0.001,
            backoff_max_sec=0.01,
        )
        assert resp.status_code == 401


def test_max_429_constant() -> None:
    assert MAX_429_RETRIES == 3
