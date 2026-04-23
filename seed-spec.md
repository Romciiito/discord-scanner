# Seed Spec: discord-scanner (Stage 2)

**Status:** FROZEN — this is the authoritative technical specification. Downstream agents (idea-refiner, requirements-engineer, architect, stack-selector, workplan-builder) MUST treat this document as the source of truth. Copy details into your own outputs verbatim when needed. DO NOT ask clarifying questions about anything covered here. If a gap exists, note it in a "Known Gaps" section in your own output and proceed; do not halt.

---

## 1. Summary

`discord-scanner` is a Python 3.12+ CLI that, given a curated list of Discord invite codes (filtered from `civit-hf-scanner/output/latest/invites.enriched.json`), connects via a **burner Discord user account token** with strong anti-detection discipline, enumerates configured public channels / forums / threads of each target guild, fetches message history + pinned messages + forum posts + image attachments, and writes **compressed raw JSONL dumps per guild per day**. Pure mechanical scraping — zero LLM, zero Obsidian integration, zero classification. Idempotent. Resumable. Rate-limit-clean. Re-runnable. The output is consumed by a separate future repo `discord-curator` (Stage 3).

## 2. Functional scope

### 2.1 Invite resolution

- Input: `civit-hf-scanner/output/latest/invites.enriched.json` (path configurable). Filter internally on `score_pct ≥ config.discovery.filter.min_score_pct` (default 70) **AND** `intent ∈ config.discovery.filter.intent_allowlist` (default `[prompt_sharing, tutorials, workflows, collab]`).
- Plus `config.discovery.manual_invites: list[str]` for codes not from Stage 1.
- For each unique invite code: `GET /api/v10/invites/{code}?with_counts=true&with_expiration=true` (this ONE Discord API call is the ONE exception to the "no Discord API enrichment" rule of Stage 1 — here it's essential to resolve invite → guild_id, and it's performed by the burner account not by civit-hf-scanner).
- Extract `guild.id` + `guild.name`. Cache the resolution for 7 days.

### 2.2 Guild enumeration

- `GET /api/v10/users/@me/guilds` — list guilds the burner has joined. Only scan guilds that are BOTH in the resolved invite list AND the @me/guilds response (defensive: the user must manually join the target guilds with the burner first; the tool does NOT auto-join).
- `GET /api/v10/guilds/{guild_id}/channels` — list channels. Filter to accessible text channels + forum channels (type 0 = GUILD_TEXT, type 5 = GUILD_ANNOUNCEMENT, type 15 = GUILD_FORUM). Respect per-server `include_channels` / `exclude_channels` config overrides.
- `GET /api/v10/guilds/{guild_id}/roles` — fetch role names once for ID resolution. No member list calls.

### 2.3 Message fetching

- For each accessible channel:
  - `GET /api/v10/channels/{channel_id}/messages?limit=100&after={last_message_id_from_cursor}` — paginate until fewer than 100 returned (end of new messages) or `max_messages_per_scan` cap (default 10 000 per channel).
  - Per-request 1.5–4.0 s jitter + burst pause 30–90 s at end of channel.
- For forum channels: additionally `GET /api/v10/channels/{channel_id}/threads/public_archived_threads?limit=50` + active threads. Fetch each thread's messages.
- Pinned messages: `GET /api/v10/channels/{channel_id}/pins` per channel.

### 2.4 Attachment handling

- For each message with `attachments[]`:
  - Images (`.png, .jpg, .jpeg, .webp, .gif`, configurable) under `max_size_mb` (default 20): download to `output/{guild_id}/{date}/attachments/{msg_id}_{filename}`.
  - Non-images or oversized: record URL only (warn: CDN URL expires in ~24 h — Stage 3 handles).
- Downloader uses the SAME realistic Chrome UA as REST (anti-pattern in the old scraper was a self-identifying bot UA). Separate rate-limit budget for `cdn.discordapp.com` (default 1 req/s, configurable).

### 2.5 Gateway session (dormant-but-present)

- **Purpose:** anti-detection. A burner account making REST API calls without any gateway session is a strong automation signal.
- Connect to `wss://gateway.discord.gg/?v=10&encoding=json`.
- OPCODE 2 (IDENTIFY) with realistic `properties` payload: `{os: "Windows", browser: "Chrome", browser_version: "<config.http.user_agent_chrome_version>", device: "", system_locale: "en-US", browser_user_agent: "<matching UA string>", client_build_number: <plausible>, release_channel: "stable"}`.
- Receive HELLO (OPCODE 10), respect `heartbeat_interval`. Send OPCODE 1 (HEARTBEAT) on schedule.
- Once READY received, send OPCODE 3 (PRESENCE UPDATE) with `{status: "online", activities: [], afk: false}`.
- **Do NOT process events** — just stay connected, heartbeat, maintain presence.
- On disconnect: try OPCODE 6 (RESUME) with session_id + sequence; fall back to fresh IDENTIFY if resume fails.
- Gateway session is maintained for the entire scan duration. It is NOT torn down between channel iterations.

