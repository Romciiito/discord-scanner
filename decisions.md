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

---

## 2026-04-24 — Phase 3 Gateway Dormant Session

**Context:** single-file FSM (~400 LOC) plus hand-rolled `FakeWebSocket` mock
in `tests/conftest.py`. Merge-blocker floor ≥85% coverage on `session/gateway.py`.

- **Decision:** `IDENTIFY.properties` is the **exact** dict returned by
  `settings.http.fingerprint()` — no parallel fingerprint blob, no re-derivation.
  REST `X-Super-Properties` base64-decodes to the same dict. Parity is verified
  two ways: (a) `test_identify_properties_equal_rest_xsp_dict` compares dicts,
  (b) `test_identify_properties_byte_stable_via_shared_kwargs` compares bytes
  after serializing with the shared `XSP_JSON_KWARGS` constant (exported from
  `session/headers.py` — TODO-P3-01 closed).

- **Decision:** the outer `IDENTIFY` frame is serialized with Python-default
  `json.dumps` (spaces after separators). Only the **inner `properties` payload**
  needs byte parity with REST XSP because that's what Discord fingerprints.
  Frame-level whitespace is not part of the protocol fingerprint.

- **Decision (reviewer-flagged MAJOR, fixed inline):** `reconnect_with_resume`
  cancels the live heartbeat task BEFORE opening the new WS, otherwise
  `_do_handshake` spawns a second heartbeat loop and OPCODE 1 rate doubles —
  a real detectability signal. Regression test
  `test_reconnect_cancels_old_heartbeat_task` plants a never-finishing task,
  calls reconnect, asserts the old task is `done()` and the new one is different.

- **Decision:** `FileLock(state/gateway-{keyring_username}.lock, timeout=0)`
  is acquired + `os.chmod(0o600)` best-effort immediately. Windows only sets
  the read-only bit (documented in docs/claude/development.md §PII).
  `GatewayConcurrencyError` maps to CLI exit 1.

- **Decision:** `self._ws: Any | None` — duck-typed so `FakeWebSocket` and the
  real `websockets.connect(GATEWAY_URL)` both fit. Acceptable because the
  websockets API is stable + pinned `>=13` and the real WS is only touched
  through `recv / send / close`. Reviewer flagged as MINOR; accepted.

- **Decision:** migrated from deprecated `websockets.client.connect` to the
  top-level `websockets.connect` API (websockets 16 deprecation). mypy was
  correct to flag; fix was a one-import change.

- **Decision:** heartbeat loop uses
  `await asyncio.wait_for(self._shutdown.wait(), timeout=interval * jitter)`
  instead of `asyncio.sleep` because shutdown must interrupt the sleep
  immediately. Loop exits cleanly on `self._shutdown.set()` and the `TimeoutError`
  from `wait_for` is the "keep looping" signal. No `time.sleep` anywhere
  (CI grep enforces).

