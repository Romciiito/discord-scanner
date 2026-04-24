"""Full Discord REST header set + X-Super-Properties blob.

Traces to:
- seed-spec.md §4.1 (17-header set)
- security-model.md §6 SEC-P0-07 (header completeness), SEC-P0-09 (XSP fields
  match config), SEC-P0-25 (fingerprint single source)
- claude-rules.md MUST "Full Discord REST header set"

Both this module and the gateway (P3) consume `Settings.http.fingerprint()`
so the REST `X-Super-Properties` blob and the gateway OPCODE 2 IDENTIFY
`properties` payload are byte-for-byte identical — a merge-blocker test
enforces this from P3 onwards.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Final

from pydantic import SecretStr

from discord_scanner.config import Settings

# Shared `json.dumps(...)` kwargs used by BOTH the REST `X-Super-Properties`
# header builder AND the gateway OPCODE 2 IDENTIFY `properties` serializer
# (P3). Keeping the kwargs in one place is a prerequisite for the
# byte-for-byte parity test enforced by `test_gateway.py` — SEC-P0-25.
XSP_JSON_KWARGS: Final[dict[str, Any]] = {
    "separators": (",", ":"),
    "ensure_ascii": False,
}

# The 17 REST headers from seed-spec §4.1. Every key in this set MUST be
# present (non-empty) on every outbound REST request.
REQUIRED_HEADER_KEYS: Final[tuple[str, ...]] = (
    "Authorization",
    "User-Agent",
    "Sec-Ch-Ua",
    "Sec-Ch-Ua-Mobile",
    "Sec-Ch-Ua-Platform",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Dest",
    "X-Super-Properties",
    "X-Discord-Locale",
    "X-Discord-Timezone",
    "X-Debug-Options",
    "Origin",
    "Referer",
    "Accept",
    "Accept-Encoding",
    "Accept-Language",
)


def build_x_super_properties(settings: Settings) -> str:
    """Base64-encoded JSON blob consumed by both REST headers and gateway IDENTIFY.

    The JSON is compact + key order is stable (matches `fingerprint()` dict
    insertion) so REST and gateway are byte-for-byte equal. Shared kwargs
    are in `XSP_JSON_KWARGS` — both call sites must use them.
    """
    fp = settings.http.fingerprint()
    raw = json.dumps(fp, **XSP_JSON_KWARGS).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def build_rest_headers(settings: Settings, token: SecretStr) -> dict[str, str]:
    """Build the full seed-spec §4.1 header set.

    `Authorization` is a user token — **no `Bearer` prefix** (bot tokens use
    Bearer; user tokens do not). That distinction is itself a detectability
    signal: passing `Bearer <user_token>` would break auth.
    """
    ua_version = settings.http.user_agent_chrome_version
    major = ua_version.split(".", 1)[0]
    user_agent = (
        f"Mozilla/5.0 ({settings.http.fake_os}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{ua_version} Safari/537.36"
    )
    sec_ch_ua = f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not?A_Brand";v="99"'
    headers: dict[str, str] = {
        "Authorization": token.get_secret_value(),
        "User-Agent": user_agent,
        "Sec-Ch-Ua": sec_ch_ua,
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": f'"{settings.http.fake_os_platform}"',
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "X-Super-Properties": build_x_super_properties(settings),
        "X-Discord-Locale": settings.http.locale,
        "X-Discord-Timezone": settings.http.timezone,
        "X-Debug-Options": "bugReporterEnabled",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": f"{settings.http.locale},en;q=0.9",
    }
    # invariant: every required key present and non-empty
    missing = [k for k in REQUIRED_HEADER_KEYS if not headers.get(k)]
    if missing:
        raise RuntimeError(f"BUG: build_rest_headers produced an incomplete header set: {missing}")
    return headers
