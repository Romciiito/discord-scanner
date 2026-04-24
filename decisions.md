# Decisions Journal: discord-scanner

This file records non-obvious implementation decisions made by build agents.
Read by the orchestrator on startup to give all agents cross-session continuity.

**Format:** Append new entries at the bottom. Never edit or delete existing entries.

---

## 2026-04-24 — Phase 1 CLI skeleton

**Context:** CLI callback needs to pass `--config / --verbose / --dry-run / --offline`
to every subcommand.

- **Decision:** use `typer.Context.obj` (a slotted `_CliContext` dataclass-like class)
  to carry invocation context. Rejected the alternative — a module-level `_CTX: dict`
  — because claude-rules "No global mutable state besides the single shared
  httpx.AsyncClient, the configured structlog logger, and the cursor/cookie/keyring-backed
  stores" forbids it. Code-reviewer (Opus) flagged this as a MAJOR finding during P1
  review; fix landed in the same commit.

- **Decision:** `_MIN_PLAUSIBLE_CHROME_MAJOR = 130` is a compile-time floor only.
  The dynamic "UA > 90 days stale" horizon from SEC-P0-08 is deferred to
  **TODO-P1-01** — it needs a monthly CI probe against chromestatus.com, which
  belongs in `.github/workflows/ci.yml` (devops-engineer, Phase 8).

- **Decision:** `store-token` silently falls back to default service/username
  when config load fails, but now logs a structlog `WARNING store_token_fallback_to_defaults`
  so the operator sees why. Code-reviewer MAJOR #3.

- **Decision:** `HttpSettings.fingerprint()` is the single source (SEC-P0-25) for
  both the REST `X-Super-Properties` blob (P2) and the gateway OPCODE 2 IDENTIFY
  `properties` payload (P3). Byte-for-byte parity is the responsibility of the
  P2 and P3 callers — enforced by a unit test that will land in each of those phases.

- **Decision:** Chrome UA default bumped from `134.0.0.0` to `134.0.0.0` (kept);
  the _floor_ was raised from `125` to `130` so the default sits comfortably above
  the rejection threshold. If future `discord-scanner` versions ship with a stale
  default UA, config-load will fail fast on the operator's first run.

- **Non-obvious test fixture choice:** `mock_keyring` in `tests/conftest.py`
  monkeypatches the **real** `keyring` module in-place (`monkeypatch.setattr(real_kr, ...)`)
  rather than substituting `sys.modules["keyring"]`. The earlier substitution
  approach broke `from keyring.errors import KeyringError` inside `store-token`,
  which needs the real submodule to exist. In-place patching keeps the CLI's
  error-handling reachable while still isolating tests from the OS credential store.

---

## 2026-04-24 — Phase 2 Auth + REST core

**Context:** 7 session modules + list-guilds wiring + 7 test files, with
`session/rest.py` carrying a ≥85% coverage merge-blocker floor.

- **Decision:** URL allowlist enforced by **httpx `event_hooks["request"]`**
  (not by wrapping `client.request()`). The hook runs BEFORE the transport
  emits any packet, so SSRFViolation is raised pre-network. Redirects also
  hit the hook on each hop. Alternative (explicit URL check at each call site)
  rejected — too easy to forget and impossible to grep-enforce.

- **Decision:** `_strip_auth_on_cdn` is a second request hook (not merged into
  `_enforce_allowlist`) so the two concerns stay independent. CDN Authorization
  leakage was a forward-risk MAJOR flagged by code-reviewer (Opus) on Phase 2
  review; fix landed in the same commit with two new tests
  (`test_authorization_stripped_on_cdn`, `test_authorization_present_on_discord_api`).

- **Decision:** `MAX_429_RETRIES=3` counts **only consecutive** 429s per spec
  §4.1 / SEC-P0-15. Interleaved 5xx resets the counter — code-reviewer flagged
  this as MAJOR ("a 429,500,429,500,429 would prematurely trigger ChannelAbort").
  Fix: reset `consecutive_429 = 0` inside the RETRY_STATUSES branch of
  `retry.py::request_with_retry`. Regression: existing
  `test_three_consecutive_429s_raise_channel_abort` covers the strict path.

- **Decision:** `build_x_super_properties` uses
  `json.dumps(fp, separators=(",", ":"), ensure_ascii=False)` — BYTE-STABLE
  output. `HttpSettings.fingerprint()` builds a dict with deterministic key
  order. P3's gateway IDENTIFY `properties` payload **MUST** decode this exact
  base64 blob and pass the decoded dict as-is — a P3 regression test will
  assert byte-for-byte parity. Extracting `_XSP_JSON_KWARGS` to a shared
  constant (reviewer MINOR) deferred to P3 when the second call site exists.

- **Decision:** `test_client_has_http2_and_verify_true` reaches into
  `client._transport._pool._http2` (double-underscore private). Reviewer
  flagged as fragility. Accepted for now because httpx doesn't expose an
  official getter; if httpx minor upgrade breaks it, we add a respx HTTP/2
  echo test. Tracked as TODO-P2-02.

- **Decision:** the `async for attempt in AsyncRetrying(...)` + nested
  `with attempt:` pattern in `retry.py` is the tenacity idiom but mypy
  struggles to infer `Any` on `e.last_attempt.exception()`. Explicit
  `cause = e.last_attempt.exception(); if cause is not None: raise cause from e`
  keeps strict-typing happy without `# type: ignore`.

- **Deferred:** SEC-P0-30 (dependency lock file + `pip install --require-hashes`)
  — tracked as TODO-P2-01. Owner: devops-engineer. Lands during Phase 8 CI sweep,
  once the full dependency footprint is stable across all 9 phases.

