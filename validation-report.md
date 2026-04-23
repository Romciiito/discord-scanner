# Validation Report

Generated: 2026-04-23T00:00:00Z
Status: PASSED WITH NOTES

---

## Validator Note: stack-decision.md vs. design-decisions.md

The output-validator agent specification calls for `stack-decision.md` as the fourth required input. This file does not exist. The project uses `docs/claude/design-decisions.md` as a combined stack-selection + design-decisions document (authored by the stack-selector agent). All Check 3 verification was performed against that document. This is a naming deviation only; the content is fully present.

---

## Check 1 — Security Coverage

Every SEC-P0-01 through SEC-P0-32 item from `security-model.md §6` was checked against `architecture.md §5.1` (the threat-to-control cross-reference table) and the broader component descriptions in §2.

### Covered Items (32/32)

- [COVERED] SEC-P0-01: Arbitrary token source → `session/auth.py::load_token()` with exactly three enumerated sources. Architecture §5.1, §2.1 session/auth.py.
- [COVERED] SEC-P0-02: Shoulder-surfing on token entry → `getpass.getpass()` enforced in `store-token`; Typer echoing prompt explicitly forbidden. Architecture §5.1, §4.5.
- [COVERED] SEC-P0-03: Token leak via log record → `redact_token()` registered as structlog processor; architecture §5.1 maps to `logging_conf.py`. Processor order shown in §6.1.
- [COVERED] SEC-P0-04: Raw token pattern in repo → CI grep merge-blocker. Architecture §5.1, §8.2.
- [COVERED] SEC-P0-05: Token written to artefacts → integration test greps all artefacts; `session/auth.py` never serialises token outside keyring. Architecture §5.1.
- [COVERED] SEC-P0-06: Keyring plaintext fallback → `load_token()` inspects backend class name; refuses `keyrings.alt.*`. Architecture §5.1, §5.3.
- [COVERED] SEC-P0-07: Missing anti-detection headers → single header-build function in `session/rest.py` event_hook attaches all 17 headers; respx test. Architecture §5.1, §4.2.
- [COVERED] SEC-P0-08: Stale Chrome UA → `config.py` validator; staleness > 56 days exits 1. Architecture §5.1, §2.1 config.py.
- [COVERED] SEC-P0-09: X-Super-Properties blob integrity → `build_super_properties()` single source; unit test. Architecture §5.1, §5.2.
- [COVERED] SEC-P0-10: Gateway fingerprint drift / wss:// → `gateway.py` asserts wss scheme; properties built via same function. Architecture §5.1, §4.3.
- [COVERED] SEC-P0-11: HTTP/1.1 TLS fingerprint → `httpx.AsyncClient(http2=True)` single construction; unit test. Architecture §5.1.
- [COVERED] SEC-P0-12: Cross-burner cookie leak → `state/cookies-<keyring_username>.json` per-burner file. Architecture §5.1, §2.1 session/rest.py.
- [COVERED] SEC-P0-13: Concurrent gateway / second scan → `filelock` on `state/gateway-<burner>.lock` + `state/cursor.lock`. Architecture §5.1, §4.3.
- [COVERED] SEC-P0-14: Rate-limit violation → per-host token bucket + per-request jitter + burst pause. Architecture §5.1, §4.2.
- [COVERED] SEC-P0-15: 429 ignoring Retry-After → tenacity wired to read `Retry-After`; MAX_429_RETRIES=3. Architecture §5.1, §4.2.
- [COVERED] SEC-P0-16: Captcha auto-solve temptation → response hook detects `captcha_key|captcha_sitekey|captcha_service`; raises CaptchaDetected; cli maps to exit 2. Architecture §5.1.
- [COVERED] SEC-P0-17: SSRF via config or redirect → `httpx.event_hooks.request` validator; 3xx Location re-validated. Architecture §5.1, §5.4.
- [COVERED] SEC-P0-18: Disabled TLS verify → `httpx.AsyncClient(verify=True)` single site; CI grep. Architecture §5.1, §5.4.
- [COVERED] SEC-P0-19: Malicious attachment (exec-as-image) → MIME sniff first 16 bytes vs declared extension. Architecture §5.1, §4.4.
- [COVERED] SEC-P0-20: Zip-bomb / oversized attachment → streaming size cap at `max_size_mb * 1.1`. Architecture §5.1, §4.4.
- [COVERED] SEC-P0-21: Filename path traversal → `PurePosixPath(name).name`; strip null + forbidden chars; length cap 128; realpath within output_root. Architecture §5.1.
- [COVERED] SEC-P0-22: Local-host readability → `os.chmod(0o600)` on every file; `0o700` on every dir; best-effort on Windows documented. Architecture §5.1, §2.1 dump/.
- [COVERED] SEC-P0-23: Retention escape via symlink → `shutil.rmtree(..., followlinks=False)` replacement with islink + realpath check. Architecture §5.1, §2.1 retention.py.
- [COVERED] SEC-P0-24: Retention following symlinks → same walker as SEC-P0-23. Architecture §5.1.
- [COVERED] SEC-P0-25: Gateway/REST fingerprint drift → single `http_fingerprint` config block; merge-blocker unit test. Architecture §5.1, §5.2.
- [COVERED] SEC-P0-26: Implausible client_build_number → config validator: int >= 300000. Architecture §5.1.
- [COVERED] SEC-P0-27: Third-party PII docs missing → CI grep asserts section heading in `docs/claude/design-decisions.md`. Architecture §5.1.
- [COVERED] SEC-P0-28: output/ checked in → `.gitignore` contents asserted in CI. Architecture §5.1, §8.2.
- [COVERED] SEC-P0-29: Forbidden imports / raw tokens / verify=False → CI grep merge-blockers. Architecture §5.1, §8.2.
- [COVERED] SEC-P0-30: Unlocked dependencies → `uv.lock` committed; `pip install --require-hashes`. Architecture §5.1, §8.2.
- [COVERED] SEC-P0-31: SAST → `bandit -r src/ --severity-level medium`; ruff `S` rule-set. Architecture §5.1, §8.2.
- [COVERED] SEC-P0-32: .env hygiene → `.env.example` committed; `.env` gitignored. Architecture §5.1, §8.2.

