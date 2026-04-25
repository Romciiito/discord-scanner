# Spec: discord-scanner (Stage 2)

**Status:** Foundation idea-refiner output. Transcribed + restructured from the frozen `brainstorm.md`, `seed-spec.md`, and `market-analysis.md`. Verbatim preservation of §4.1 anti-detection header list, §5 config surface, §6 CLI, §7 data contracts, and §10 acceptance criteria of the seed-spec. Downstream agents (requirements-engineer, security-analyst, architect, stack-selector, workplan-builder) should treat the seed-spec as authoritative where this document and it overlap.

---

## 1. Personas

### 1.1 Primary persona — "The Operator" (sole user)

- **Identity:** single project owner. Data engineer. Runs the tool on their own workstation (Windows 11 primary, macOS / Linux acceptable).
- **Environment:** Python 3.12+. No team. No shared infra. No server. One burner Discord account (disposable, joined manually to 5–20 target guilds, no DMs, no content authored).
- **Cadence:** weekly or on-demand runs against a curated invite list produced by the sister project `civit-hf-scanner` (Stage 1 + Stage 1.5 enrichment).
- **Consumes the output:** themselves, later, via a separate future tool `discord-curator` (Stage 3) that runs Claude over the JSONL dumps to build an Obsidian knowledge base.
- **Accepts the ToS risk in writing** (§8.1). The burner account is disposable.

### 1.2 Secondary persona — "Stage 3 Curator" (downstream consumer, separate future repo)

- **Identity:** a future program, not a person. Reads ONLY the `output/{guild_id}/{YYYY-MM-DD}/` directory contract (§8). Never reaches into Stage 2 internals, cache, or state files.
- **Contract surface:** `messages.jsonl.zst`, `pinned.jsonl`, `threads.jsonl`, `attachments/*`, `meta.json`, `prior.txt`. Versioned via `schema_version: 1`.

### 1.3 Non-personas (explicit exclusions)

- No multi-user operators. No hosted-service tenants. No admin / moderator of target guilds. No bot account. No end-consumers of the dumps beyond Stage 3.

---

## 2. Problem statement

