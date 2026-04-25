# System Architecture: discord-scanner (Stage 2)

**Version**: 1.0
**Date**: 2026-04-23
**Author**: architect agent
**Status**: Draft
**Derived from**: spec.md v1.0, security-model.md v1.0, requirements.md v1.0, seed-spec.md (frozen, authoritative for §4/§5/§6/§7/§10), brainstorm.md (frozen), market-analysis.md (stub)

**Scope note**: `discord-scanner` is a single-user, workstation-only Python 3.12+ CLI. Sections that assume a web/SaaS shape (inbound auth flow, multi-tenant RLS, tenant isolation, WAF, CDN for us, k8s cluster) are marked **N/A** with a one-sentence rationale, following the same adaptation used in the sister project `civit-hf-scanner`. §4 is re-cast as **CLI Surface Design + Outbound API Contracts (Discord REST + Gateway WS + CDN)**.

---

## 1. Architecture Overview

### 1.1 System context (C4 Level 1)

```
                          OPERATOR WORKSTATION
                      (Windows 11 / macOS / Linux)
    ┌─────────────────────────────────────────────────────────────┐
    │                                                             │
    │  [Operator]                                                 │
    │     │                                                       │
    │     │ CLI (typer)                                           │
    │     v                                                       │
    │  [discord-scanner] ────reads────> civit-hf-scanner/         │
    │     │   │   │                        output/latest/         │
    │     │   │   │                        invites.enriched.json  │
    │     │   │   │                                               │
    │     │   │   └──reads───> ./config.yaml                      │
    │     │   │                                                   │
    │     │   └──reads/writes──> OS keyring (Win DPAPI /          │
    │     │                       macOS Keychain /                │
    │     │                       libsecret on Linux)             │
    │     │                                                       │
    │     │   writes                                              │
    │     v                                                       │
    │  output/{guild_id}/{YYYY-MM-DD}/                            │
    │    messages.jsonl.zst, pinned.jsonl, threads.jsonl,         │
    │    attachments/*, meta.json, prior.txt                      │
    │  state/ cursor.sqlite, cookies-<burner>.json,               │
    │    invite_cache.sqlite, cursor.lock, gateway-<burner>.lock  │
    │                                                             │
    └────────────────┬───────────────────────────────┬────────────┘
                     │                               │
                     │ HTTPS / TLS 1.2+ verify=True  │ WSS / TLS
                     │ URL allowlist enforced        │
                     v                               v
        ┌────────────────────────┐      ┌───────────────────────┐
        │ discord.com/api/v10/*  │      │ gateway.discord.gg    │
        │  (REST, HTTP/2)        │      │  (dormant session)    │
        └────────────────────────┘      └───────────────────────┘
                     │
                     │ CDN redirect (same allowlist)
                     v
        ┌────────────────────────────────────────┐
        │ cdn.discordapp.com / media.discordapp.net │
        │  (attachment GETs, no Auth header)     │
        └────────────────────────────────────────┘
```

**Adversary**: Discord's anti-abuse platform (primary). Local-host attacker and supply-chain are secondary. There is NO inbound user; this process is 100% outbound.

### 1.2 Design decisions summary

| # | Decision | Choice | Alternative considered | Rationale | Reference |
|---|----------|--------|------------------------|-----------|-----------|
| 1 | Runtime | Python 3.12+ | 3.11, 3.13 | Aligns with sister project `civit-hf-scanner`; taskgroups + improved asyncio; 3.12 is lowest stable with mypy-strict support for the stack | REQ-NF-001, brainstorm §6 |
| 2 | HTTP client | `httpx[http2]>=0.27` | `requests`, `aiohttp`, `urllib` | HTTP/2 required for TLS-layer parity with real Chrome (seed-spec §4.2); `requests` + `aiohttp` are merge-blockers (CI grep) | REQ-NF-022, SEC-P0-11 |
| 3 | Discord auth | Burner user token via `keyring` | `discord.py` bot, OAuth app | Operator has no admin on target guilds; user-token is the accepted (ToS-violating) path; `discord.py`/`discord.py-self` are forbidden imports | spec §8.1, ADR-002 |
| 4 | Gateway | Hand-rolled `websockets>=13` dormant session | `discord.py-self` | Surgical control over IDENTIFY/HEARTBEAT fingerprint; no silent-REST anomaly; forbidden-lib list rules out every discord-* package | ADR-003, SEC-P0-10 |
| 5 | Cursor store | sqlite3 stdlib with `?`-parameterised queries | JSON-per-channel file (old scraper) | Atomic (guild_id,channel_id) upserts; single file lock; replaces fragile per-file JSON locking | ADR-005, REQ-NF-042 |
| 6 | File locking | `filelock` | `fcntl`, `msvcrt`, hand-rolled | Cross-platform; `fcntl` is POSIX-only and breaks on Windows (the primary dev target) | ADR-006, REQ-NF-054 |
| 7 | Compression | `zstandard>=0.22` | gzip, xz, plain JSONL | ~5x smaller than gzip at similar CPU; chosen as Stage 2→3 on-disk contract | ADR-007, seed §2.8 |
| 8 | Secret store | `keyring>=24` (platform-native) | env-only, plain JSON | Uses Windows DPAPI / macOS Keychain / libsecret; refuses plaintext fallback; cross-platform vs. old scraper's mac-only Keychain | ADR-008, SEC-P0-06 |
| 9 | Config + validation | `pydantic>=2.6` + `pydantic-settings>=2.2` | argparse + dict, dataclasses | Schema tolerance (`extra='allow'`) for Discord drift; strict validation on operator config; env_prefix support | REQ-NF-035, REQ-F-001 |
| 10 | Process model | Single asyncio process, one event loop | Thread pool, multiprocess | Single burner ⇒ single gateway ⇒ single writer; filelock enforces singleton. Concurrency via `asyncio.Semaphore` + token buckets | §2.2 "Main" service, REQ-F-F050 |
| 11 | Deployment | Local install via `pip install -e ".[dev]"` | Docker, pipx, cloud | Workstation-only; no CD; CI matrix `py3.12 × {ubuntu, windows}` | §8.1, REQ-NF-005 |

---

## 2. Component Architecture

### 2.1 Component diagram (C4 Level 2)