### Missing Items

None. All 32 Phase 0 blocking checklist items are addressed in `architecture.md`.

---

## Check 2 — Requirements Coverage

All REQ-F, REQ-NF, REQ-INT, and REQ-OPS identifiers from `requirements.md` were checked against `architecture.md`.

### Mapped Requirements — Functional (REQ-F-001 to REQ-F-045)

- [MAPPED] REQ-F-001: Load invite list from Stage 1 artefact → `discovery/invite_resolve.py`; config.discovery.invites_input; pydantic validation; size cap 1 MB. Architecture §2.1 discovery/, §3.2 InviteCacheRow.
- [MAPPED] REQ-F-002: Apply score + intent filter → `discovery/` filter stage; config.discovery.filter. Architecture §2.1 discovery/.
- [MAPPED] REQ-F-003: Merge manual_invites → `discovery/` dedup logic; config.discovery.manual_invites. Architecture §2.1 discovery/.
- [MAPPED] REQ-F-004: Resolve invite → guild_id via Discord API → `discovery/invite_resolve.py`; GET /invites/{code}; invite_cache.sqlite. Architecture §2.1 discovery/, §4.2.
- [MAPPED] REQ-F-005: 7-day invite-resolution cache → invite_cache.sqlite with resolved_at; stale-row DELETE in transaction. Architecture §3.2 InviteCacheRow, §2.1 discovery/.
- [MAPPED] REQ-F-006: List guilds the burner has joined → `discovery/channels.py`; GET /users/@me/guilds; scannable-guild intersection. Architecture §2.1 discovery/, §4.2.
- [MAPPED] REQ-F-007: List channels per scannable guild → `discovery/channels.py`; GET /guilds/{id}/channels; type + override filter. Architecture §2.1 discovery/, §4.2.
- [MAPPED] REQ-F-008: Fetch role names (no member enumeration) → `discovery/channels.py`; GET /guilds/{id}/roles; /members forbidden. Architecture §4.2.
- [MAPPED] REQ-F-009: Paginate messages from channel → `fetch/messages.py`; `after={cursor}` pagination; max_messages_per_scan cap; per-request jitter. Architecture §2.1 fetch/, §4.2.
- [MAPPED] REQ-F-010: Fetch pinned messages → `fetch/pinned.py`; GET /channels/{id}/pins; pinned.jsonl. Architecture §2.1 fetch/, §4.2.
- [MAPPED] REQ-F-011: Fetch forum archived + active threads → `fetch/threads.py`; archived + active thread endpoints; threads.jsonl with parent_channel_id. Architecture §2.1 fetch/, discovery/forums.py, §4.2.
- [MAPPED] REQ-F-012: Inter-channel burst pause → asyncio.sleep(uniform(30,90)) between channels and guilds. Architecture §4.2 Rate shape.
- [MAPPED] REQ-F-013: Download image attachments → `fetch/attachments.py`; MIME sniff; size cap; filename sanitise; realpath check. Architecture §2.1 fetch/, §4.4.
- [MAPPED] REQ-F-014: Record non-image / oversized attachments URL-only → `fetch/attachments.py`; local_path=null; meta.attachments_skipped counters. Architecture §2.1 fetch/, §3.2 Attachment.
- [MAPPED] REQ-F-015: CDN downloader shares REST fingerprint → same UA/Sec-* headers; Authorization stripped; separate CDN token bucket. Architecture §4.4, §2.1 session/rest.py.
- [MAPPED] REQ-F-016: Connect gateway WebSocket → `session/gateway.py`; wss://gateway.discord.gg/?v=10&encoding=json. Architecture §2.1 session/gateway.py, §4.3.
- [MAPPED] REQ-F-017: Send OPCODE 2 IDENTIFY → `session/gateway.py`; properties blob matches REST X-Super-Properties. Architecture §4.3, §5.2.
- [MAPPED] REQ-F-018: Process HELLO heartbeat → `session/gateway.py`; clamp heartbeat_interval [1000,120000]; schedule OPCODE 1. Architecture §4.3.
- [MAPPED] REQ-F-019: Send OPCODE 3 PRESENCE UPDATE once → `session/gateway.py`; on READY, once; never re-sent. Architecture §4.3.
- [MAPPED] REQ-F-020: Do NOT process events (dormant) → `session/gateway.py`; only heartbeat_interval, session_id, sequence read; all other events discarded. Architecture §4.3.
- [MAPPED] REQ-F-021: OPCODE 6 RESUME on disconnect → `session/gateway.py`; RESUME with session_id+sequence; fresh IDENTIFY on RESUME failure. Architecture §4.3, §7.1 failure mode 9.
- [MAPPED] REQ-F-022: Gateway stays up for whole scan → `session/gateway.py`; not torn down between channels; continues REST-only if gateway fails after retry.attempts. Architecture §4.3, §7.1 failure mode 11.
- [MAPPED] REQ-F-023: Persist cursor per (guild_id, channel_id) → `cursor/state.py`; cursor.sqlite; atomic upsert after fsync; unchanged on crash. Architecture §2.1 cursor/, §3.2 CursorRow.
- [MAPPED] REQ-F-024: Single-writer filelock on state → `filelock.FileLock(state/cursor.lock)` timeout=0; second invocation exits 1. Architecture §2.1 cursor/, §4.3.
- [MAPPED] REQ-F-025: Write messages.jsonl.zst → `dump/jsonl_writer.py`; zstd-compressed; schema_version=1; byte-identical on re-run. Architecture §2.1 dump/, §3.2 Message.
- [MAPPED] REQ-F-026: Write pinned.jsonl (plain) → `dump/jsonl_writer.py`; plain JSONL; sorted (channel_id ASC, ts ASC, msg_id ASC). Architecture §2.1 dump/.
- [MAPPED] REQ-F-027: Write threads.jsonl (plain) → `dump/jsonl_writer.py`; plain JSONL; parent_channel_id populated. Architecture §2.1 dump/.
- [MAPPED] REQ-F-028: Write meta.json → `dump/jsonl_writer.py`; schema per seed-spec §7.2; all counters; channels_skipped[]; atomic write-then-rename. Architecture §2.1 dump/, §3.2 Meta.
- [MAPPED] REQ-F-029: Write prior.txt → `dump/` prior-scan-date logic; scans output/{guild_id}/ for most-recent prior date. Architecture §2.1 dump/.
- [MAPPED] REQ-F-030: Attachment directory → `output/{guild_id}/{date}/attachments/`; 0o700 dir; 0o600 files immediately. Architecture §2.1 dump/, §3.4.
- [MAPPED] REQ-F-031: `resolve` command → `cli.py`; --invite flag; rich-formatted table output; exit codes 0/1/2. Architecture §4.1.
- [MAPPED] REQ-F-032: `list-guilds` command → `cli.py`; GET /users/@me/guilds; exit 3 on 401. Architecture §4.1.
- [MAPPED] REQ-F-033: `scan` command (full) → `cli.py`; full guild sweep; all output artefacts; exit codes. Architecture §4.1.
- [MAPPED] REQ-F-034: `scan --guild` (scoped) → `cli.py`; scoped to specified guild. Architecture §4.1.
- [MAPPED] REQ-F-035: `daemon` command → `cli.py`; loop with scan_start_window + jitter; SIGINT/SIGTERM clean shutdown; exit propagation on captcha/ban. Architecture §4.1.
- [MAPPED] REQ-F-036: `status` command → `cli.py`; reads cursor.sqlite; zero HTTP. Architecture §4.1.
- [MAPPED] REQ-F-037: `store-token` command → `cli.py`; getpass.getpass(); keyring store; plaintext-backend refused; token not logged. Architecture §4.1, §4.5.
- [MAPPED] REQ-F-038: `version` command → `cli.py`; __version__ + git SHA + Python version; exit 0 always. Architecture §4.1.
- [MAPPED] REQ-F-039: Global flag `--config PATH` → `cli.py`; Path.resolve(); no `..` or system-path escape. Architecture §4.1.
- [MAPPED] REQ-F-040: Global flag `--verbose/-v` → `cli.py`; raises log level to DEBUG for invocation. Architecture §4.1.
- [MAPPED] REQ-F-041: Global flag `--dry-run` → `cli.py`; prints planned actions; zero HTTP/WS. Architecture §4.1.
- [MAPPED] REQ-F-042: Global flag `--offline` → `cli.py`; state-inspection only; zero HTTP/WS. Architecture §4.1.
- [MAPPED] REQ-F-043: Exit codes → `cli.py`; 0/1/2/3 with defined semantics; detected-ban heuristic (401 + config mtime). Architecture §4.1.
- [MAPPED] REQ-F-044: Captcha hard abort → `session/rest.py` response hook; exit 2; meta.json:errors[]; no further HTTP; gateway closed. Architecture §5.1 SEC-P0-16, §7.1 failure mode 5.
- [MAPPED] REQ-F-045: `retention --purge-all` command (Phase 1) → noted as Phase 1 in architecture §7.4; Phase 0 workaround documented. Architecture §7.4. [KNOWN GAP — Phase 1; acceptable per known-gaps section.]

