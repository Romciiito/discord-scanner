"""Captcha detection + hard-abort — NEVER solve.

Traces to:
- seed-spec.md §2.6 (captcha detection)
- security-model.md §6 SEC-P0-16 (exit 2 on captcha; no auto-solve)
- claude-rules.md MUST "Captcha hard-abort" + MUST-NOT "No captcha auto-solve"

On a 401/403 whose body contains any of `{captcha_key, captcha_sitekey,
captcha_service}` → raise `CaptchaAborted(exit_code=2)`. Plain 403 without
those keys → log WARNING and continue (caller skips channel).
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from discord_scanner.logging_conf import get_logger

logger = get_logger(__name__)

CAPTCHA_KEYS: Final[frozenset[str]] = frozenset(
    {"captcha_key", "captcha_sitekey", "captcha_service"}
)


class CaptchaAborted(RuntimeError):
    """Raised on captcha detection. Callers map this to CLI exit code 2.

    Never caught and retried: any attempt to auto-solve would violate
    claude-rules MUST-NOT "No captcha auto-solve".
    """

    def __init__(self, url: str, matched_key: str) -> None:
        self.url = url
        self.matched_key = matched_key
        self.exit_code = 2
        super().__init__(
            f"captcha detected on {url} (key={matched_key}); "
            "operator must rotate burner and retry later"
        )


def _scan_body_for_captcha(body: Any) -> str | None:
    """Return the first matching captcha key found anywhere in the JSON body.

    Recurses one level into nested dicts (Discord occasionally nests the key
    under an `errors` / `captcha` wrapper).
    """
    if isinstance(body, dict):
        for key in body:
            if key in CAPTCHA_KEYS:
                return str(key)
        for value in body.values():
            if isinstance(value, dict):
                for nested_key in value:
                    if nested_key in CAPTCHA_KEYS:
                        return str(nested_key)
    return None


def check_response(resp: httpx.Response) -> None:
    """Inspect a response for captcha markers. Raises `CaptchaAborted` on hit.

    Safe to call on any response; only 401 / 403 bodies are scanned (other
    statuses are returned to the caller untouched).
    """
    if resp.status_code not in (401, 403):
        return
    try:
        body = resp.json()
    except (ValueError, httpx.DecodingError):
        logger.debug("captcha_scan_non_json", status=resp.status_code, url=str(resp.url))
        return
    matched = _scan_body_for_captcha(body)
    if matched is None:
        if resp.status_code == 403:
            logger.warning("plain_403_no_captcha", url=str(resp.url))
        return
    logger.error(
        "captcha_detected",
        status=resp.status_code,
        url=str(resp.url),
        matched_key=matched,
    )
    raise CaptchaAborted(url=str(resp.url), matched_key=matched)