### 2.6 Rate-limit discipline

- Per-host token bucket `per_host_rate_per_sec` (default 2 for `discord.com/api`, 1 for `cdn.discordapp.com`).
- 429 handling: respect `Retry-After` header + exponential backoff on repeat 429s. After `MAX_429_RETRIES=3` consecutive 429s on same endpoint, abort the channel (move to next).
- 5xx handling: tenacity retry, up to `config.retry.attempts` (default 5).
- 403/401: captcha detection — if response body contains `captcha_key` or `captcha_sitekey`, abort scan with clear error. Plain 403 logs warning and continues to next channel.

### 2.7 State / cursor

- `state/cursor.sqlite` keyed by `(guild_id, channel_id) → last_message_id`. Atomic updates via `filelock` (cross-platform). Updated AFTER successful message dump for a channel.
- On crash mid-scan, re-run resumes from the last successfully persisted cursor per channel.

### 2.8 Output artefacts

Written to `output/{guild_id}/{YYYY-MM-DD}/`:

- **`messages.jsonl.zst`** — zstandard-compressed JSONL, one message per line. Message schema in §7.
- **`pinned.jsonl`** — plain JSONL (smaller volume). Same schema as messages.
- **`threads.jsonl`** — plain JSONL. Same schema, with `parent_channel_id` populated.
- **`attachments/{msg_id}_{filename}`** — downloaded image files.
- **`meta.json`** — scan metadata + counters (see §7).
- **`prior.txt`** — date of the previous scan of this guild (if any), for diff-friendly Stage 3 consumption.

## 3. Non-functional scope

- **Python 3.12+** on Windows / macOS / Linux. Primary dev env is Windows 11.
- **Forbidden libraries (merge-blocker via CI grep):** `requests`, `aiohttp`, `discord.py`, `discord.py-self`, `pycord`, `selenium`, `playwright`, `obsidian-*`, `anthropic` (this repo is pre-LLM).
- **TLS verify=True always.** No `verify=False` anywhere.
- **URL allowlist:** only `discord.com`, `cdn.discordapp.com`, `gateway.discord.gg`, `media.discordapp.net` (for CDN redirects). Any other host exits 1 with SSRF error.
- **Token handling:** burner token loaded ONLY via `keyring` (cross-platform) OR `DISCORD_TOKEN` env var OR `config.yaml.discord_token` field (last resort, warned). Never logged at any level. Never written to cache. Never written to output.
- **Token redaction:** even synthetic tokens like "test_token" in logs get their middle chunk redacted via `redact_token(t) -> "{t[:6]}***{t[-4:]}"`.
- **Invite-code redaction in logs:** full invite codes NEVER printed in logs. Redact to `{code[:2]}***{code[-2:]}`. Enforced by CI grep.
- **Structured logs:** every HTTP request logs `{stage, source, url_hash, cache_hit (N/A for scan), http_status, duration_ms, attempt}`. Every gateway event logs `{stage, event_type, timestamp}`. Per-scan summary log emitted at end of each guild with full counters.
- **Sort stability:** message ordering within `messages.jsonl.zst` is deterministic: `(channel_id ASC, timestamp ASC, message_id ASC)`. Pinned / threads similarly sorted.
- **Idempotence:** re-scanning with unchanged cursor state produces byte-identical output (modulo zstd compression which has non-deterministic dictionary state — comparison is on the decompressed JSONL).
- **Resumability:** network drop mid-channel leaves cursor unchanged for that channel; re-run continues from last-seen boundary. No duplicate messages in output.
- **Schema tolerance:** pydantic models use `extra='allow'`. Validation errors on a single message log WARNING + skip the message; the run continues.
- **Retention:** optional `retention.raw_dump_keep_days` (default 30) auto-deletes old `output/{guild_id}/` subtrees on scan start.

## 4. Anti-detection discipline (the critical non-negotiable)

Derived from the deep audit of `dsc-smartscraper`. Five MUST-fix items:

### 4.1 Full Discord REST header set

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

### 4.2 HTTP/2

`httpx.AsyncClient(http2=True)`. Discord's real clients negotiate HTTP/2 via ALPN. HTTP/1.1 requests (the old scraper's `urllib`) are distinguishable at the TLS layer.

### 4.3 Gateway WebSocket

See §2.5. A burner account with 100% REST activity and 0% gateway activity is a Discord anti-abuse anomaly.