### Mapped Requirements — Non-Functional (REQ-NF-001 to REQ-NF-072)

- [MAPPED] REQ-NF-001: Python 3.12+ → architecture §1.2 decision #1, §8.1.
- [MAPPED] REQ-NF-002: Library allowlist → architecture §1.2 decision #2-#11; design-decisions §4.
- [MAPPED] REQ-NF-003: Forbidden libraries CI grep → architecture §8.2 CI step 9.
- [MAPPED] REQ-NF-004: Package path → architecture §4.1 entry point.
- [MAPPED] REQ-NF-005: Cross-platform CI matrix → architecture §8.2.
- [MAPPED] REQ-NF-006..021: All 16 anti-detection headers → architecture §4.2 full header table; single header-build function in session/rest.py.
- [MAPPED] REQ-NF-022: HTTP/2 mandatory → architecture §2.1 session/rest.py `http2=True`.
- [MAPPED] REQ-NF-023: Cookie jar persistence per burner → architecture §2.1 session/rest.py, §3.4.
- [MAPPED] REQ-NF-024: MIME sniff → architecture §4.4 step 3, §5.1 SEC-P0-19.
- [MAPPED] REQ-NF-025: Streaming size cap → architecture §4.4 step 4, §5.1 SEC-P0-20.
- [MAPPED] REQ-NF-026: Filename sanitisation → architecture §4.4 step 5, §5.1 SEC-P0-21.
- [MAPPED] REQ-NF-027: Per-request jitter 1.5–4.0 s → architecture §4.2 Rate shape.
- [MAPPED] REQ-NF-028: Burst pause 30–90 s → architecture §4.2 Rate shape.
- [MAPPED] REQ-NF-029: Structured logging via structlog → architecture §6.1 full log schema.
- [MAPPED] REQ-NF-030: Token redaction helper → architecture §6.1 processor chain step 4, §5.1 SEC-P0-03.
- [MAPPED] REQ-NF-031: Invite-code redaction helper → architecture §6.1 processor chain step 5.
- [MAPPED] REQ-NF-032: Sort stability → architecture §2.1 dump/jsonl_writer.py; §7.2 idempotence section.
- [MAPPED] REQ-NF-033: Idempotence → architecture §7.2.
- [MAPPED] REQ-NF-034: Resumability → architecture §7.2; cursor advanced only after fsync.
- [MAPPED] REQ-NF-035: Pydantic schema tolerance → architecture §3.2 (all entities set `extra='allow'`).
- [MAPPED] REQ-NF-036: No NaN / Infinity in JSON → architecture §2.1 dump/jsonl_writer.py "rejects NaN / Infinity (raises)".
- [MAPPED] REQ-NF-037: File permissions 0o600/0o700 → architecture §5.1 SEC-P0-22, §2.1 dump/.
- [MAPPED] REQ-NF-038: Retention prune at scan start → architecture §2.1 retention.py.
- [MAPPED] REQ-NF-039: Attachment retention → architecture §2.1 retention.py; attachment_keep_days.
- [MAPPED] REQ-NF-040: URL allowlist (SSRF guard) → architecture §5.1 SEC-P0-17, §5.4.
- [MAPPED] REQ-NF-041: TLS verify=True always → architecture §5.1 SEC-P0-18, §5.4.
- [MAPPED] REQ-NF-042: SQL parameterisation → architecture §2.1 cursor/state.py `?` placeholders; §1.2 decision #5.
- [MAPPED] REQ-NF-043: Token loading enumerated → architecture §5.1 SEC-P0-01, §5.3.
- [MAPPED] REQ-NF-044: store-token uses getpass → architecture §5.1 SEC-P0-02, §4.5.
- [MAPPED] REQ-NF-045: Token never written to artefacts → architecture §5.1 SEC-P0-05.
- [MAPPED] REQ-NF-046: Keyring plaintext-fallback refused → architecture §5.1 SEC-P0-06, §5.3.
- [MAPPED] REQ-NF-047: Chrome-UA staleness check at load → architecture §5.1 SEC-P0-08, §2.1 config.py.
- [MAPPED] REQ-NF-048: Gateway fingerprint matches REST fingerprint → architecture §5.1 SEC-P0-25, §5.2.
- [MAPPED] REQ-NF-049: Plausible client_build_number → architecture §5.1 SEC-P0-26.
- [MAPPED] REQ-NF-050: Gateway concurrent-instance guard → architecture §5.1 SEC-P0-13, §4.3.
- [MAPPED] REQ-NF-051: No sync time.sleep in async → architecture §8.2 CI grep guard.
- [MAPPED] REQ-NF-052: No print() in src (rich in cli.py only) → architecture §8.2 CI grep guard; §2.2 cli.py.
- [MAPPED] REQ-NF-053: No bare except → architecture §2.2 (implied by mypy --strict + ruff E722/BLE001).
- [MAPPED] REQ-NF-054: No fcntl in src → architecture §8.2 CI grep guard.
- [MAPPED] REQ-NF-055: No raw discord.gg/ in logs → architecture §8.2 CI grep guard.
- [MAPPED] REQ-NF-056: No raw Discord token pattern in src/fixtures → architecture §8.2 CI grep guard.
- [MAPPED] REQ-NF-057: Per-host token bucket → architecture §4.2 Rate shape; §2.1 session/rest.py.
- [MAPPED] REQ-NF-058: 429 Retry-After honouring → architecture §4.2 Retry policy, §7.3.
- [MAPPED] REQ-NF-059: 5xx tenacity retry → architecture §4.2 Retry policy, §7.3.
- [MAPPED] REQ-NF-060: 403 skip, no retry → architecture §4.2 Error classes, §7.1 failure mode 3.
- [MAPPED] REQ-NF-061: 401 → detected-ban heuristic → architecture §4.2 Error classes, §7.1 failure mode 4.
- [MAPPED] REQ-NF-062: ruff check clean → architecture §8.2 CI step 3-4.
- [MAPPED] REQ-NF-063: mypy --strict clean → architecture §8.2 CI step 5.
- [MAPPED] REQ-NF-064: pytest coverage >= 70% → architecture §8.2 CI step 6.
- [MAPPED] REQ-NF-065: bandit severity-medium+ clean → architecture §8.2 CI step 7.
- [MAPPED] REQ-NF-066: pip-audit / Dependabot clean → architecture §8.2 CI step 8.
- [MAPPED] REQ-NF-067: Lock file committed + hash-require in CI → architecture §8.2 CI step 2.
- [MAPPED] REQ-NF-068: .env / .env.example hygiene → architecture §8.2 CI grep guard.
- [MAPPED] REQ-NF-069: output/ gitignored → architecture §8.2 CI grep guard.
- [MAPPED] REQ-NF-070: Third-party PII docs section present → architecture §5.1 SEC-P0-27, §8.2.
- [MAPPED] REQ-NF-071: Mid-sized guild E2E <= 20 min → architecture §2.2 session/ performance note (I/O-bound, asyncio concurrency).
- [MAPPED] REQ-NF-072: Timeout per request → architecture §4.2 (httpx.AsyncClient(timeout=config.http.timeout_sec)).