The operator holds a ranked, intent-tagged list of Discord invite codes (`invites.enriched.json` from `civit-hf-scanner`). Each high-scoring invite corresponds to a guild whose **public text channels, announcement channels, forum channels, and threads** likely contain generative-AI workflow knowledge (prompts, configs, tutorials, collab notes). The operator cannot use a bot token (they don't own the guilds) and the prior tool (`dsc-smartscraper`) is fragile:

- Windows-incompatible (`fcntl`), stale Chrome UA, missing `X-Super-Properties`, no gateway session → burner account looks like a silent REST-only automator → detection risk.
- Monolithic `monitor.py` (570 LOC) entangles scraping with Obsidian / curation / learner concerns that belong in Stage 3.

`discord-scanner` rebuilds the scraping core with hardened anti-detection discipline, ejects curation to Stage 3, and promises a single typed on-disk contract (`messages.jsonl.zst` + sidecars) as the Stage 2 → Stage 3 interface.

---

## 3. Feature list

### 3.1 Invite resolution

- Read `invites.enriched.json` from `config.discovery.invites_input`.
- Filter on `score_pct ≥ config.discovery.filter.min_score_pct` (default 70) AND `intent ∈ config.discovery.filter.intent_allowlist` (default `[prompt_sharing, tutorials, workflows, collab]`).
- Merge `config.discovery.manual_invites`.
- For each unique invite: `GET /api/v10/invites/{code}?with_counts=true&with_expiration=true` via the burner account. This is the **single sanctioned Discord-side call for enrichment** in the whole pipeline (Stage 1 is forbidden from making it; Stage 2 must).
- Cache resolution `(invite_code → guild.id, guild.name)` for 7 days.

### 3.2 Guild enumeration

- `GET /api/v10/users/@me/guilds` — only scan guilds present in BOTH the resolved invite list AND this response (the burner must have been manually joined; no auto-join).
- `GET /api/v10/guilds/{guild_id}/channels` — list channels. Filter to types `0` (GUILD_TEXT), `5` (GUILD_ANNOUNCEMENT), `15` (GUILD_FORUM). Apply per-guild `include_channels` / `exclude_channels` overrides.
- `GET /api/v10/guilds/{guild_id}/roles` — role names for ID resolution. No member list calls.

### 3.3 Message + pinned + thread + forum fetching

- Per channel: `GET /api/v10/channels/{channel_id}/messages?limit=100&after={cursor}` paginated to end-of-new or `config.http.max_messages_per_scan` (default 10 000).
- Pinned: `GET /api/v10/channels/{channel_id}/pins`.
- Forum channels: `GET /api/v10/channels/{channel_id}/threads/public_archived_threads?limit=50` + active threads. Fetch messages of each.
- Per-request jitter `random.uniform(1.5, 4.0)` s. Inter-channel burst pause `random.uniform(30, 90)` s.

### 3.4 Attachment handling

- For each message with `attachments[]`:
  - Image (extension ∈ `config.attachments.image_extensions`, size ≤ `max_size_mb`): download to `output/{guild_id}/{date}/attachments/{msg_id}_{filename}`.
  - Else: record URL only; warn that Discord CDN URLs expire ~24 h (Stage 3 handles).
- CDN downloader uses the SAME full Chrome UA + header set as REST (no self-identifying bot UA). Separate rate budget for `cdn.discordapp.com` (default 1 req/s).

### 3.5 Dormant gateway session (anti-detection #2)

- `wss://gateway.discord.gg/?v=10&encoding=json`.
- OPCODE 2 IDENTIFY with realistic `properties` (os / browser / browser_version / client_build_number / release_channel / system_locale / browser_user_agent matching the REST UA).
- Receive HELLO (OPCODE 10) → schedule OPCODE 1 HEARTBEAT per `heartbeat_interval`.
- Receive READY → send OPCODE 3 PRESENCE UPDATE `{status: "online", activities: [], afk: false}`.
- **Do not process events** — stay connected, heartbeat, hold presence.
- On disconnect: attempt OPCODE 6 RESUME with `session_id` + `sequence`; fall back to fresh IDENTIFY on failure.
- Session maintained for the entire scan duration, not torn down between channels.

### 3.6 Rate-limit and error handling

- Per-host token bucket (default `discord.com/api: 2 req/s`, `cdn.discordapp.com: 1 req/s`).
- 429: honour `Retry-After`; exponential backoff on repeats; abort channel after `MAX_429_RETRIES=3` consecutive same-endpoint 429s.
- 5xx: tenacity retry to `config.retry.attempts` (default 5).
- 401/403 + body contains `captcha_key` or `captcha_sitekey`: **hard abort scan** (`captcha_action: abort` default; `notify_and_wait` alternative). Plain 403: log warning, skip channel, continue.

### 3.7 Cursor / resumability

- `state/cursor.sqlite` keyed by `(guild_id, channel_id) → last_message_id`. Atomic updates via `filelock` (cross-platform; replaces `fcntl`). Cursor advances AFTER a channel's messages are successfully dumped.
- Network drop mid-channel → cursor unchanged for that channel → re-run resumes from last-seen boundary. Zero duplicates.

### 3.8 Output artefacts (Stage 2 → Stage 3 contract)

Under `output/{guild_id}/{YYYY-MM-DD}/`:
- `messages.jsonl.zst` — zstandard-compressed JSONL, one message per line. Schema §7.1.
- `pinned.jsonl` — plain JSONL, same schema.
- `threads.jsonl` — plain JSONL, same schema, `parent_channel_id` populated.
- `attachments/{msg_id}_{filename}` — downloaded image files only.
- `meta.json` — §7.2.
- `prior.txt` — date string of previous scan of this guild (empty on first scan).

### 3.9 CLI commands (verbatim from seed-spec §6)

Entry point: `discord-scanner`. Commands:

- `discord-scanner resolve` — takes invite codes (from config or `--invite`), calls `/api/v10/invites/{code}`, prints `guild_id + guild_name` per invite, caches resolutions.
- `discord-scanner list-guilds` — print all guilds the burner is joined to.
- `discord-scanner scan` — full scan of all configured guilds.
- `discord-scanner scan --guild <guild_id>` — scan one guild only.
- `discord-scanner daemon` — loop: scan → sleep `interval_hours ± jitter` → scan.
- `discord-scanner status` — print cursor state per channel.
- `discord-scanner store-token` — prompt for burner token via getpass, store in keyring.
- `discord-scanner version`.

Global flags: `--config PATH`, `--verbose/-v`, `--dry-run`, `--offline` (state-inspection only).
Exit codes: `0` success, `1` user/config error, `2` runtime (network/captcha), `3` detected-ban (token invalid + no recent change).

### 3.10 Daemon mode

- `interval_hours: 168` (weekly) default ± `jitter_hours: [-1, 2]`.
- `scan_start_window: "02:00-06:00 UTC"` — randomised start within the window (anti-detection #5).

### 3.11 Retention

- `retention.raw_dump_keep_days` (default 30) auto-deletes old `output/{guild_id}/` subtrees at scan start.
- `retention.attachment_keep_days` (default 14).

---

## 4. Anti-detection discipline (non-negotiable, verbatim from seed-spec §4)

### 4.1 Full Discord REST header set (verbatim)

Every REST request includes:
- `Authorization: <token>` (no `Bearer` prefix for user tokens)
- `User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/<VER>.0.0.0 Safari/537.36` — `<VER>` from `config.http.user_agent_chrome_version` (default: current stable minus ~3 weeks).
- `Sec-Ch-Ua: "Chromium";v="<VER>", "Google Chrome";v="<VER>", "Not?A_Brand";v="99"`
- `Sec-Ch-Ua-Mobile: ?0`
- `Sec-Ch-Ua-Platform: "Windows"` (or matching `config.http.fake_os`)
- `Sec-Fetch-Site: same-origin`
- `Sec-Fetch-Mode: cors`
- `Sec-Fetch-Dest: empty`
- `X-Super-Properties: <base64(JSON blob)>` — blob contains `{os, browser, browser_version, os_version, device, browser_user_agent, system_locale, client_build_number, release_channel: "stable"}`
- `X-Discord-Locale: en-US` (configurable)
- `X-Discord-Timezone: Europe/Prague` (configurable)
- `X-Debug-Options: logGatewayEvents`
- `Content-Type: application/json` (for requests with body; GETs don't set it)
- `Origin: https://discord.com`
- `Referer: https://discord.com/channels/@me`
- `Accept: */*`
- `Accept-Encoding: gzip, deflate, br`
- `Accept-Language: en-US,en;q=0.9`

### 4.2 HTTP/2 mandatory

`httpx.AsyncClient(http2=True)`. HTTP/1.1 is TLS-layer distinguishable from real clients.

### 4.3 Gateway WebSocket (see §3.5)

Silent REST-only is a detection anomaly; dormant gateway session fixes it.

### 4.4 Cookie persistence

`httpx.AsyncClient` initialised with `cookies=<persistent jar>` loaded from `state/cookies.json` per burner account. `__dcfduid`, `__sdcfduid`, `locale` cookies persist across requests like a real session.

### 4.5 Randomised behaviour

- Per-request jitter `random.uniform(1.5, 4.0)` s between REST calls.
- Burst pause `random.uniform(30, 90)` s between channels.
- Daemon jitter `interval_hours + random.uniform(-1, 2)` h.
- Scan start window random point in `config.daemon.scan_start_window`.

---

## 5. Config surface (verbatim from seed-spec §5)

```yaml
run:
  log_level: info
  output_root: ./output
  state_root: ./state

auth:
  token_source: keyring       # keyring | env | config | pool
  token_pool: null             # list[str], used if token_source == pool
  rotate_every: per_server     # per_request | per_server | per_scan
  keyring_service: discord-scanner
  keyring_username: burner-1

gateway:
  enabled: true                # false = REST-only (more detectable)
  heartbeat_interval_ms: null  # null = use HELLO-provided
  presence: online             # online | idle | dnd | invisible
  resume_on_drop: true

http:
  http2: true
  timeout_sec: 30
  per_host_rate_per_sec:
    "discord.com/api": 2
    "cdn.discordapp.com": 1
  per_channel_delay_sec: [1.5, 4.0]
  burst_pause_sec: [30, 90]
  max_messages_per_scan: 10000
  user_agent_chrome_version: "148.0.7778.56"
  fake_os: "Windows NT 10.0; Win64; x64"
  fake_os_platform: "Windows"
  locale: "en-US"
  timezone: "Europe/Prague"

retry:
  attempts: 5
  backoff_initial_sec: 2
  backoff_max_sec: 60
  retry_on_status: [429, 500, 502, 503, 504]
  captcha_action: abort        # abort | notify_and_wait

discovery:
  invites_input: ../civit-hf-scanner/output/latest/invites.enriched.json
  filter:
    min_score_pct: 70
    intent_allowlist: [prompt_sharing, tutorials, workflows, collab]
    exclude_nsfw: false
  manual_invites: []

channels:
  # Per-server overrides
  # "{guild_id}":
  #   include_channels: [...]
  #   exclude_channels: [...]
  #   include_archived_threads: true
  #   max_thread_history: 500

attachments:
  download_images: true
  image_extensions: [.png, .jpg, .jpeg, .webp, .gif]
  max_size_mb: 20
  download_cdn_ua_matches_rest: true

daemon:
  interval_hours: 168          # weekly
  jitter_hours: [-1, 2]
  scan_start_window: "02:00-06:00 UTC"

retention:
  raw_dump_keep_days: 30
  attachment_keep_days: 14
```

---

## 6. CLI surface

See §3.9. Verbatim preserved from seed-spec §6.

---

## 7. Data contracts (verbatim from seed-spec §7)

All pydantic v2 models with `extra='allow'`.

### 7.1 Message (one line in `messages.jsonl.zst` / `pinned.jsonl` / `threads.jsonl`)

```json
{
  "schema_version": 1,
  "guild_id": "123",
  "channel_id": "456",
  "channel_name": "workflow-tips",
  "channel_type": "text",
  "parent_channel_id": null,
  "message_id": "789",
  "author_id": "user_abc",
  "author_name": "alice",
  "author_discriminator": "0001",
  "content": "...",
  "timestamp": "2026-04-23T12:00:00+00:00",
  "edited_timestamp": null,
  "reactions": [
    {"emoji": "👍", "count": 4},
    {"emoji": ":fire:", "count": 2, "animated": false, "id": "..."}
  ],
  "attachments": [
    {
      "id": "att_1",
      "filename": "result.png",
      "content_type": "image/png",
      "size": 123456,
      "cdn_url": "https://cdn.discordapp.com/...",
      "local_path": "attachments/789_result.png"
    }
  ],
  "mentions": {"users": ["..."], "roles": ["..."]},
  "reply_to_message_id": null,
  "thread_id": null,
  "pinned": false,
  "flags": 0
}
```

### 7.2 `meta.json` (per guild per date)

```json
{
  "schema_version": 1,
  "guild_id": "123",
  "guild_name": "AI Art Server",
  "scan_started_at": "2026-04-23T02:15:00+00:00",
  "scan_completed_at": "2026-04-23T03:42:18+00:00",
  "channels_scanned": ["456", "457", "458"],
  "channels_skipped": [{"id": "459", "reason": "403_no_permission"}],
  "message_count": 8432,
  "pinned_count": 47,
  "thread_count": 112,
  "attachment_count": 1291,
  "attachments_downloaded": 1180,
  "attachments_skipped": [
    {"reason": "non_image", "count": 87},
    {"reason": "size_cap", "count": 24}
  ],
  "rate_limit_hits": 3,
  "gateway_disconnects": 0,
  "gateway_resumes": 0,
  "errors": [],
  "cursor_file": "cursor.sqlite",
  "prior_scan_date": "2026-04-16"
}
```

### 7.3 `prior.txt`

One line: previous scan's `YYYY-MM-DD` for this guild, or empty on first scan.

---

## 8. Constraints

### 8.1 Terms-of-Service risk — FIRST-CLASS, NOT BURIED

> **Using a Discord user account token for automated scraping is against Discord's Terms of Service (§3 self-bots). The operator has explicitly accepted this risk in writing.** All of §4 (full header set, HTTP/2, dormant gateway, cookie jar, randomised behaviour), §3.5 (gateway), §3.6 (captcha abort), §3.11 (retention), and the burner-account discipline (disposable account, manually joined, no DMs, no authored content, VPN recommended, weekly rotation optional) are mitigations layered to keep the burner usable for at least 3 months of weekly runs — best-effort; Discord's anti-abuse is a moving target. A ban is an accepted outcome, not a defect. Security-analyst downstream MUST model this as threat input #1.

### 8.2 Runtime + language lock

- Python 3.12+. Windows 11 primary dev target; macOS / Linux acceptable.
- No code outside `src/discord_scanner/`.

### 8.3 Library allowlist (merge-blockers enforced by CI grep)

**Required:**
- `httpx[http2]>=0.27`, `websockets>=13`, `tenacity>=8.2`, `pydantic>=2.6`, `pydantic-settings>=2.2`, `keyring>=24.0`, `zstandard>=0.22`, `filelock`, `typer>=0.12`, `rich>=13.0`, `structlog>=24.0`, `sqlite3` (stdlib), `pytest>=8`, `pytest-asyncio>=0.23`, `respx>=0.21`, `pytest-websocket` (or hand-rolled WS fixtures), `ruff>=0.4`, `mypy>=1.10` (strict), `hatchling`.

**Forbidden (CI grep merge-blockers):** `requests`, `aiohttp`, `discord.py`, `discord.py-self`, `pycord`, `selenium`, `playwright`, `obsidian-*`, `anthropic` (this repo is pre-LLM).

### 8.4 Network constraints (SSRF allowlist)

- Outbound ONLY to `discord.com`, `cdn.discordapp.com`, `gateway.discord.gg`, `media.discordapp.net`. Any other host: exit 1 with SSRF-labelled error.
- `httpx.AsyncClient(verify=True)` always. `verify=False` anywhere in `src/` is a merge-blocker.

### 8.5 Boundary — what Stage 2 must NOT do

| Forbidden | Belongs in |
|---|---|
| Semantic classification (intent / topic / noise vs. useful) | Stage 3 |
| LLM / embedding / Claude / Anthropic calls | Stage 3 |
| Obsidian vault writes, note templates, markdown synthesis | Stage 3 |
| Scoring, weights, learner | Stage 3 |
| Per-message `keep` / `discard` decision | Stage 3 |
| Topic clustering, author profiles | Stage 3 |
| Interactive review UI / FastAPI | Stage 3 |
| Model-weight or binary-blob downloads (non-image) | — |
| POST / PATCH / DELETE / react / type / join / DM on Discord | — |
| `/guilds/{id}/members` enumeration | — |
| Auto-joining guilds (operator joins manually with burner) | — |
| Bot-token operation (impossible — no admin access) | — |

### 8.6 Secrets

- Burner token loaded ONLY via `keyring` OR `DISCORD_TOKEN` env var OR `config.yaml.discord_token` (last-resort, warned).
- Never logged at any level. Never written to cache. Never written to output.
- `redact_token(t) -> "{t[:6]}***{t[-4:]}"` on any synthetic token reference in logs.
- Invite code redaction in logs: `redact_invite_code(c) -> "{c[:2]}***{c[-2:]}"`. CI grep forbids raw `discord.gg/` or full invite code in any `logger.*` / `structlog.*` call.

### 8.7 SQL + locking

- Every SQLite query uses `?` placeholders. Zero string-interpolated SQL.
- Cross-platform cursor + cookie locking via `filelock` (NOT `fcntl` — it's POSIX-only and breaks on Windows).
- `state/cursor.sqlite` and `state/cookies.json` chmod `0o600` best-effort (documented degradation on Windows).

### 8.8 Sort stability / determinism

- Message ordering within any emitted file: `(channel_id ASC, timestamp ASC, message_id ASC)`.
- JSON dict keys alphabetised.
- Two consecutive runs on unchanged cursor state → byte-identical decompressed JSONL content. (Raw zstd bytes may differ due to dictionary state; equality asserted after decompress.)

### 8.9 Schema tolerance

- Every pydantic model uses `model_config = ConfigDict(extra='allow')`.
- `ValidationError` on one message → WARNING + skip that message, run continues.

### 8.10 Output hygiene

- Writes only under `output/{guild_id}/{date}/` and `state/` and `attachments/`.
- No NaN / Infinity in JSON output (raise instead).
- No `print()` in `src/` (`rich.console.Console` allowed only in `cli.py` for user-facing output). Use structlog logger.
- No bare `except:`. No commented-out code in merged commits.

---

## 9. Non-functional requirements

### 9.1 Performance

- End-to-end scan of a mid-sized guild (~10 000 new messages across ~10 channels) completes in 10–20 min.
- Per-host rate: 2 req/s to `discord.com/api`, 1 req/s to `cdn.discordapp.com`.
- `asyncio` throughout; no sync `time.sleep` inside `async def` (CI grep merge-blocker).

### 9.2 Reliability / resumability

- Network drop mid-channel → re-run resumes from the last persisted cursor, no duplicates.
- Gateway session stays up for the entire scan; disconnect triggers OPCODE 6 RESUME, falls back to fresh IDENTIFY only on failure.
- Captcha detected → abort cleanly with operator-actionable runbook entry (no auto-solve).

### 9.3 Observability

- Every HTTP request logs `{stage, source, url_hash, cache_hit (N/A), http_status, duration_ms, attempt}`.
- Every gateway event logs `{stage, event_type, timestamp}`.
- Per-guild summary log at end-of-scan with full counters (matches `meta.json`).
- All log output respects redaction rules (§8.6).

### 9.4 Idempotence

- Two consecutive scans with unchanged cursor produce byte-identical decompressed `messages.jsonl` content (see §8.8).

### 9.5 Portability

- Runs on Windows 11 / macOS / Linux. CI matrix `python-3.12 × {ubuntu-latest, windows-latest}`.
- No `fcntl` anywhere in `src/` (`filelock` mandatory).

### 9.6 Quality gates (merge-blockers)

- `ruff check src tests` — 0 errors.
- `ruff format --check src tests` — clean.
- `mypy --strict src` — 0 errors.
- `pytest -q --cov=src/discord_scanner --cov-fail-under=70` — green.
- CI grep guards: no `import requests`, `import aiohttp`, `import discord`, `import anthropic`, `from selenium`, `from playwright`; no `verify=False`; no `time.sleep` inside `async def`; no raw `discord.gg/` or full invite code in `logger.*`; no raw Discord token pattern (55+ char `[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`) in `src/`.

### 9.7 Observability / telemetry

Structured logs via `structlog` only. No telemetry exfiltration. No anonymous usage stats. Pure local operation.

### 9.8 Retention / storage

- `raw_dump_keep_days: 30`, `attachment_keep_days: 14` by default. Auto-prune old `output/{guild_id}/` subtrees at scan start.

---

## 10. Risks

### 10.1 First-class risk: Discord Terms-of-Service violation

- **Probability:** certain (the use case IS the violation).
- **Impact:** burner account ban (expected cost) → new burner setup → 1–2 h operator time.
- **Mitigations:** every item in §4 (anti-detection) + burner discipline (disposable account, manually joined, no authored content, VPN recommended, weekly rotation optional).
- **Residual:** Discord's anti-abuse evolves; expect periodic UA / header refresh (monthly CI job checks Chrome version currency).
- **Acceptance:** operator has accepted in writing; a ban is a cost of doing business, not a defect of this tool.

### 10.2 Detection landscape drift

- Chrome UA rots. `X-Super-Properties` blob drifts. Client-hint format changes.
- Mitigation: monthly CI probe of current Chrome stable; alert when `config.http.user_agent_chrome_version` is > 8 weeks behind stable.

### 10.3 Captcha challenge mid-scan

- Mitigation: abort cleanly. Operator runbook covers rotation to fresh burner.

### 10.4 CDN URL expiry (~24 h)

- Non-issue for Stage 2 (we download images; non-images we emit URLs only and flag in `meta.json`). Stage 3's problem if it defers fetching.

### 10.5 Cursor file corruption

- Mitigation: `filelock` single-writer discipline; sqlite WAL; atomic commit after successful channel dump.

### 10.6 Schema drift from Discord API

- Mitigation: `extra='allow'` on every pydantic model; `ValidationError` on one message skips that message, run continues.

### 10.7 False sense of safety

- These mitigations are best-effort. No guarantee. The operator accepts that at any point Discord may change detection heuristics and ban the burner without warning.

### 10.8 Forbidden-library regression

- Mitigation: CI grep merge-blockers (§9.6).

---

## 11. Acceptance criteria (verbatim from seed-spec §10, all 15)

1. `pip install -e ".[dev]"` succeeds on Python 3.12 (Windows + Linux CI).
2. `ruff check`, `ruff format --check`, `mypy --strict src`, `pytest --cov` all green.
3. `discord-scanner --help` shows all 8 commands + all global flags.
4. `discord-scanner store-token` prompts via getpass, stores in keyring (platform-appropriate backend), token round-tripped via `discord-scanner list-guilds`.
5. `discord-scanner resolve --invite aaaabbbb` returns `guild.id` + `guild.name` from a mocked Discord invite endpoint response.
6. `discord-scanner scan --dry-run` prints planned actions (N invites × M guilds × K channels), makes zero HTTP calls.
7. Mocked live scan (respx for REST + WebSocket mock for gateway) against fixture payloads: one guild × 2 channels × 30 messages each → writes `output/{guild_id}/{YYYY-MM-DD}/messages.jsonl.zst` + auxiliaries; line count matches.
8. Gateway WebSocket mock test: IDENTIFY → HELLO → HEARTBEAT schedule verified.
9. Idempotence: two consecutive mocked scans with unchanged cursor produce byte-identical decompressed `messages.jsonl` content.
10. `discord-scanner scan` with `--offline` inspects cursor state without making any HTTP/WS call.
11. State file `cursor.sqlite` contains correct per-channel last_message_id after a scan.
12. Full Discord header set verified present on every mocked REST request.
13. Grep guards pass: no forbidden imports, no raw token pattern, no `verify=False`, no raw invite in logs, no sync `time.sleep` in async code.
14. Schema drift tolerance: mocked response with an unexpected extra field on message object → pydantic accepts (`extra='allow'`), message makes it to dump.
15. CDN image download: mocked CDN fetch writes file to correct path with correct filename; non-image attachments record URL only.

---

## 12. Out of scope (explicit non-goals)

- Semantic classification, intent labelling, topic clustering, author profiles, learner weights (Stage 3).
- Claude / LLM / embedding calls of any kind (Stage 3).
- Obsidian writes, note templates, markdown synthesis (Stage 3).
- Per-message `keep` / `discard` UI, FastAPI review server (Stage 3).
- Multi-user / multi-tenant / hosted operation.
- Bot-token operation (no admin access on target guilds).
- Auto-joining guilds (operator joins manually with the burner).
- Content summarisation, digests, webhooks.
- Data-lifecycle management / analytics beyond `retention.*`.

---

## 13. Inheritance from `dsc-smartscraper`

Port with minor edits (≥90% reuse):
- `client.py` → `session/rest.py` — replace `urllib` with `httpx[http2]`, add full §4.1 header set.
- `keychain.py` → `session/auth.py` — replace macOS-only Keychain + plaintext fallback with cross-platform `keyring`.
- `state.py` → `cursor/state.py` — migrate JSON-per-file to sqlite; replace `fcntl` with `filelock`.

Rewrite from scratch:
- `monitor.py::run_scan` (570 LOC) — split into `session/`, `discovery/`, `fetch/`, `cursor/`, `dump/`.

Move wholesale to Stage 3 (future separate repo):
- `filters.py`, `categorizer.py`, `scorer.py`, `tags.py`, `image_context.py`, `context.py`, `learner.py`, `web/app.py`, `obsidian.py`, `topics.py`, `digest.py`.

Expect ~40% of the old repo's 1,250 tests to port to Stage 2.

---

## Known Gaps

- **Path to `invites.enriched.json`** — seed-spec defaults to `../civit-hf-scanner/output/latest/invites.enriched.json`. Relative path assumes sibling-directory layout on operator's workstation. No cross-repo contract test in this repo; breakage surfaces at first real run. Acceptable given single-user scope.
- **Stage 1.5 enrichment not yet shipped** — the `intent` and `confidence` fields on `invites.enriched.json` are a blocker for meaningful `intent_allowlist` filtering (brief §12.1). Scanner can still run against raw `invites.json` with `intent_allowlist` effectively disabled (empty / permissive), but the default config assumes enrichment is live.
- **Monthly Chrome-version CI** — planned (seed-spec §8 test strategy), but the specific source-of-truth for "current Chrome stable" is not pinned. Recommend a deterministic endpoint (e.g. `https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Windows`) to be decided in architect / workplan phase.
- **Token-pool rotation semantics** — `auth.rotate_every: per_request | per_server | per_scan` is specified, but the interaction with gateway session identity (one gateway per burner at a time vs. many-at-once) is not fully fleshed. Default path (`pool size 1` / `per_server`) is unambiguous; multi-token pool is a post-MVP concern.
- **`detected-ban` exit code 3 heuristic** — the exact signal for "token invalid + no recent config change" is not pinned (could be 401 on `list-guilds` + cursor-file mtime within N hours). To be detailed in requirements-engineer output.
- **Retention interaction with in-flight daemon** — if `retention.raw_dump_keep_days` prunes at scan start while a prior scan's output is still consumed by Stage 3, Stage 3 could race. Out of scope for Stage 2; Stage 3's problem to snapshot before consuming.