- **Decision:** `OP_INVALID_SESSION` on a RESUME attempt does NOT raise —
  instead we fall through to a fresh IDENTIFY (seed-spec §2.5 "On disconnect:
  try RESUME with session_id + sequence; fall back to fresh IDENTIFY if resume
  fails"). Only INVALID_SESSION on a fresh IDENTIFY is treated as a hard
  error (bad token or bad fingerprint).

---

## 2026-04-24 — Phase 4 Discovery

**Context:** 6 discovery modules + `models/discord.py` with pydantic v2 models
for Invite/Guild/Channel/Role, sqlite-backed 7-day invite cache, `resolve` CLI
command wired end-to-end.

- **Decision (reviewer MAJOR #1 fixed inline):** 401 on `/api/v10/invites/{code}`
  now raises `TokenInvalid` (new exception in `invite_resolve.py`). The CLI
  `resolve` command catches it and exits 3 (detected-ban), matching the
  exit-code contract used by `list-guilds`. Previously 401 was silently
  logged-and-skipped, causing `resolve` to exit 0 on a banned token.

- **Decision (reviewer MAJOR #2 accepted-with-documentation):** sqlite
  SELECT-then-DELETE atomicity in `InviteCache.get()` relies on the
  single-writer assumption (one CLI invocation at a time, enforced by the
  gateway filelock). Module docstring now documents this; no `BEGIN IMMEDIATE`
  needed. If P9 introduces concurrent writers we revisit.

- **Decision:** `INVITE_CODE_REGEX = r"^[A-Za-z0-9-]{4,20}$"` validates codes
  pre-network. Invalid codes never leave the tool — `resolve_invite` short-
  circuits with a redacted-log WARNING. This is both a spec requirement and
  a cheap anti-waste filter.

- **Decision:** `load_enriched_invites` has a hard 1 MB cap. Stage 1
  (civit-hf-scanner) output is typically < 50 KB even at full matrix; a
  multi-MB file signals either corruption or a Stage 1 regression and should
  fail loudly. The cap is configurable via the keyword arg for power users.

- **Decision:** `intersect_with_resolved_guild_ids` is belt-and-suspenders
  per seed-spec §2.2 — the tool only scans guilds that are in BOTH the
  invite-resolve output AND the `/users/@me/guilds` response. Prevents a
  malicious `invites.enriched.json` from coercing the scanner into probing
  guilds the burner hasn't joined.

- **Decision:** `SCANNABLE_CHANNEL_TYPES = frozenset({0, 5, 15})` — text,
  announcement, forum only. Voice (2), category (4), threads (10, 11, 12)
  are excluded from the default filter. Per-guild include/exclude overrides
  can whitelist additional channels. Exclude always wins over include.

- **Decision:** every discovery module catches `Exception` in the per-record
  pydantic coerce loop (`# noqa: BLE001`) because "Pydantic tolerance" is a
  claude-rules MUST — one bad upstream record should skip itself and let the
  run continue. Reviewer flagged as MINOR (prefer narrowing to
  `ValidationError`); accepted deferral, will be tightened in a Phase 5 polish pass.

---

## 2026-04-25 — Phase 12 hardening (D1 — Chrome-stable probe endpoint)

**Context:** TODO-P1-01 has been carried forward since Phase 1. The compiled-in
`_MIN_PLAUSIBLE_CHROME_MAJOR=130` floor in `src/discord_scanner/config.py`
catches an absurdly stale UA at config-load time, but does NOT enforce the
SEC-P0-08 dynamic "UA > 90 days behind live Chrome stable" horizon. The
ROADMAP's Phase 12.a closes this with a monthly CI probe + auto-PR bump.

The probe needs a deterministic, no-auth, JSON-returning endpoint that
publishes the current Chrome stable major version per-platform.

- **Decision:** use `https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Windows`
  as the canonical source-of-truth for the monthly probe.

  - Returns a JSON array of releases ordered newest-first; `[0].version` is
    a string like `"134.0.6998.166"` from which the major is `int(version.split(".")[0])`.
  - No auth required, no rate-limit pressure for a once-monthly call,
    historically stable URL maintained by the Chromium team.
  - `--platform=Windows` matches `config.http.fake_os_platform` default
    ("Windows") so the probe stays in lock-step with the fingerprint we send.
    If the operator changes platform, the probe URL must be updated to match.

- **Rejected alternatives:**

  - `https://versionhistory.googleapis.com/v1/chrome/platforms/win/channels/stable/versions`
    — also deterministic + JSON, but the `versionhistory` API has shifted
    its surface twice in the past 3 years; chromiumdash has been more stable.
  - Chrome Releases RSS (`chromereleases.googleblog.com/feeds/posts/default`)
    — versions appear in post titles as free text; parsing is fragile.
  - Scraping `chrome://version` from a headless browser — violates
    claude-rules MUST-NOT "No browser automation".

- **Probe job behaviour (`.github/workflows/chrome-ua-probe.yml`):**

  - Cron `0 0 1 * *` (1st of each month, 00:00 UTC) + `workflow_dispatch`
    for manual validation.
  - Step 1 — `curl` the endpoint, parse with `jq` (`-r '.[0].version'`).
  - Step 2 — extract the major (`cut -d. -f1`).
  - Step 3 — read `config.http.user_agent_chrome_version` from the test
    config or a constant; compare majors.
  - Step 4 — if our major is more than 3 minor-versions stale (a heuristic
    proxy for the 90-day horizon, since Chrome ships ~every 4 weeks), open
    an auto-PR via `peter-evans/create-pull-request@v6` bumping the value.
    The PR body cites both the stale value and the live value, plus the
    chromiumdash URL for human verification.
  - The job always exits 0 — the auto-PR IS the signal. A failed probe
    posts a workflow comment but does not fail CI; we don't want a probe
    outage to block merges.

- **Coupling note:** the probe checks one config field
  (`config.http.user_agent_chrome_version`), not the platform-specific
  `fake_os` / `fake_os_platform`. If the operator switches to macOS or
  Linux fingerprint, the probe URL's `platform=Windows` query param must
  be updated by hand. Tracked as Phase 13 polish (multi-platform probe).

---

## 2026-04-25 — Phase 12.a probe-validated; manual Chrome UA bump

**Context:** First `workflow_dispatch` of `.github/workflows/chrome-ua-probe.yml`
on commit `e1e4bab` (Romciiito/discord-scanner Actions run id 24935296446)
produced these values:

- Configured: `134.0.0.0` (major 134)
- Live (chromiumdash): `148.0.7778.56` (major 148)
- Major gap: 14 (well above the gap >= 3 threshold)

The probe correctly flagged staleness, ran the in-place regex bump on
`src/discord_scanner/config.py` in the runner, and called
`peter-evans/create-pull-request@v6` — which failed with
`GitHub Actions is not permitted to create or approve pull requests.`
That is a repo-level setting (Settings → Actions → General → Workflow
permissions → "Allow GitHub Actions to create and approve pull requests"),
not a workflow bug.

- **Decision:** apply the bump manually now rather than wait for the
  setting flip. Configured value goes from `134.0.0.0` to `148.0.7778.56`
  across:
  - `src/discord_scanner/config.py` (the runtime default)
  - `tests/conftest.py` test config fixture
  - `tests/test_config.py` `.startswith("148.")` assertion
  - `spec.md`, `seed-spec.md`, `docs/claude/architecture.md` example values

- **Decision:** the auto-PR mechanism is validated to the point where it's
  reasonable to trust monthly cron operation once the repo setting is
  flipped. The remaining unverified piece is the `peter-evans/create-pull-request`
  call itself — which is one of the most heavily used third-party Actions
  and behaves the same way across thousands of repos. Risk of the cron
  failing silently next month is low; if it does, the
  `actions/permissions/workflow` API + the `Allow create/approve PRs`
  setting needs operator action either way.

- **Carried forward (Phase 13 hygiene):** if the operator wants
  fully-autonomous monthly bumps, flip
  `Repo → Settings → Actions → General → Workflow permissions → Allow
  GitHub Actions to create and approve pull requests` to enabled, OR run
  `gh api -X PUT /repos/Romciiito/discord-scanner/actions/permissions/workflow
  -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true`.

- **Visibility note:** the repo flipped from PRIVATE to PUBLIC during this
  same session at the operator's explicit instruction (GitHub Actions
  billing block on the PRIVATE repo motivated the change). Public
  exposure of the anti-detection technique stack and operator identity
  (`Romciiito` ↔ burner-token Discord scraper) is an accepted risk per
  operator decision; security-model.md §8's accepted-risk model already
  covered the underlying Discord-ToS exposure.