### Mapped Requirements — Integration (REQ-INT-001 to REQ-INT-011)

- [MAPPED] REQ-INT-001: Read invites.enriched.json on disk → architecture §1.1 system context diagram; §2.1 discovery/.
- [MAPPED] REQ-INT-002: No runtime coupling to civit-hf-scanner → architecture §2.1 discovery/; CI grep guard.
- [MAPPED] REQ-INT-003: Schema version of invites.enriched.json → architecture §2.1 discovery/ (pydantic load + schema_version check).
- [MAPPED] REQ-INT-004: Discord REST API v10 only → architecture §4.2.
- [MAPPED] REQ-INT-005: Discord Gateway v10 only → architecture §4.3.
- [MAPPED] REQ-INT-006: Discord CDN — separate host → architecture §4.4.
- [MAPPED] REQ-INT-007: Stage 2 → Stage 3 contract surface → architecture §3.4 data ownership table; §7.2 data consistency guarantees.
- [MAPPED] REQ-INT-008: Schema versioning → architecture §3.3 migration strategy; schema_version=1 per record.
- [MAPPED] REQ-INT-009: No runtime coupling to Stage 3 → architecture CI grep guard (no obsidian/anthropic/discord_curator imports).
- [MAPPED] REQ-INT-010: Keyring backend per OS → architecture §5.3.
- [MAPPED] REQ-INT-011: Keyring service + username keys → architecture §5.3; config.auth.keyring_service/username.