```
                       ┌─────────────────────────────────────────────┐
                       │              cli.py (typer)                 │
                       │  resolve | list-guilds | scan [--guild] |   │
                       │  daemon | status | store-token | version    │
                       │  Global flags: --config --verbose --dry-run │
                       │                --offline                    │
                       └──────────────────┬──────────────────────────┘
                                          │ imports, calls async main
                                          v
        ┌──────────────────────────────────────────────────────────┐
        │ config.py  (pydantic-settings + YAML load + path guard)  │
        │   Settings env_prefix="DISCORD_SCANNER_"                 │
        │   Validates: URL allowlist ⊆ {discord.com, cdn.*,        │
        │   gateway.*, media.*}; output_root realpath hygiene;     │
        │   Chrome UA staleness (≤ 8 weeks); client_build_number   │
        │   plausibility; `date_override` ISO-8601                 │
        └───┬──────────────────────────────────────────────────────┘
            │
            v
  ┌──────────────────────────────────────────────────────────────────┐
  │                  session/  (the one shared object graph)         │
  │                                                                  │
  │ ┌─ auth.py ────────────────────────────────────────────────────┐ │
  │ │  load_token() → keyring > env > config (priority, logged);   │ │
  │ │  refuses keyrings.alt plaintext backend (SEC-P0-06)          │ │
  │ └──────────────────────────────────────────────────────────────┘ │
  │ ┌─ rest.py ────────────────────────────────────────────────────┐ │
  │ │  httpx.AsyncClient(http2=True, verify=True,                  │ │
  │ │    cookies=<persistent jar>, event_hooks={request:[          │ │
  │ │    url_allowlist, attach_full_header_set, token_bucket],     │ │
  │ │    response:[redact, observe, detect_captcha]})              │ │
  │ │  Exposes: get(), get_paginated(), stream_download()          │ │
  │ │  Token bucket per-host: discord.com/api:2rps, cdn:1rps       │ │
  │ │  Global asyncio.Semaphore(8) for fairness                    │ │
  │ │  tenacity retry: 429 (Retry-After), 5xx; honour              │ │
  │ │    MAX_429_RETRIES=3 then skip channel                       │ │
  │ └──────────────────────────────────────────────────────────────┘ │
  │ ┌─ gateway.py ─────────────────────────────────────────────────┐ │
  │ │  websockets.connect(wss://gateway.discord.gg/?v=10&…)        │ │
  │ │  OPCODE 2 IDENTIFY (properties ≡ REST X-Super-Properties)    │ │
  │ │  OPCODE 10 HELLO → schedule OPCODE 1 HEARTBEAT               │ │
  │ │  OPCODE 3 PRESENCE UPDATE once on READY, then silent         │ │
  │ │  OPCODE 6 RESUME on drop (session_id+seq); fresh IDENTIFY    │ │
  │ │    on RESUME failure; single-writer via gateway-<b>.lock     │ │
  │ └──────────────────────────────────────────────────────────────┘ │
  └──────────────────────────────────────────────────────────────────┘
            │                                  │
            │ used by                          │ used by
            v                                  v
  ┌───────────────────────────────┐   ┌───────────────────────────────┐
  │ discovery/                    │   │ fetch/                        │
  │  invite_resolve.py            │   │  messages.py  (paginated)     │
  │    GET /invites/{code}        │   │  pinned.py                    │
  │    cache: invite_cache.sqlite │   │  threads.py  (forum+archived) │
  │    TTL 7d                     │   │  attachments.py (CDN, MIME    │
  │  channels.py                  │   │    sniff, size cap, sanitise  │
  │    GET /users/@me/guilds      │   │    filename, realpath check)  │
  │    GET /guilds/{id}/channels  │   └────────────────┬──────────────┘
  │    GET /guilds/{id}/roles     │                    │
  │  forums.py (thread lists)     │                    │
  └────────────┬──────────────────┘                    │
               │                                       │
               └───────────┬───────────────────────────┘
                           │ yields Message / Attachment /
                           │        Reaction pydantic v2 models
                           │        (extra='allow')
                           v
                 ┌──────────────────────────────────────────────┐
                 │ dump/                                        │
                 │  schema.py  (pydantic models + ser helpers)  │
                 │  jsonl_writer.py                             │
                 │    - sorts (channel_id ASC, ts ASC, id ASC)  │
                 │    - alphabetised dict keys                  │
                 │    - rejects NaN / Infinity (raises)         │
                 │    - zstd stream for messages.jsonl.zst      │
                 │    - plain JSONL for pinned / threads        │
                 │    - meta.json writer                        │
                 │    - prior.txt writer                        │
                 │    - chmod 0o600 on every emit               │
                 └──────────────────────────────────────────────┘
                           ^
                           │ persists cursor after
                           │ successful channel dump
                           │
                 ┌──────────────────────────────────────────────┐
                 │ cursor/state.py                              │
                 │  sqlite3 (state/cursor.sqlite)               │
                 │  table: channel_cursor                       │
                 │  PRIMARY KEY (guild_id, channel_id)          │
                 │  atomic upsert after channel dump succeeds   │
                 │  filelock on state/cursor.lock, timeout=0    │
                 └──────────────────────────────────────────────┘

                 ┌──────────────────────────────────────────────┐
                 │ logging_conf.py (structlog processors)       │
                 │   redact_token(t) "{t[:6]}***{t[-4:]}"       │
                 │   redact_invite_code(c) "{c[:2]}***{c[-2:]}" │
                 │   JSON renderer; per-scan run_id context;    │
                 │   every record validated against pydantic    │
                 │   log-event model (SEC-P1-03 forward-compat) │
                 └──────────────────────────────────────────────┘

                 ┌──────────────────────────────────────────────┐
                 │ retention.py                                 │
                 │   prune on scan start; realpath within       │
                 │   output_root; followlinks=False             │
                 └──────────────────────────────────────────────┘
```

**Data-flow annotations**:
- `cli → config`: synchronous; filesystem read; sensitivity = internal (no token).
- `session.rest → discord.com`: HTTPS, TLS ≥1.2, verify=True, HTTP/2; full header set (REQ-NF-006..021); token in `Authorization` header, redacted in logs.
- `session.gateway → gateway.discord.gg`: WSS, TLS; token in IDENTIFY payload (post-TLS); redacted in logs.
- `session.rest → cdn.discordapp.com`: HTTPS; same UA/Sec-* headers as REST; **Authorization header stripped** (REQ-F-015); pre-signed URL only.
- `fetch → dump`: in-process object references; no IPC.
- `dump → disk`: atomic write-then-rename for `meta.json` and `prior.txt`; append-streaming for `.jsonl.zst`; all files chmod 0o600 immediately after creation (REQ-NF-037).
- `cursor/state ↔ disk`: single-writer via filelock; sqlite WAL; atomic commit after channel dump.

**Auth mechanism on every arrow**: either OS-user filesystem permissions (local), or Discord burner token (outbound REST + Gateway IDENTIFY), or nothing (CDN, pre-signed URL).

### 2.2 Service responsibilities

Only ONE service exists (the CLI process). Sub-components documented as module units:

```
Module: cli.py
Responsibility: Parse CLI args/flags, resolve config, dispatch to subcommand handlers.
  Does NOT: do any network I/O itself; touch output dirs directly.
Technology: typer>=0.12, rich>=13 (only in cli.py — forbidden elsewhere per REQ-NF-052).
Scaling strategy: N/A (single process, single user).
Dependencies: config.py, session/, discovery/, fetch/, dump/, cursor/.
Health check: N/A (no port). `discord-scanner version` + future `doctor` (SEC-P2-02).
Owned data: none (coordinator).
External APIs called: none (indirect via session/*).
Security perimeter: OS-user (whoever can exec the binary + read keyring).

Module: session/
Responsibility: Single source of authenticated outbound I/O (REST + Gateway) + token
  loading + cookie jar. Owns the one shared httpx.AsyncClient and the one WS.
  Does NOT: decide what to fetch (discovery's job), what to write (dump's job),
  persist cursor (cursor's job).
Technology: httpx[http2], websockets, tenacity, keyring.
Scaling strategy: Stateful singleton; filelock guards singletons:
  - state/cursor.lock (any CLI writer)
  - state/gateway-<burner>.lock (gateway WS)
Dependencies: keyring backend, DNS to discord.com + gateway.discord.gg.
Health check: at startup — DNS resolves, TLS negotiates to discord.com, keyring
  backend is not `keyrings.alt`, URL allowlist compiled.
Owned data: state/cookies-<burner>.json; in-memory token (never on disk outside keyring).
External APIs called: Discord REST (full allowlist), Discord Gateway, Discord CDN.
Security perimeter: process-local memory; filelock-enforced singleton.

Module: discovery/
Responsibility: Turn config.discovery + invites.enriched.json into a list of
  (guild_id, channel_id, channel_type, channel_name) tuples to scan.
  Does NOT: fetch message content.
Technology: pydantic v2 models with extra='allow'; sqlite3 cache (7-day TTL).
Dependencies: session/rest.
Owned data: state/invite_cache.sqlite (invite_code → guild_id, name, resolved_at).

Module: fetch/
Responsibility: Given a (guild, channel) target, paginate messages / pins / threads;
  download image attachments; sanitise filenames; MIME-sniff; stream size cap.
  Does NOT: persist cursor itself; sort the final output (that's dump's job on emit).
Dependencies: session/rest, cursor/state (read cursor), dump/.
Owned data: none directly; feeds dump.
External APIs called: Discord REST (message endpoints), Discord CDN (attachments).

Module: cursor/state.py
Responsibility: Persist (guild_id, channel_id) → last_message_id cursors.
Technology: sqlite3 stdlib; WAL; `?` placeholders only.
Dependencies: filelock.
Owned data: state/cursor.sqlite.

Module: dump/
Responsibility: Serialize messages to jsonl.zst + plain JSONL; write meta.json;
  enforce sort stability, alphabetised keys, no NaN/Infinity; chmod 0o600.
  Does NOT: fetch, retry, or schedule.
Technology: zstandard, pydantic serialisers.
Dependencies: schema models from dump/schema.py.
Owned data: output/{guild_id}/{YYYY-MM-DD}/ subtree (writes); prior scan dir (reads).

Module: retention.py
Responsibility: Prune output/{guild_id}/ older than raw_dump_keep_days; prune
  attachments/ older than attachment_keep_days. Reject symlink escapes via realpath.
Runs: at scan start, before any fetch.

Module: logging_conf.py
Responsibility: Configure structlog with redact_token + redact_invite_code
  processors; JSON renderer; run_id context; bind scan-level fields.

Module: config.py
Responsibility: Pydantic-settings Settings class; load YAML + env (prefix
  `DISCORD_SCANNER_`); path normalisation; URL allowlist literal; UA staleness check.
```

**Component count in §2: 10** (cli, config, session (composite: auth+rest+gateway), discovery, fetch, cursor, dump, retention, logging_conf, models).

---

## 3. Data Model

### 3.1 Entity relationship overview

The "database" here is (a) `state/cursor.sqlite` (1 table), (b) `state/invite_cache.sqlite` (1 table), (c) on-disk JSONL (records, not rows). No relational joins. All entities are pydantic v2 with `model_config = ConfigDict(extra='allow')`.