### 4.4 Cookie persistence

`httpx.AsyncClient` initialized with a `cookies=<persistent jar>` loaded from `state/cookies.json` (per-burner-account file). Discord issues `__dcfduid`, `__sdcfduid`, `locale` cookies; real sessions persist them across requests.

### 4.5 Randomized behaviour

- Per-request jitter: `random.uniform(1.5, 4.0)` s between REST calls.
- Burst pause: `random.uniform(30, 90)` s between channels.
- Daemon mode jitter: `interval_hours + random.uniform(-1, 2)` h variance.
- Scan start window: `config.daemon.scan_start_window` (default "02:00-06:00 UTC") — picks a random start time within the window.

## 5. Config surface (complete minimum)

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
  user_agent_chrome_version: "134.0.0.0"
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

## 6. CLI surface

Entry point: `discord-scanner`.

Commands:
- `discord-scanner resolve` — takes invite codes (from config or `--invite`), calls `/api/v10/invites/{code}`, prints guild_id + guild_name per invite, caches resolutions.
- `discord-scanner list-guilds` — print all guilds the burner is joined to.
- `discord-scanner scan` — full scan of all configured guilds.
- `discord-scanner scan --guild <guild_id>` — scan one guild only.
- `discord-scanner daemon` — loop: scan → sleep `interval_hours ± jitter` → scan.
- `discord-scanner status` — print cursor state per channel.
- `discord-scanner store-token` — prompt for burner token via getpass, store in keyring.
- `discord-scanner version`.

Global flags: `--config PATH`, `--verbose/-v`, `--dry-run`, `--offline` (state-inspection only).

Exit codes: 0 success, 1 user/config error, 2 runtime (network/captcha), 3 detected-ban (token invalid + no recent change — suggests ban).

## 7. Data contracts (JSON schema)

All pydantic v2 models with `extra='allow'`. The output files use these schemas.

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

## 8. Directory layout

```
discord-scanner/
├── pyproject.toml
├── CLAUDE.md
├── .env.example
├── .gitignore
├── config.example.yaml
├── config.smoke.yaml
├── brainstorm.md                     # pre-seeded (this project)
├── seed-spec.md                      # pre-seeded (this project)
├── market-analysis.md                # stub
├── spec.md                           # idea-refiner output
├── security-model.md                 # security-analyst output
├── requirements.md                   # requirements-engineer output
├── validation-report.md              # output-validator output
├── workplan.md                       # workplan-builder output
├── claude-rules.md                   # workplan-builder output
├── docs/claude/
│   ├── architecture.md
│   ├── design-decisions.md
│   ├── development.md
│   └── env-vars.md
├── src/
│   └── discord_scanner/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── logging_conf.py
│       ├── session/
│       │   ├── __init__.py
│       │   ├── auth.py
│       │   ├── rest.py
│       │   └── gateway.py
│       ├── discovery/
│       │   ├── __init__.py
│       │   ├── invite_resolve.py
│       │   ├── channels.py
│       │   └── forums.py
│       ├── fetch/
│       │   ├── __init__.py
│       │   ├── messages.py
│       │   ├── pinned.py
│       │   ├── threads.py
│       │   └── attachments.py
│       ├── cursor/
│       │   ├── __init__.py
│       │   └── state.py
│       ├── dump/
│       │   ├── __init__.py
│       │   ├── jsonl_writer.py
│       │   └── schema.py
│       └── models.py
├── tests/
│   ├── __init__.py
│   ├── fixtures/
│   └── test_*.py
├── state/                            # gitignored (cursor.sqlite, cookies.json)
├── output/                           # gitignored
├── .claude/
│   ├── agents/
│   └── settings.local.json
└── .github/
    └── workflows/
        └── ci.yml
```

## 9. Quality gates (same as civit-hf-scanner)

- `ruff check src tests` — 0 errors
- `ruff format --check src tests` — clean
- `mypy --strict src` — 0 errors
- `pytest -q --cov=src/discord_scanner --cov-fail-under=70` — all pass
- CI grep guards:
  - No `import requests`, `import aiohttp`, `import discord`, `import anthropic`, `from selenium`, `from playwright`
  - No `verify=False`
  - No `time.sleep` inside `async def` (must be `asyncio.sleep`)
  - No raw `discord.gg/` or full Discord invite code in `logger.*` calls without `redact_invite_code()`
  - No raw Discord token pattern (55+ char `[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`) anywhere in `src/`

## 10. Acceptance criteria

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
14. Schema drift tolerance: mocked response with an unexpected extra field on message object → pydantic accepts (extra='allow'), message makes it to dump.
15. CDN image download: mocked CDN fetch writes file to correct path with correct filename; non-image attachments record URL only.