### Mapped Requirements — Operational (REQ-OPS-001 to REQ-OPS-006)

- [MAPPED] REQ-OPS-001: Per-HTTP-request log schema → architecture §6.1 (full per-request log event schema shown).
- [MAPPED] REQ-OPS-002: Per-gateway-event log schema → architecture §6.1 (per-gateway-event log event schema shown).
- [MAPPED] REQ-OPS-003: Per-scan-summary log record → architecture §6.1 `run.summary` event schema.
- [MAPPED] REQ-OPS-004: Log retention → architecture §6.2 (operator-managed; log file chmod 0o600 if configured).
- [MAPPED] REQ-OPS-005: Rate-limit-hit counter → architecture §6.2 meta.json counters.
- [MAPPED] REQ-OPS-006: Gateway-disconnect counter → architecture §6.2 meta.json counters.

### Unmapped Requirements

None.

### Gap Requirements

- [GAP] REQ-NF-071: Mid-sized guild E2E <= 20 min (Should priority)
  Issue: architecture.md does not include a concrete runtime estimate, load test, or simulation model demonstrating the 10–20 min target is achievable with the specified jitter parameters (per-request uniform(1.5,4.0) + burst pause uniform(30,90) × 10 channels = worst-case 15+ min in burst-pause alone, plus fetch time). The architecture notes the work is I/O-bound but does not verify the math.
  Recommended fix: Add a timing analysis note to architecture.md §6.2 or §2.2 showing the expected scan time envelope for the default config (10k messages × 10 channels). This is a "Should" requirement — workplan Phase 1 is appropriate. Do not block.