```
[Config] ──1:1──> [Settings pydantic]
[InviteRow] ──N:1──> [GuildResolution]       (invite_cache.sqlite)
[CursorRow] ──1:1──> (guild_id, channel_id)  (cursor.sqlite)
[Message] ──N:M──> [Attachment]              (dump records)
[Message] ──N:M──> [Reaction]
[Message] ──N:1──> [Channel]                 (channel_id denormalised on message)
[Channel] ──N:1──> [Guild]
[Guild] ──1:N──> [Channel]
[Guild] ──1:1──> [Meta]  (per scan date)
```

No foreign keys are enforced at the sqlite layer — the application maintains integrity.

### 3.2 Core entities

```
Entity: CursorRow
Table: channel_cursor (state/cursor.sqlite)
Description: One row per (guild, channel) tracking pagination.

Fields:
  guild_id            TEXT NOT NULL             — Discord snowflake as string
  channel_id          TEXT NOT NULL             — Discord snowflake as string
  last_message_id     TEXT NOT NULL             — last successfully-dumped msg id, "0" on first scan
  last_scan_at        TEXT NOT NULL             — ISO-8601 UTC timestamp
  message_count_total INTEGER NOT NULL DEFAULT 0 — lifetime count for observability

Indexes:
  PRIMARY KEY (guild_id, channel_id)
  INDEX ON (last_scan_at)                       — for status command + stale detection

Row-level security: N/A (single-user).
Encryption: filesystem-level (chmod 0o600); sqlcipher explicitly rejected (§8.3 security-model).
Data ownership: cursor/state.py has sole write authority.
```

```
Entity: InviteCacheRow
Table: invite_resolution (state/invite_cache.sqlite)

Fields:
  invite_code   TEXT PRIMARY KEY                — ^[A-Za-z0-9-]{4,20}$
  guild_id      TEXT NOT NULL
  guild_name    TEXT NOT NULL
  resolved_at   TEXT NOT NULL                   — ISO-8601 UTC
  expires_at    TEXT NULL                       — Discord `with_expiration=true` data, if any

Indexes:
  PRIMARY KEY (invite_code)
  INDEX ON (resolved_at)                        — for 7-day TTL sweep

Data ownership: discovery/invite_resolve.py has sole write authority.
```

```
Entity: Message  (pydantic v2; one line in messages.jsonl.zst / pinned.jsonl / threads.jsonl)

Fields (per seed-spec §7.1 / spec §7.1, all optional if extra='allow' forbids strictness):
  schema_version        int            = 1
  guild_id              str            — Discord snowflake
  channel_id            str
  channel_name          str
  channel_type          Literal["text","announcement","forum","thread"]
  parent_channel_id     str | None     — set iff channel_type == "thread"
  message_id            str
  author_id             str
  author_name           str
  author_discriminator  str
  content               str
  timestamp             datetime       — UTC, tz-aware
  edited_timestamp      datetime | None
  reactions             list[Reaction]
  attachments           list[Attachment]
  mentions              Mentions       — {users: list[str], roles: list[str]}
  reply_to_message_id   str | None
  thread_id             str | None
  pinned                bool
  flags                 int

Serialisation: alphabetised dict keys; tz-aware ISO-8601 for datetimes.
Validation tolerance: extra='allow' (REQ-NF-035).
Forbidden in output: raw burner token; un-redacted URLs inside log *references*
  (the attachment cdn_url IS kept in the JSONL — it's the Stage 3 input).
```

```
Entity: Attachment

Fields:
  id             str
  filename       str                  — SANITISED (REQ-NF-026) before storage
  content_type   str
  size           int
  cdn_url        str                  — pre-signed, ~24h TTL
  local_path     str | None           — relative path under output/{guild_id}/{date}/
  download_failed bool = False        — true iff REQ-F-013 path failed mid-stream

MIME-sniff enforced before local write (SEC-P0-19).
Path realpath asserted within output_root before open() (SEC-P0-21).
```

```
Entity: Reaction

Fields:
  emoji    str       — unicode char OR ":name:" for custom emoji
  count    int
  id       str | None        — custom emoji id
  animated bool = False       — custom emoji only
```

```
Entity: Meta  (one file per guild per scan date, output/{guild_id}/{date}/meta.json)

Fields (per seed-spec §7.2):
  schema_version          int = 1
  guild_id                str
  guild_name              str
  scan_started_at         datetime (UTC)
  scan_completed_at       datetime (UTC) | None (None if partial)
  channels_scanned        list[str]
  channels_skipped        list[{id, reason, thread_id?}]
  message_count           int
  pinned_count            int
  thread_count            int
  attachment_count        int
  attachments_downloaded  int
  attachments_skipped     list[{reason, count}]
  rate_limit_hits         int
  gateway_disconnects     int
  gateway_resumes         int
  errors                  list[{stage, source, error_class, message_redacted}]
  cursor_file             str = "cursor.sqlite"
  prior_scan_date         str | None     — YYYY-MM-DD from prior.txt

Sort: keys alphabetised on serialise (REQ-NF-032).
```

**Entity count in §3: 6** (CursorRow, InviteCacheRow, Message, Attachment, Reaction, Meta). An additional pydantic Settings object governs config but is not a persisted entity.

### 3.3 Migration strategy

**Migration tool**: none. sqlite schema is versioned inline in `cursor/state.py` via a `schema_version` PRAGMA + idempotent `CREATE TABLE IF NOT EXISTS`. Rationale: single-user, single-machine; Alembic is overkill. JSONL output carries `schema_version: 1` per line — Stage 3 handles any future bump.

**Policy**:
- Cursor schema: forward-only; breaking changes require a new table name + migration read from the old.
- JSONL contract bump (breaking): increments `schema_version` field on every record; Stage 3 branches on it.
- Invite cache: disposable; drop + recreate on breaking change is acceptable (7-day TTL means cost ≤ 7 days of re-resolutions).

### 3.4 Data ownership and access control

```
Module           | Tables/files it owns (write)          | Reads only
---------------- | -------------------------------------  | ---------------------
cursor/state.py  | state/cursor.sqlite                    | —
discovery/*      | state/invite_cache.sqlite              | invites.enriched.json
dump/*           | output/{guild_id}/{YYYY-MM-DD}/**      | prior date's prior.txt
retention.py     | deletes under output/ older than TTL   | —
session/rest.py  | state/cookies-<burner>.json            | keyring entry
session/auth.py  | keyring entry (via store-token cmd)    | keyring, env, config
logging_conf.py  | stdout; optional log file (chmod 600)  | —
```

**Multi-tenant isolation**: N/A — single-user. Cross-guild isolation on disk is by directory namespacing on `guild_id`. Retention operates per-guild subtree.

**RLS / Postgres policies**: N/A — no Postgres.

---

## 4. CLI Surface Design + Outbound API Contracts