- [GAP] REQ-F-045: `retention --purge-all` command (Phase 1 priority)
  Issue: requirements.md marks this Should/Phase 1. Architecture §7.4 defers it to operator-manual instructions. The Phase 0 workaround (manual rm -rf) is documented but no architecture component owns it in Phase 0.
  Recommended fix: architecture.md already acknowledges this in the disaster recovery section. This matches the requirements.md "Phase 1" classification. Accept as known-and-documented.

---

## Check 3 — Stack Consistency

`docs/claude/design-decisions.md` (the stack-selector agent output) was checked against `architecture.md`.

**Note**: The agent spec calls for `stack-decision.md`. This project uses `docs/claude/design-decisions.md` as the authoritative stack document. No information gap results — all stack decisions are present.

### Consistent Items

- [OK] Runtime: both specify Python 3.12+.
- [OK] HTTP client: both specify `httpx[http2] >= 0.27` (async, single shared AsyncClient, http2=True, verify=True, event_hooks for URL allowlist).
- [OK] WebSocket: both specify `websockets >= 13` (hand-rolled OPCODE state machine, NOT discord.py).
- [OK] Auth / secret storage: both specify `keyring >= 24.0` with OS-native backends (DPAPI / Keychain / libsecret); plaintext-fallback refused.
- [OK] Database: both specify sqlite3 (stdlib) only — no ORM, no PostgreSQL, no SQLAlchemy. Two tables: cursor.sqlite and invite_cache.sqlite.
- [OK] Config and validation: both specify `pydantic >= 2.6` + `pydantic-settings >= 2.2`; `env_prefix="DISCORD_SCANNER_"`; `extra='allow'`.
- [OK] Compression: both specify `zstandard >= 0.22` for messages.jsonl.zst.
- [OK] File locking: both specify `filelock` (cross-platform) replacing `fcntl` (POSIX-only, forbidden).
- [OK] Retry: both specify `tenacity >= 8.2`.
- [OK] Logging: both specify `structlog >= 24.0` with redact_token + redact_invite_code processor chain.
- [OK] CLI framework: both specify `typer >= 0.12` + `rich >= 13.0` (rich confined to cli.py).
- [OK] Build backend: both specify `hatchling`.
- [OK] Auth pattern (outbound): both specify burner user token (keyring > env > config priority); no bot token, no OAuth.
- [OK] Forbidden imports: both enumerate the same set (requests, aiohttp, discord, anthropic, selenium, playwright, fcntl, obsidian-*).
- [OK] CI quality gates: both specify ruff + mypy --strict + pytest 70% + bandit + pip-audit on python-3.12 × {ubuntu, windows}.
- [OK] Process model: both specify single asyncio process, single event loop, filelock-enforced singleton.
- [OK] Deployment: both specify local pip install only; no Docker, no cloud.
- [OK] Rate limiting: both specify per-host token bucket (discord.com/api=2rps, cdn=1rps) + global asyncio.Semaphore(8) + per-request jitter.
- [OK] Anti-detection header count: design-decisions names "17 headers"; architecture §4.2 lists the same 17 (Authorization, User-Agent, Sec-Ch-Ua, Sec-Ch-Ua-Mobile, Sec-Ch-Ua-Platform, Sec-Fetch-Site, Sec-Fetch-Mode, Sec-Fetch-Dest, X-Super-Properties, X-Discord-Locale, X-Discord-Timezone, X-Debug-Options, Origin, Referer, Accept, Accept-Encoding, Accept-Language) + Content-Type (body only, effectively absent in GET-only Stage 2).

### Contradictions

None found.

---

## Summary

| Check | Status | Issues found |
|-------|--------|-------------|
| Security coverage | PASSED | 0 missing items (32/32 SEC-P0 items covered) |
| Requirements coverage | PASSED WITH NOTES | 0 unmapped; 1 minor gap (REQ-NF-071 timing math not verified; Should priority); 1 known-and-documented deferred item (REQ-F-045 Phase 1) |
| Stack consistency | PASSED | 0 contradictions (stack-decision.md absent but content present in docs/claude/design-decisions.md) |

**Overall: PASSED WITH NOTES**

### Notes accepted as known-and-documented

1. ToS §3 (self-bots) violation — explicitly accepted by operator in writing (spec.md §8.1, §10.1); treated as first-class threat in security-model.md, not a gap.
2. Burner-token-only auth path — by design; no inbound auth surface exists.
3. No LLM / Anthropic calls — Stage 3 territory; explicitly rejected in security-model.md §8.3 and requirements.md §3.5.
4. No Obsidian — Stage 3 territory; explicitly rejected.
5. Market analysis stub (market-analysis.md) — not a blocker for Stage 2 construction.
6. Stage 1.5 enrichment absent (`intent` filter empty-list path documented as "Stage-1.5 absent path" in REQ-F-002 notes).
7. REQ-F-045 retention --purge-all deferred to Phase 1 with documented Phase 0 workaround.
8. `stack-decision.md` file absent — content fully present in `docs/claude/design-decisions.md`; naming deviation only.

### Unresolved gaps requiring action before workplan-builder runs

None. All gaps are classified as known-and-documented or Should-priority deferred items.

### Recommended actions before workplan-builder runs

**None blocking.** workplan-builder may proceed immediately.

The workplan-builder SHOULD:
1. Include all 32 SEC-P0-## items as Phase 0 tasks, grouped by security-model.md §6 subsection headings.
2. Add a Phase 1 task for REQ-F-045 (`retention --purge-all --confirm` command).
3. Add a Phase 1 task for REQ-NF-071 timing validation (run a dry-run estimate against default config and add a note to docs).
4. Slot SEC-P1-01..SEC-P1-05 into Phase 1 (already called out in security-model.md §7).
5. Note `docs/claude/design-decisions.md` as the authoritative stack document (there is no separate `stack-decision.md`).