(This section replaces the template's API-Surface-for-web-SaaS. The tool has no inbound API.)

### 4.1 CLI conventions

```
Entry point: discord-scanner = "discord_scanner.cli:app"
Framework:   typer>=0.12 + rich>=13 (user-facing output only, confined to cli.py)
Global flags:
  --config PATH       default: ./config.yaml
  --verbose/-v        increases log level to DEBUG
  --dry-run           prints planned actions, makes zero HTTP/WS calls
  --offline           inspects state/ only, zero HTTP/WS calls, zero writes

Exit codes:
  0    success
  1    user/config error (missing file, SSRF violation, UA too stale, plaintext keyring)
  2    runtime error (network failure, captcha detected, gateway RESUME failed)
  3    detected-ban heuristic (401 on previously-working endpoint + no config change)

Commands (8 total):
  resolve        — invite codes → guild_id/name via /api/v10/invites/{code}; caches
  list-guilds    — /api/v10/users/@me/guilds for the burner
  scan           — full scan of all configured guilds (supports --guild <id>)
  daemon         — loop: scan → sleep interval_hours±jitter → scan
  status         — prints cursor state per channel (local only, no network)
  store-token    — getpass prompt → keyring (platform-native, plaintext-fallback refused)
  version        — prints package version
  (plus help/root)

Error output: structlog JSON to stdout/stderr; rich table in cli.py for --dry-run summary.
```

### 4.2 Outbound Discord REST contract

```
Base: https://discord.com/api/v10/
Auth: Authorization: <burner-token>   (NO "Bearer " prefix — user token convention)
Headers (on EVERY request, REQ-NF-006..021):
  User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36
              (KHTML, like Gecko) Chrome/<VER>.0.0.0 Safari/537.36
  Sec-Ch-Ua: "Chromium";v="<VER>", "Google Chrome";v="<VER>", "Not?A_Brand";v="99"
  Sec-Ch-Ua-Mobile: ?0
  Sec-Ch-Ua-Platform: "Windows"
  Sec-Fetch-Site: same-origin
  Sec-Fetch-Mode: cors
  Sec-Fetch-Dest: empty
  X-Super-Properties: base64(json({os, browser, browser_version, os_version, device,
                      browser_user_agent, system_locale, client_build_number,
                      release_channel="stable"}))
  X-Discord-Locale: en-US
  X-Discord-Timezone: Europe/Prague
  X-Debug-Options: logGatewayEvents
  Origin: https://discord.com
  Referer: https://discord.com/channels/@me
  Accept: */*
  Accept-Encoding: gzip, deflate, br
  Accept-Language: en-US,en;q=0.9
  Content-Type: application/json         (only on body-bearing requests; none here)

Endpoints called (read-only, GET only):
  GET /invites/{code}?with_counts=true&with_expiration=true
  GET /users/@me/guilds
  GET /guilds/{guild_id}/channels
  GET /guilds/{guild_id}/roles
  GET /channels/{channel_id}/messages?limit=100&after={cursor}
  GET /channels/{channel_id}/pins
  GET /channels/{channel_id}/threads/public_archived_threads?limit=50[&before=...]
  GET /guilds/{guild_id}/threads/active

Endpoints explicitly NOT called (enforced by absence in session/ + design review):
  /guilds/{id}/members                 — loud, member-list enumeration
  ANY POST / PATCH / DELETE / PUT      — write operations banned
  /users/@me/dms  etc.                 — no DM surface

Rate shape:
  Per-host token bucket: discord.com/api = 2 req/s (burst 4).
  Per-request jitter: uniform(1.5, 4.0) s.
  Burst pause: uniform(30, 90) s between channels + between guilds.
  Global asyncio.Semaphore(8) — upper bound across all hosts.

Retry policy (tenacity):
  Retry on: 429 (Retry-After honoured), 500, 502, 503, 504, httpx timeouts.
  Backoff: exponential, 2s base, 60s cap, attempts=5.
  MAX_429_RETRIES=3 on same endpoint → skip channel; record in meta.errors[].

Error classes:
  401 on previously-working endpoint → exit 3 (detected-ban heuristic).
  403 + body has captcha_key|captcha_sitekey|captcha_service → exit 2 (hard abort).
  Plain 403 → skip channel, continue; append to meta.channels_skipped[].
  404 on invite → skip invite, WARNING.
  Redirect out of URL allowlist → SSRFViolation, exit 1.
```

### 4.3 Outbound Discord Gateway contract (dormant session)

```
URL: wss://gateway.discord.gg/?v=10&encoding=json
Scheme: wss:// only (SEC-P0-10). ws:// is a startup error.
Lifetime: opened before first REST call, maintained for entire scan, closed at end.

State machine:
  1. CONNECT wss → TLS handshake
  2. RECV    OPCODE 10 HELLO {heartbeat_interval}
             clamp interval to [1000, 120000] ms
  3. SEND    OPCODE 2 IDENTIFY
             {
               token: <burner>,
               properties: {os, browser, browser_version, os_version, device,
                           system_locale, browser_user_agent,
                           client_build_number, release_channel},
                           // MUST match REST X-Super-Properties byte-for-byte
               compress: false,
               intents: 0                  // nothing requested
             }
  4. RECV    OPCODE 0 READY {session_id, resume_gateway_url, ...}
             store session_id + sequence
  5. SEND    OPCODE 3 PRESENCE UPDATE {status:"online", activities:[], afk:false}
             ONCE. Never re-sent during a scan.
  6. LOOP    every `heartbeat_interval * uniform(0.8, 1.0)`:
             SEND OPCODE 1 HEARTBEAT {sequence}
  7. ON disconnect:
             if resume_on_drop and session_id known:
               reconnect to resume_gateway_url
               SEND OPCODE 6 RESUME {token, session_id, sequence}
             if RESUME fails (opcode 9 Invalid Session): fresh IDENTIFY (step 3)
             log gateway_disconnects++, gateway_resumes++ in meta.json

Singleton enforcement:
  filelock on state/gateway-<burner>.lock, timeout=0.
  Two scans with same burner: second exits 1 with clear error.

Events NOT processed:
  Any OPCODE 0 DISPATCH event other than READY is dropped silently (decoded and discarded).
  No voice state, no lazy-guild, no message-create handlers.
  Inbound messages > 1 MB are discarded (DoS guard, SEC-P0 §4.1 input-vector table).
```

### 4.4 Outbound CDN contract

```
Hosts: cdn.discordapp.com, media.discordapp.net  (both in URL allowlist).
Auth:  NONE. Pre-signed URL contains its own `?ex=...&is=...&hm=...` signature.
       The burner `Authorization` header MUST be stripped before CDN GETs (REQ-F-015).
Headers: same User-Agent + Sec-Ch-Ua* + Sec-Fetch-* as REST (SEC-P0-15).
Rate: per-host bucket = 1 req/s, shared asyncio.Semaphore(8).

Per-download:
  1. Stream GET (httpx.stream).
  2. Read first 16 bytes.
  3. MIME-sniff vs. declared extension:
       PNG  89 50 4E 47
       JPEG FF D8 FF
       WebP 52 49 46 46 ..     ..       57 45 42 50  (offset 0, 8)
       GIF  47 49 46 38
     Mismatch → close stream, delete partial, record URL-only + WARNING (SEC-P0-19).
  4. Continue streaming; abort at max_size_mb * 1.1 bytes → delete partial (SEC-P0-20).
  5. Write to output/{guild_id}/{date}/attachments/{msg_id}_{sanitised_filename}.
  6. os.path.realpath asserted within Path(output_root).resolve() — reject symlink escape.
  7. chmod 0o600.

Redirects: followed only within URL allowlist; any cross-host redirect → SSRFViolation.
```

### 4.5 Auth flow

**Inbound**: N/A — no inbound auth, no sessions, no logins, no MFA, no tokens issued by us. Standard OS user-level permissions gate the tool.

**Outbound (burner credential)**:

```
  [Operator]                        [discord-scanner]                  [discord.com]
       │                                 │                                  │
       │  store-token (getpass)          │                                  │
       │────────────────────────────────>│                                  │
       │                                 │ keyring.set_password(            │
       │                                 │   service=auth.keyring_service,  │
       │                                 │   username=auth.keyring_username,│
       │                                 │   password=<token>)              │
       │                                 │                                  │
   ... (time passes; operator runs `scan`) ...                              │
       │                                 │                                  │
       │   discord-scanner scan          │                                  │
       │────────────────────────────────>│                                  │
       │                                 │ load_token():                    │
       │                                 │   keyring > env > config         │
       │                                 │   refuse plaintext-backend       │
       │                                 │   log {token_source} at INFO     │
       │                                 │                                  │
       │                                 │ open gateway ────────────────────>│ WSS IDENTIFY (token)
       │                                 │                                  │
       │                                 │ open httpx client                │
       │                                 │ cookies = load(state/cookies-*)  │
       │                                 │                                  │
       │                                 │ GET /invites/{code} ────────────>│ REST (token header)
       │                                 │ ...                              │
       │                                 │                                  │
       │                                 │ redact_token processor on        │
       │                                 │   every log record + every       │
       │                                 │   exception traceback            │
       │                                 │                                  │
```

**Token revocation**: Discord-side only. Operator resets burner password → all tokens invalidate → re-run `store-token`.

### 4.6 WebSocket / real-time design

See §4.3. This is OUTBOUND only. We do not host a WebSocket server.

---

## 5. Security Architecture

(Every SEC-P0-* from security-model.md §6 is traced to an architectural control below.)

### 5.1 Threat-to-control cross-reference

| SEC-P0-# | Threat | Architectural control | Module |
|---------|--------|------------------------|--------|
| 01 | Arbitrary token source | `load_token()` enumerates exactly three sources (keyring > env > config) | session/auth.py |
| 02 | Shoulder-surfing on token entry | `getpass.getpass()` in `store-token`; typer prompt forbidden | cli.py, session/auth.py |
| 03 | Token leak via log record | `redact_token()` structlog processor on every record; registered in logging_conf.py before any handler | logging_conf.py |
| 04 | Raw token pattern in repo | CI grep merge-blocker in `.github/workflows/ci.yml` | CI |
| 05 | Token written to state/output | Integration test greps all artefacts for test-token; `session/auth.py` never serialises token outside keyring | tests + session/auth.py |
| 06 | Keyring plaintext fallback | `load_token()` inspects `keyring.get_keyring()` class name; refuses `keyrings.alt.*` | session/auth.py |
| 07 | Missing anti-detection headers | Single header-build function in `session/rest.py` event_hook attaches all 17 headers; respx test asserts every key present | session/rest.py |
| 08 | Stale Chrome UA | Config-load validator: `user_agent_chrome_version` annotated with `chrome_version_released_at`; fail if > 56 days | config.py |
| 09 | X-Super-Properties blob integrity | `build_super_properties(http_cfg) -> str` single source; gateway IDENTIFY `properties` MUST equal `base64.decode(header)` (unit test) | session/rest.py, session/gateway.py |
| 10 | Gateway fingerprint drift / wss:// | `gateway.py` asserts scheme == "wss"; IDENTIFY properties built via the same function as X-Super-Properties | session/gateway.py |
| 11 | HTTP/1.1 TLS fingerprint | `httpx.AsyncClient(http2=True)` — single construction site; unit test on `client.http2 is True` | session/rest.py |
| 12 | Cross-burner cookie leak | `state/cookies-<keyring_username>.json` — filename includes burner alias | session/rest.py |
| 13 | Concurrent gateway / second scan | `filelock` on `state/gateway-<burner>.lock` + `state/cursor.lock`, timeout=0 | session/gateway.py, cursor/state.py |
| 14 | Rate-limit violation | Per-host token bucket (tokenbucket in session/rest.py); per-request jitter; burst pause | session/rest.py |
| 15 | 429 ignoring Retry-After | tenacity retry wired to read `Retry-After`; MAX_429_RETRIES=3 counter per-endpoint | session/rest.py |
| 16 | Captcha auto-solve temptation | Response hook detects `captcha_key|captcha_sitekey|captcha_service` → raises `CaptchaDetected`; cli.py maps to exit 2. No auto-solver dependency exists | session/rest.py, cli.py |
| 17 | SSRF via config or redirect | `httpx.event_hooks.request` validator: netloc ∈ {discord.com, cdn.discordapp.com, gateway.discord.gg, media.discordapp.net}; response 3xx `Location` re-validated | session/rest.py |
| 18 | Disabled TLS verify | `httpx.AsyncClient(verify=True)` single construction site; CI grep merge-blocker | session/rest.py + CI |
| 19 | Malicious attachment (exec-as-image) | MIME sniff first 16 bytes vs declared extension; mismatch → discard | fetch/attachments.py |
| 20 | Zip-bomb / oversized attachment | Streaming size cap at `max_size_mb * 1.1`; abort + delete partial | fetch/attachments.py |
| 21 | Filename path traversal | `PurePosixPath(name).name`; strip null + forbidden chars; length cap 128; realpath within output_root | fetch/attachments.py |
| 22 | Local-host readability | `os.chmod(0o600)` on every file; `0o700` on every dir; best-effort on Windows (documented) | dump/, cursor/, session/rest.py |
| 23 | Retention escape via symlink | `shutil.rmtree(..., followlinks=False)` replacement: manual walk with `os.path.islink` + realpath containment check | retention.py |
| 24 | Retention following symlinks | Same walker as 23, islink short-circuit | retention.py |
| 25 | Gateway/REST fingerprint drift | Single `http_fingerprint` config block sourced by both; merge-blocker unit test | config.py |
| 26 | Implausible client_build_number | Config validator: int ≥ 300000 | config.py |
| 27 | Third-party PII docs missing | CI grep asserts section heading "Third-party PII handling and operator obligations" in `docs/claude/design-decisions.md` | CI + docs |
| 28 | output/ checked in | `.gitignore` contents asserted in CI | CI + .gitignore |
| 29 | Forbidden imports / raw tokens / verify=False | CI grep merge-blockers (union of §9.6) | CI |
| 30 | Unlocked dependencies | `uv.lock` (or poetry.lock) committed; `pip install --require-hashes` in CI | CI |
| 31 | SAST | `bandit -r src/ --severity-level medium`; ruff `S` rule-set | CI |
| 32 | `.env` hygiene | `.env.example` committed; `.env` gitignored | repo hygiene |

### 5.2 Fingerprint coherence invariant (the anti-detection keystone)

The heart of §4 anti-detection discipline is a **single configuration source** for the impersonation fingerprint:

```
┌──── config.http ────────────────────────────────────────────┐
│  user_agent_chrome_version   ("148.0.7778.56")              │
│  fake_os                     ("Windows NT 10.0; Win64; x64")│
│  fake_os_platform            ("Windows")                    │
│  locale                      ("en-US")                      │
│  timezone                    ("Europe/Prague")              │
│  client_build_number         (>= 300000)                    │
└──────────────┬────────────────────────────┬─────────────────┘
               │                            │
               v                            v
  build_rest_headers(cfg)         build_gateway_properties(cfg)
   User-Agent, Sec-Ch-Ua,          OPCODE 2 IDENTIFY .properties
   X-Super-Properties (base64 of   (os, browser, browser_version,
    the same JSON blob),            os_version, device,
   X-Discord-Locale, -Timezone,    system_locale, browser_user_agent,
   Sec-Fetch-*, etc.                client_build_number, release_channel)
               │                            │
               v                            v
          REST request                 Gateway IDENTIFY
          to discord.com               to gateway.discord.gg

Merge-blocker unit test (SEC-P0-25):
  assert base64.b64decode(headers["X-Super-Properties"]).json() ==
         identify_payload["properties"]
```

A mismatch is a detection signal Discord can train on. Treating both derived artefacts as *derived* from one frozen struct, and testing equality in CI, prevents drift forever.

### 5.3 Secrets management

```
Burner token (the ONLY secret):
  Dev:        operator runs `discord-scanner store-token` interactively.
  Runtime:    keyring.get_password(service, username).
              Fallback order: env DISCORD_TOKEN → config.yaml.discord_token (warned).
  Transit:    Authorization header (REST), IDENTIFY.token (WSS).
  At rest:    OS-native (DPAPI / Keychain / libsecret). NEVER plaintext on disk.
  Logs:       redact_token(t) processor on every record + traceback; CI grep forbids raw pattern.
  Rotation:   "rotate" = reset burner password or switch burner; tool cannot revoke upstream.

CI/CD secrets:
  None required. The CLI needs no secrets to build or test — `store-token` is
  interactive, tests use fixture tokens (which CI grep rejects outside tests/fixtures/*).

Break-glass:
  Lost keyring access → operator re-runs `store-token`.
  Suspect compromise → operator resets burner Discord password → all tokens invalidate.
```

### 5.4 Network security

```
External traffic:
  - HTTPS TLS 1.2+ (TLS 1.3 preferred via httpx/OpenSSL negotiation).
  - WSS TLS for gateway.
  - verify=True always; certifi bundle; no custom CA pinning (we want to look like
    vanilla Chrome using system roots).
  - URL allowlist literal: {discord.com, cdn.discordapp.com, gateway.discord.gg,
    media.discordapp.net}. Redirects out of allowlist rejected.
  - HSTS: we are a client; rely on httpx + certifi.

Internal traffic: N/A (no internal services).

Network topology: N/A (workstation).

WAF / edge: N/A (we are the outbound client, not a target).
```

### 5.5 Auth flow (inbound)

**N/A** — no inbound surface. Single-user CLI with no daemon port. The only authorisation boundary is OS-user-level (shell access to workstation + keyring).

### 5.6 Multi-tenant isolation

**N/A** — single user, single operator. Cross-guild on-disk isolation is directory namespacing on `guild_id`. No tenancy model exists.

---

## 6. Observability Design

### 6.1 Logging architecture

```
Pipeline:
  structlog → JSON renderer → stdout (primary) + optional rotating file (operator opt-in)

Processors (order matters):
  1. add_timestamp (ISO-8601 UTC)
  2. add_log_level
  3. add_run_id                          — bind once at scan start
  4. redact_token(t) "{t[:6]}***{t[-4:]}"   — SEC-P0-03
  5. redact_invite_code(c) "{c[:2]}***{c[-2:]}"  — REQ-NF-031
  6. (Phase 1) pydantic log-event model validator  — SEC-P1-03
  7. JSONRenderer

Per-request (REST) log event (from every httpx response hook):
{
  "timestamp": "2026-04-23T02:15:03.421Z",
  "level": "INFO",
  "run_id": "run_01HX…",
  "stage": "fetch.messages",
  "source": "discord.com/api",
  "url_hash": "sha256(path)[:16]",       — path only, never token
  "http_status": 200,
  "duration_ms": 142,
  "attempt": 1,
  "cache_hit": null,                     — N/A for scan; present in schema for Stage 1 parity
  "guild_id": "123…",
  "channel_id": "456…"
}

Per-gateway-event log event:
{
  "timestamp": "...",
  "level": "INFO",
  "run_id": "...",
  "stage": "gateway",
  "event_type": "HELLO" | "IDENTIFY" | "READY" | "HEARTBEAT" | "RESUME" | "DISCONNECT",
  "heartbeat_interval_ms": 41250,        — on HELLO
  "session_id": "redacted"               — NEVER raw; hashed
}

run.summary event (ONE per guild at end-of-scan, values match meta.json):
{
  "timestamp": "...",
  "level": "INFO",
  "stage": "run.summary",
  "run_id": "...",
  "guild_id": "...",
  "duration_sec": 4335,
  "message_count": 8432,
  "pinned_count": 47,
  "thread_count": 112,
  "attachment_count": 1291,
  "attachments_downloaded": 1180,
  "rate_limit_hits": 3,
  "gateway_disconnects": 0,
  "gateway_resumes": 0,
  "errors": []
}

NEVER logged (redaction + allowlist):
  - Raw burner token (any form)
  - Raw invite code (only redacted form)
  - Full CDN URL with signature (redacted to host + path[:32])
  - Message content body (we log counts, never content)
  - Author usernames (only author_id at DEBUG, never at INFO)
  - Cookie values
```

### 6.2 Metrics and dashboards

```
Metrics collection: none (local tool, no Prometheus).
Instead: per-scan summary event in structlog (see above) AND meta.json on disk.

Operator-visible counters (emitted to meta.json per guild per scan):
  message_count
  pinned_count
  thread_count
  attachment_count
  attachments_downloaded
  attachments_skipped[] (by reason)
  rate_limit_hits
  gateway_disconnects
  gateway_resumes
  errors[] (stage, source, redacted message)

Rationale: Prometheus/Datadog/Grafana are a SaaS-shaped overreach for a single-user CLI.
If the operator needs historical trending, `meta.json` files ARE the time series —
one file per (guild, date).

Alerting: N/A (no pager, no SLOs). The operator's CLI exit code + stderr log is the signal.
```

### 6.3 Distributed tracing

**N/A**. Single process, single event loop, no cross-service calls. structlog's `run_id` binding is sufficient causal correlation across all log records for one scan.

---

## 7. Failure Modes and Recovery

### 7.1 Failure mode analysis (each mapped to detection, impact, recovery)

| # | Component / trigger | Failure mode | Detection method | Impact | Recovery action | Recovery time |
|---|---------------------|--------------|------------------|--------|-----------------|---------------|
| 1 | Discord REST | **429 Too Many Requests** | httpx response hook reads status + `Retry-After` | Current request delayed | tenacity retry honouring `Retry-After`; after MAX_429_RETRIES=3 on same endpoint, skip channel; meta.errors[] append | seconds–minutes |
| 2 | Discord REST | **500/502/503/504** | httpx status | Request fails | tenacity exponential backoff (2s base, 60s max, 5 attempts) | seconds–minutes |
| 3 | Discord REST | **Plain 403 on channel** | httpx status | Channel inaccessible | Skip channel, log WARNING, append to meta.channels_skipped with reason "403_no_permission"; do NOT retry (retrying probes permissions, loud) | immediate |
| 4 | Discord REST | **401 on previously-working endpoint** | httpx status + cursor.last_scan_at comparison | Token likely invalidated (ban or password reset) | Exit code 3 ("detected-ban heuristic"); operator runbook: try login on browser | immediate |
| 5 | Discord REST | **Captcha challenge (401/403 + body `captcha_key\|captcha_sitekey\|captcha_service`)** | Response body hook | Discord wants interactive proof | **Hard abort, exit code 2.** Cursor persisted for current progress up to the previous successful channel. Operator runbook: rotate burner / wait / investigate. NO auto-solve (§8.3 security-model). | immediate |
| 6 | Network | **Connection drop mid-request** | httpx ConnectError / ReadTimeout | Current request fails | tenacity retries up to 5; if still fails, channel aborts; cursor unchanged → next run resumes | seconds |
| 7 | Network | **DNS failure for discord.com** | `socket.gaierror` at client construction | Cannot start | Exit 2 with clear error | operator reconnects |
| 8 | Gateway | **HELLO timeout after connect** | 10s timer expires before OPCODE 10 | Gateway unusable | Close WS; retry connect up to 3; if all fail, log WARNING, continue scan with gateway.enabled=false for this run (detection risk acknowledged) | seconds |
| 9 | Gateway | **Disconnect mid-scan** | `websockets.ConnectionClosed` | Session drops | OPCODE 6 RESUME with session_id+sequence to resume_gateway_url; increment gateway_disconnects; on success increment gateway_resumes | seconds |
| 10 | Gateway | **RESUME fails (OPCODE 9 Invalid Session)** | OPCODE 9 received | Session cannot resume | Fresh IDENTIFY; increment gateway_disconnects++; continue | seconds |
| 11 | Gateway | **Repeated RESUME failures (>3 in one scan)** | counter | Likely persistent issue | Log WARNING, continue scan without gateway (REST-only for remainder; noted in meta.errors[]) | immediate |
| 12 | Cursor | **Concurrent writer (second CLI invocation)** | filelock `Timeout` at startup | Would corrupt sqlite | Exit 1 with clear message: "another scan is running" | immediate |
| 13 | Cursor | **cursor.sqlite corruption** | sqlite3 `DatabaseError` at open | State lost for affected rows | Log ERROR; exit 1; operator runbook: backup cursor.sqlite, delete, next run treats all cursors as "0" (safe re-download, dedup by message_id downstream) | operator action |
| 14 | Cursor | **Clock skew (resolved_at in future)** | `resolved_at > now` | 7d TTL broken | Treat as fresh (do not delete); do not re-resolve (spec §10.5 Known Gap in security-model) | N/A |
| 15 | Filesystem | **Disk full during dump** | OSError ENOSPC on write | Current channel's dump incomplete | Delete partial file; cursor NOT advanced (channel re-runs next scan); log ERROR; exit 2 | operator clears space |
| 16 | Filesystem | **chmod 0o600 fails (Windows)** | OSError or silent non-effect | Less-strict permissions | Log WARNING once; continue (documented best-effort degradation) | N/A — mitigated by BitLocker on Windows |
| 17 | Filesystem | **Symlink escape attempt (output_root)** | os.path.realpath not within resolved output_root | Path traversal attempt | Abort write; exit 1; log ERROR; retention prune also refuses | immediate |
| 18 | Attachment | **CDN URL expired (~24h old cursor re-run)** | 403/404 on CDN | Download fails | Record URL-only with `download_failed=true`; continue | immediate |
| 19 | Attachment | **MIME mismatch with declared extension** | First-16-bytes sniff vs header | Possible exec-as-image | Discard stream bytes; record URL-only; meta.attachments_skipped `mime_mismatch`++ | immediate |
| 20 | Attachment | **Oversized (> max_size_mb * 1.1)** | Streamed-bytes counter | Potential zip-bomb | Abort stream, delete partial, record URL-only; meta.attachments_skipped `size_cap`++ | immediate |
| 21 | Config | **SSRF: config/redirect to non-allowlist host** | httpx event_hook validator | Data exfil attempt | Raise `SSRFViolation`; exit 1 | immediate |
| 22 | Config | **Stale Chrome UA (> 8 weeks)** | config.py validator at load | Anti-detection fingerprint obsolete | Exit 1 with remediation message; operator updates config | operator updates config |
| 23 | Config | **Plaintext keyring backend detected** | keyring class-name inspection | Token storage insecure | Exit 1 with remediation; refuse to run | operator installs real backend |
| 24 | Stage-1 input | **invites.enriched.json missing / >1 MB / malformed** | file stat + pydantic | Cannot determine scan targets | Exit 1 with clear error; log path (not content) | operator fixes file |
| 25 | Input | **Single-message pydantic ValidationError** | pydantic in fetch loop | One record invalid | Skip message with WARNING; run continues (REQ-NF-035) | immediate |

**Failure-mode count in §7: 25.**

### 7.2 Data consistency guarantees

```
Within-channel:
  Messages fetched in Discord's `after`-cursor order; written in sorted order at emit
  (channel_id ASC, timestamp ASC, message_id ASC). Cursor advanced ONLY after the
  channel's file write completes successfully. Therefore a crash mid-channel leaves
  the cursor at its pre-channel value → next scan re-dumps the channel → dedup at
  Stage 3 via message_id (or here, idempotent overwrite of the same date's file if
  the crash was same-day; cross-date duplicates possible and are a feature, not a
  bug: Stage 3 sees both snapshots).

Cross-channel:
  Each channel is an independent unit of work. The per-channel cursor decouples them.

Idempotence:
  Two scans on unchanged cursor state produce byte-identical *decompressed* JSONL
  content (REQ-NF-033). zstd frame bytes may differ; comparison is post-decompress.

Distributed transactions: N/A (single process, single sqlite).
Ordering: deterministic (sort-on-emit).
Concurrent writes: prevented by filelock singleton.
```

### 7.3 Circuit breakers and retry policies

```
Discord REST:
  Retry on: 429 (Retry-After honoured), 500, 502, 503, 504, httpx.ConnectError,
            httpx.ReadTimeout
  Do NOT retry: 400, 401, 403 (captcha or plain), 404, 405, SSRFViolation
  Backoff: exponential 2s base, 2x multiplier, 60s cap, jitter
  Attempts: config.retry.attempts (default 5)
  Per-endpoint 429 counter: MAX_429_RETRIES=3 → skip channel
  Circuit breaker: implicit via per-host token bucket (no separate breaker lib)
  Fallback when "broken": skip channel → meta.errors[] → continue next channel

Discord Gateway:
  Retry RESUME on disconnect: up to 3 consecutive; after 3, disable gateway for scan
  No circuit breaker library — failure is binary (session up / down)

Discord CDN:
  Retry on: 5xx, timeouts; NOT on 403/404 (URL expiry is not transient)
  Attempts: 3
  Fallback: record URL-only with `download_failed=true`

Keyring:
  No retry — first failure is fatal (exit 1)
```

### 7.4 Disaster recovery

```
Backups (operator responsibility):
  state/ and output/ — operator's choice (BitLocker volume snapshot, rsync, etc.)
  keyring entry — re-enterable via store-token

Recovery:
  Lost cursor.sqlite → next run treats all cursors as "0"; safe, but re-downloads.
                      Operator may prefer to restore from backup.
  Lost output/       → re-run; fresh scan on same date builds new subtree.
  Lost keyring entry → store-token again.

DR testing: N/A for a single-user CLI. Operator discretion.
```

---

## 8. Infrastructure and Deployment

### 8.1 Environment topology

```
Environments:
  Operator workstation — ONLY. No staging, no production. No cloud.
  Optional: local smoke-test dir with a fixture Stage-1 input (tests/fixtures/config.offline.yaml).

Install:
  pip install -e ".[dev]"
  discord-scanner store-token
  discord-scanner scan --config config.yaml

Cloud provider: NONE — spec §8.2 + brainstorm §2 lock the tool to workstation operation.
Container orchestration: NONE.
Infrastructure as code: NONE — no infrastructure.
```

### 8.2 CI/CD pipeline

```
CI (on every PR, GitHub Actions):
  Matrix: python-3.12 × {ubuntu-latest, windows-latest}  (REQ-NF-005)
  Steps:
    1. Checkout
    2. Install: pip install -e ".[dev]" --require-hashes (SEC-P0-30)
    3. ruff check src tests      → 0 errors
    4. ruff format --check src tests
    5. mypy --strict src         → 0 errors
    6. pytest -q --cov=src/discord_scanner --cov-fail-under=70
    7. bandit -r src/ --severity-level medium   (SEC-P0-31)
    8. pip-audit                                (REQ-NF-066)
    9. CI grep merge-blockers (SEC-P0-04, -17, -18, -29, REQ-NF-003, -051..-056):
         no `import requests|aiohttp|discord|anthropic`
         no `from selenium|from playwright`
         no `verify=False`
         no raw Discord token pattern
         no `time.sleep` inside `async def`
         no `fcntl` import in src/
         no raw `discord.gg/` | `discord.com/invite/` in logger.*/structlog.*
         `docs/claude/design-decisions.md` contains Third-party PII section (SEC-P0-27)
         `.gitignore` contains `output/` + `state/` + `.env` (SEC-P0-28, -32)
   10. (monthly cron, Phase 1) Chrome stable-version probe (SEC-P1-01)

CD:
  NONE. No deploys. Tool is installed manually on the operator's machine.

Rollback: N/A (no deploy to roll back).
```

---

## 9. Technology Decisions Reference

| Decision | Choice | Alternatives | Justification |
|----------|--------|--------------|---------------|
| UUID / ID type | Discord snowflake (str) | uuid4, uuid7 | Upstream-supplied; we don't mint IDs |
| Soft deletes | N/A | — | Retention prune is hard-delete (directory removal) |
| Row-level security | N/A | — | Single-user, no tenants |
| Timezone in storage | ISO-8601 UTC, tz-aware | Naive UTC, unix epoch | Round-trip through Discord + JSONL + sqlite; pydantic-friendly |
| SQL placeholders | `?` (sqlite stdlib) | Named (`:param`) | Simpler, stdlib-native |
| Date bucket | `YYYY-MM-DD` operator tz | ISO week, unix day | Matches spec §3.8 and sister project |
| Compression | zstandard level default 3 | gzip, level 22 | CPU/size balance; level 3 is enough; deterministic verification is on decompress (spec §8.8) |
| Logging format | JSON via structlog | Plain text, logfmt | Greppable by operator; schema-ready for Phase 1 pydantic validation |
| Python version floor | 3.12 | 3.11 | asyncio taskgroups; typing ergonomics; match civit-hf-scanner |

---

## 10. Architecture Decision Records (ADR)

Thirteen ADRs. Each captures a critical, non-obvious decision.

### ADR-001: Accept Discord ToS violation as first-class threat
**Status**: Accepted.
**Context**: Using a user-token for automated access violates Discord ToS §3 (self-bots). The operator has accepted this in writing (spec §8.1). Every other decision flows from this one.
**Decision**: Treat Discord's anti-abuse platform as an **active adversary**, not a passive infrastructure provider. Every control in §5 is measured by: "does this keep the burner alive longer?"
**Consequences**: Anti-detection discipline is non-negotiable (§4 seed-spec); a burner ban is an *accepted outcome*, not a defect; the operator owns the blast radius via disposable-burner discipline.
**Alternatives considered**: Bot-token path — rejected (operator has no admin on target guilds); OAuth app — rejected (Discord would not approve a scraping app); do nothing — rejected (use case exists).

### ADR-002: No `discord.py` / `discord.py-self` dependency
**Status**: Accepted.
**Context**: Existing libraries (`discord.py`, `pycord`) require bot tokens. `discord.py-self` supports user tokens but is unmaintained and still violates ToS (no anti-detection advantage over rolling our own).
**Decision**: Hand-rolled client over `httpx[http2]` + `websockets>=13`. Forbidden-lib grep blocks any `import discord`.
**Consequences**: More code to write (session/rest + session/gateway); surgical control of every header byte and every OPCODE; no dependency surprises on upgrades; aligned with §4 fingerprint coherence invariant.
**Alternatives**: `discord.py-self` — rejected: (a) adds supply-chain risk, (b) may not match our X-Super-Properties expectations, (c) CI grep already blocks its import shape.

### ADR-003: Dormant-but-present gateway session
**Status**: Accepted.
**Context**: `dsc-smartscraper` audit showed a user account with 100% REST and 0% gateway activity is a strong automation signal — real Discord clients maintain a persistent WebSocket for presence/events.
**Decision**: Open gateway WSS before first REST call; send OPCODE 2 IDENTIFY with `properties` identical to REST `X-Super-Properties`; respect HELLO heartbeat; send one PRESENCE UPDATE; **process no events**; OPCODE 6 RESUME on drop.
**Consequences**: Adds one long-lived asyncio task per scan; requires fingerprint invariant (§5.2); slightly higher ban-detection cost if Discord scrutinises per-account gateway sessions (captcha is handled by hard-abort anyway).
**Alternatives**: REST-only — rejected (detection delta). `discord.py-self` gateway — rejected (ADR-002).

### ADR-004: Per-burner cookie jar file
**Status**: Accepted.
**Context**: Discord sets `__dcfduid`, `__sdcfduid`, `locale` cookies; a real browser persists them across sessions. Old scraper discarded cookies per run.
**Decision**: `httpx.AsyncClient(cookies=<jar>)` loaded from `state/cookies-<auth.keyring_username>.json` per scan start, saved at scan end. Filename includes burner alias to prevent cross-burner leakage (SEC-P0-12).
**Consequences**: Cookies are credential-adjacent; chmod 0o600 required; rotated on burner rotation.
**Alternatives**: Fresh cookies every run — rejected (detection anomaly). Shared cookie jar — rejected (leakage).

### ADR-005: SQLite cursor store (one row per (guild_id, channel_id))
**Status**: Accepted.
**Context**: Old scraper used JSON-per-channel files with fragile per-file locks; failure modes included partial writes and POSIX-only `fcntl`.
**Decision**: Single `state/cursor.sqlite` with WAL; one row per (guild_id, channel_id); atomic upsert via `?`-parameterised `INSERT ... ON CONFLICT(...) DO UPDATE`.
**Consequences**: Cross-platform; atomic; backup-friendly (one file); migrating forward is a `CREATE TABLE IF NOT EXISTS` + `INSERT ... SELECT` pattern.
**Alternatives**: JSON-per-file — rejected (locking complexity). Postgres — rejected (infra overreach). lmdb — rejected (stack discipline).

### ADR-006: `filelock` everywhere, `fcntl` nowhere
**Status**: Accepted.
**Context**: Primary dev target is Windows 11; `fcntl` is POSIX-only; old scraper broke on Windows.
**Decision**: Use `filelock` library for all single-writer guards: `state/cursor.lock` (cursor writer), `state/gateway-<burner>.lock` (singleton gateway). CI grep blocks `fcntl` import in src/ (REQ-NF-054).
**Consequences**: Advisory locking semantics (vs. mandatory) — acceptable for single-user tool. Cross-platform.
**Alternatives**: `fcntl` + `msvcrt` branch — rejected (complexity). Hand-rolled lockfile — rejected (edge cases).

### ADR-007: zstandard for messages.jsonl compression
**Status**: Accepted.
**Context**: Messages.jsonl dominates on-disk size; gzip is ~5x larger at similar CPU; the output is the Stage 2 → Stage 3 contract so format must be stable.
**Decision**: `zstandard>=0.22` at default compression level (3). Pinned as the stage contract.
**Consequences**: Stage 3 must depend on `zstandard` too. Idempotence is asserted on decompressed bytes (spec §8.8) because frame state varies.
**Alternatives**: gzip — rejected (size). Plain JSONL — rejected (disk bloat over 30-day retention).

### ADR-008: Cross-platform `keyring` (not macOS-only Keychain)
**Status**: Accepted.
**Context**: Old scraper used macOS Keychain via bespoke code + plaintext fallback. Operator is on Windows 11.
**Decision**: `keyring>=24.0`. Refuses `keyrings.alt.*` plaintext backend (SEC-P0-06). Loads in priority order: keyring → env → config (last-resort, warned).
**Consequences**: Uses DPAPI on Windows, Keychain on macOS, libsecret on Linux — all native. If no real backend available, operator must install one; tool refuses to run.
**Alternatives**: Env-only — rejected (env leaks to process lists and child processes). Config-only — rejected (plaintext on disk).

### ADR-009: `httpx[http2]` — HTTP/2 mandatory, not `urllib`
**Status**: Accepted.
**Context**: Discord's real clients negotiate HTTP/2 via ALPN; HTTP/1.1 is distinguishable at the TLS layer. Old scraper used `urllib` (HTTP/1.1).
**Decision**: `httpx[http2]>=0.27` with `AsyncClient(http2=True, verify=True)`; single construction site.
**Consequences**: Requires `h2` transitive dep; CI grep blocks `requests`/`aiohttp`/`urllib3` usage in src.
**Alternatives**: `requests` — rejected (HTTP/1.1 only, sync). `aiohttp` — rejected (HTTP/1.1, fingerprint drift).

### ADR-010: JSON log events pass through pydantic-like processors
**Status**: Accepted; Phase 1 adds strict validation (SEC-P1-03).
**Context**: Structured logs that accidentally include un-redacted tokens or invite codes are a silent compliance bug.
**Decision**: structlog processor chain: `redact_token` + `redact_invite_code` before JSON rendering. Phase 1 adds a pydantic log-event model that every record is validated against; unknown keys rejected.
**Consequences**: Tiny per-log-record CPU overhead; strong guarantee no secret leaks from new log call sites.
**Alternatives**: Ad-hoc `str.replace()` — rejected (too easy to miss).

### ADR-011: Captcha → hard abort, never auto-solve
**Status**: Accepted.
**Context**: Auto-solve temptation via 2captcha or ML is (a) additional ToS escalation, (b) creates an ML fingerprint Discord can train against, (c) adds a paid-service dependency with its own secrets, (d) still fails on behavioural captchas.
**Decision**: On 401/403 + body matching captcha indicators, raise `CaptchaDetected`; cli.py maps to exit code 2. Cursor persisted for progress up to the previous successfully-dumped channel. Operator runbook covers rotation. **No captcha-solve library is permitted.**
**Consequences**: Clean abort; operator reacts manually; honest signal of detection pressure on burner.
**Alternatives**: Auto-solve — rejected (see above).

### ADR-012: Scope locked to `src/discord_scanner/` — Stage 3 is a separate repo
**Status**: Accepted.
**Context**: Old scraper `dsc-smartscraper` mixed scraping with curation (LLM, Obsidian, review UI, learner). Entanglement caused the 570-LOC `monitor.py`.
**Decision**: Stage 2 is mechanical scraping only. Stage 3 (`discord-curator`, future separate repo) does LLM filtering, Obsidian synthesis, scoring, review UI. Forbidden imports: `obsidian-*`, `anthropic`.
**Consequences**: Clean output contract (`messages.jsonl.zst` + sidecars, `schema_version: 1`); no shared code with Stage 3 except for design principles.
**Alternatives**: Monolithic repo — rejected (the problem this rebuild solves).

### ADR-013: Date path `YYYY-MM-DD` in operator local timezone, not UTC
**Status**: Accepted (for path bucketing); all internal timestamps stay UTC.
**Context**: Operator triggers daemon in `scan_start_window: "02:00-06:00 UTC"`; output bucketed by operator's intuitive date.
**Decision**: `output/{guild_id}/{YYYY-MM-DD}/` uses operator's local-wall-clock date at scan start; all `datetime` fields inside artefacts remain UTC (ISO-8601 tz-aware). `prior.txt` records the previous bucket in the same local-date convention.
**Consequences**: A single scan never crosses a date-boundary path-wise. One day's worth of operator runs land in the same directory. Cross-tz backfill may need manual date remap (Phase 1 concern).
**Alternatives**: UTC date bucket — rejected (operator intuition mismatch). Unix-day — rejected (human-unreadable).

**ADR count in §10: 13.**

---

## Known Gaps

These are open questions or acknowledged shortcomings that do not block the MVP but should be tracked. They extend the seed-spec's own "Known Gaps" list with architecture-level items.

1. **Stage 1.5 enrichment not yet shipped** — `intent` and `confidence` fields default to absent; `intent_allowlist` becomes an empty filter (permissive). Operator must narrow via `min_score_pct` alone until Stage 1.5 lands.
2. **Path to `invites.enriched.json`** — default assumes sibling-directory layout; no cross-repo contract test; first real-run breakage risk.
3. **Monthly Chrome-version CI probe** (SEC-P1-01) — source of truth TBD; `chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Windows` is a recommended endpoint, to be pinned in Phase 1.
4. **Token-pool rotation semantics** (`auth.rotate_every`) — interaction with single-gateway-per-burner constraint not fully specified. Default (pool size 1 / `per_server`) is unambiguous; multi-token pool is a post-MVP concern.
5. **`detected-ban` exit code 3 heuristic** — exact signal for "token invalid + no recent change" not pinned; candidate: 401 on `list-guilds` + cursor mtime within N hours.
6. **Retention × daemon × Stage 3 race** — if retention prunes while Stage 3 consumes a prior day's output, Stage 3 could race. Documented as Stage 3's problem to snapshot before consuming.
7. **Windows chmod is best-effort** — true ACL protection requires `win32security` (rejected for stack discipline). Documented degradation; operator is instructed to enable BitLocker.
8. **Idempotence tolerance for zstd frame bytes** — two runs produce byte-identical *decompressed* JSONL, not byte-identical `.jsonl.zst` files. Tests compare post-decompress. Acceptable per seed-spec §8.8.
9. **Gateway single-burner filelock across multiple machines** — out of scope: the tool assumes one workstation. If the operator runs from two machines with the same burner, the remote filelock is not visible. Documented in runbook.
10. **No distributed tracing across retries** — a retried request has multiple log records sharing `run_id` but no trace-span id. Acceptable for a single-process tool.
11. **No SBOM in Phase 0** — `cyclonedx-py` added in Phase 1 (SEC-P1-04).
12. **Attachment download on resumed scan** — if a message was dumped yesterday but its attachment was URL-only, today's resumed scan will NOT go back to re-download (cursor has advanced past it). Operator runs a manual backfill if needed. Acceptable given the 24h CDN URL expiry makes this mostly moot.

**Known Gaps count: 12.**

---

## Document quality self-check

- [x] Every component has defined responsibility + explicit non-responsibilities (§2.2).
- [x] Every data flow annotated with protocol + auth + sensitivity (§2.1 + §1.1).
- [x] Every decision in §1.2 references a REQ / SEC-P0 / spec section.
- [x] Security architecture (§5) cross-references every SEC-P0-01..32 from security-model.md to a concrete control.
- [x] Failure mode analysis (§7.1) covers all external dependencies + local failure paths (25 rows).
- [x] Data model (§3) covers every entity implied by spec §3 and §7.
- [x] Scale: N/A stated explicitly — single-user workstation tool, no scale axis (§1.1, §6.2, §8.1).
- [x] ASCII diagrams only. No Mermaid.
- [x] N/A sections (inbound auth, RLS, tenancy, WAF, k8s) reasoned, not silently omitted.
