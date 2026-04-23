# Requirements Specification: discord-scanner (Stage 2)

**Version**: 1.0
**Date**: 2026-04-23
**Author**: requirements-engineer agent
**Status**: Draft
**Relationship to spec.md**: This document exhaustively expands `spec.md`, `seed-spec.md`, `brainstorm.md`, and `security-model.md`. Spec.md defines WHAT the product is; this document defines EVERYTHING required to build and operate it correctly. Every Phase 0 blocking item from `security-model.md §6 (SEC-P0-01..32)` is represented as a REQ-NF with traceability back.

**Scope reminder**: `discord-scanner` is a single-user, workstation-only, Python 3.12+ CLI. No server, no daemon port, no multi-tenant. Inbound authentication is N/A; the only credential handled is the outbound Discord burner user token. Sections 5 (Accessibility), 6 (i18n), and 7 (Browser/Device) of the standard template are flagged N/A with reasoned one-sentence explanations — this is a headless, English-only, local CLI.

---

## 1. Functional Requirements

### 1.1 Invite resolution

**REQ-F-001: Load invite list from Stage 1 artefact**
- Source: spec.md §3.1, seed-spec.md §2.1, brainstorm.md §3
- Priority: Must
- User story: As the Operator, I want the scanner to read `invites.enriched.json` produced by `civit-hf-scanner` so that I only scan invites I have already curated.
- Acceptance criteria:
  1. Given `config.discovery.invites_input` points to a well-formed `invites.enriched.json`, when the scanner starts, then it loads and deserialises the file via pydantic v2 before any HTTP call.
  2. Given the file is missing, when the scanner starts, then it exits 1 with a structured error naming the missing path (no stack trace).
  3. Given the file exceeds 1 MB, when the scanner loads it, then it exits 1 (JSON-bomb guard, per security-model.md §4.1).
- Edge cases:
  - File exists but is empty `[]` → log INFO "no invites", exit 0 after retention prune (nothing to scan).
  - File has records with missing `score_pct` or `intent` → skip the record with a WARNING; do not halt.
  - File has records with invite codes not matching `^[A-Za-z0-9-]{4,20}$` → skip with WARNING.
- Notes: pydantic model uses `extra='allow'` (schema-tolerance requirement).

**REQ-F-002: Apply score + intent filter**
- Source: spec.md §3.1, seed-spec.md §2.1
- Priority: Must
- User story: As the Operator, I want only high-scoring, intent-allowed invites scanned so that I don't waste burner-account activity on low-signal guilds.
- Acceptance criteria:
  1. Given the loaded invite list, when the filter runs, then only records with `score_pct >= config.discovery.filter.min_score_pct` (default 70) AND `intent ∈ config.discovery.filter.intent_allowlist` (default `[prompt_sharing, tutorials, workflows, collab]`) are kept.
  2. Given the `intent_allowlist` is empty `[]`, when the filter runs, then no intent filter is applied (Stage-1.5 absent path — see Known Gaps).
  3. Given `config.discovery.filter.exclude_nsfw=true`, when the filter runs, then invites with `nsfw=true` are dropped.
- Edge cases:
  - All invites filtered out → log INFO, continue to manual_invites merge.
- Notes: Filter decision is logged per invite at DEBUG with redacted code (REQ-NF-029).

**REQ-F-003: Merge manual_invites**
- Source: spec.md §3.1, seed-spec.md §2.1
- Priority: Must
- User story: As the Operator, I want to add invites not sourced from Stage 1.
- Acceptance criteria:
  1. Given `config.discovery.manual_invites` contains codes matching `^[A-Za-z0-9-]{4,20}$`, when the scanner runs, then those codes are appended to the filtered Stage-1 list.
  2. Duplicates (same code) are deduplicated; the Stage-1 record wins for metadata.

**REQ-F-004: Resolve invite → guild_id via Discord API**
- Source: spec.md §3.1, seed-spec.md §2.1
- Priority: Must
- User story: As the Operator, I need each invite code resolved to a `guild_id` so the scanner can enumerate its channels.
- Acceptance criteria:
  1. For each unique invite code, the scanner calls `GET /api/v10/invites/{code}?with_counts=true&with_expiration=true` exactly once (cache hit within 7 days skips the call).
  2. On success, `guild.id` and `guild.name` are persisted to `state/invite_cache.sqlite` with `resolved_at` timestamp.
  3. On 404 (invalid/expired invite), log WARNING with redacted code and skip.
- Edge cases:
  - Response missing `guild` field → log WARNING, skip invite.
  - Response has `guild` but invalid `id` → same.
- Notes: This is the ONE sanctioned Discord-side enrichment call in the whole Stage 1→2 pipeline (spec.md §3.1).

**REQ-F-005: Honour 7-day invite-resolution cache**
- Source: seed-spec.md §2.1
- Priority: Must
- Acceptance criteria:
  1. If a cached resolution exists with `resolved_at >= now - 7d`, reuse it (zero HTTP call).
  2. Stale rows (> 7d) are deleted before the network call in the same transaction.
- Edge cases:
  - Clock skew (cache from future) → treat as fresh; do not delete.

### 1.2 Guild enumeration

**REQ-F-006: List guilds the burner has joined**
- Source: spec.md §3.2, seed-spec.md §2.2
- Priority: Must
- Acceptance criteria:
  1. `GET /api/v10/users/@me/guilds` is called exactly once per scan.
  2. Response is deserialised; a guild is "scannable" iff it is in BOTH the resolved invite list AND this response.
  3. Guilds in the invite list but not in the burner's membership are logged at WARNING with `reason: "burner_not_joined"`, appended to `meta.json:channels_skipped[]` equivalent at guild-level.
- Edge cases:
  - Burner in zero guilds → log INFO, exit 0 (nothing to scan).
  - Burner in > 200 guilds → no issue; Discord paginates but `/users/@me/guilds` default page is sufficient for expected burner scale (≤50).

**REQ-F-007: List channels per scannable guild**
- Source: spec.md §3.2, seed-spec.md §2.2
- Priority: Must
- Acceptance criteria:
  1. `GET /api/v10/guilds/{guild_id}/channels` is called once per scannable guild.
  2. Filter to channel types `0` (GUILD_TEXT), `5` (GUILD_ANNOUNCEMENT), `15` (GUILD_FORUM).
  3. Apply per-guild `config.channels.{guild_id}.include_channels` / `exclude_channels` overrides after type filter.
- Edge cases:
  - 403 on channel list → skip the guild, log WARNING, append to `meta.json:channels_skipped[]` at guild level.
  - Zero accessible channels after filter → log INFO, proceed to next guild, still write empty artefacts + meta.

**REQ-F-008: Fetch role names (no member enumeration)**
- Source: spec.md §3.2, seed-spec.md §2.2
- Priority: Must
- Acceptance criteria:
  1. `GET /api/v10/guilds/{guild_id}/roles` called exactly once per guild per scan.
  2. Role names are available to the message fetcher for mention resolution.
  3. The scanner MUST NOT call `/guilds/{id}/members` or any member-list endpoint (merge-blocker, per security-model.md §2.1).

### 1.3 Message fetching

**REQ-F-009: Paginate messages from channel**
- Source: spec.md §3.3, seed-spec.md §2.3
- Priority: Must
- Acceptance criteria:
  1. For each accessible channel, call `GET /api/v10/channels/{channel_id}/messages?limit=100&after={cursor}` where `cursor` = last persisted `message_id` from `state/cursor.sqlite` (or `0` on first scan).
  2. Paginate until either fewer than 100 messages returned (end of new) OR `config.http.max_messages_per_scan` (default 10 000) reached.
  3. Each page applies per-request jitter `random.uniform(1.5, 4.0)` s before the next call.
- Edge cases:
  - Empty channel (zero messages ever) → first response is `[]`, cursor unchanged, 0 messages dumped.
  - Channel with 200 000 messages backlog → capped at `max_messages_per_scan=10 000`; subsequent scans resume from new cursor. Operator is informed via `meta.json:errors[]` with `reason: "max_messages_per_scan_reached"` per affected channel.
  - Single oversized message (body > 4 MB) → pydantic response cap (REQ-NF-046) triggers, message skipped with WARNING.
- Notes: Discord `after` parameter returns messages newer than `last_message_id`.

**REQ-F-010: Fetch pinned messages**
- Source: spec.md §3.3, seed-spec.md §2.3
- Priority: Must
- Acceptance criteria:
  1. For each accessible channel, call `GET /api/v10/channels/{channel_id}/pins` exactly once per scan.
  2. Pinned messages are written to `pinned.jsonl` (plain, not zstd-compressed per seed-spec §2.8).
  3. Pinned messages are NOT advanced into cursor (they may repeat across scans — consumer dedupe by `message_id`).

**REQ-F-011: Fetch forum archived + active threads**
- Source: spec.md §3.3, seed-spec.md §2.3
- Priority: Must
- Acceptance criteria:
  1. For each channel with `type==15` (GUILD_FORUM), call `GET /api/v10/channels/{channel_id}/threads/public_archived_threads?limit=50` (paginated via `before` cursor if `has_more`).
  2. Additionally call `GET /api/v10/guilds/{guild_id}/threads/active` once per guild to enumerate active threads across all forum channels.
  3. For each thread, call `GET /api/v10/channels/{thread_id}/messages?...` paginated as in REQ-F-009, capped at `config.channels.{guild_id}.max_thread_history` (default 500).
  4. Thread messages are written to `threads.jsonl` with `parent_channel_id` populated.
- Edge cases:
  - 403 on a thread → skip thread, log WARNING, append to `meta.json:channels_skipped[]` with `reason: "403_no_permission", thread_id: ...`.
  - Thread deleted between listing and fetch → 404; skip silently.

**REQ-F-012: Inter-channel burst pause**
- Source: spec.md §3.3, seed-spec.md §2.3, §4.5
- Priority: Must
- Acceptance criteria:
  1. Between the completion of one channel's full fetch and the start of the next, the scanner MUST `asyncio.sleep(random.uniform(config.http.burst_pause_sec[0], config.http.burst_pause_sec[1]))` (default `[30, 90]`s).
  2. The pause applies inside a single guild's channel loop AND between the last channel of one guild and the first of the next.

### 1.4 Attachment handling

**REQ-F-013: Download image attachments**
- Source: spec.md §3.4, seed-spec.md §2.4
- Priority: Must
- Acceptance criteria:
  1. For each message with `attachments[]`, for each attachment whose extension is in `config.attachments.image_extensions` (default `[.png, .jpg, .jpeg, .webp, .gif]`) AND whose declared `size <= config.attachments.max_size_mb * 1_000_000`, stream-download via the same httpx client.
  2. Target path: `output/{guild_id}/{date}/attachments/{msg_id}_{sanitised_filename}`.
  3. Filename sanitisation: `pathlib.PurePosixPath(filename).name`, null-byte strip, replace `[<>:"/\\|?*\x00-\x1f]` with `_`, length cap 128 (REQ-NF-026; SEC-P0-21).
- Edge cases:
  - Download exceeds `max_size_mb * 1.1` streamed → abort + delete partial (REQ-NF-025; SEC-P0-20).
  - MIME sniff mismatch with declared extension → discard bytes, record URL only, log WARNING (REQ-NF-024; SEC-P0-19).
  - Network drop mid-stream → partial deleted; attachment recorded URL-only with `download_failed=true` in output.

**REQ-F-014: Record non-image / oversized attachments URL-only**
- Source: spec.md §3.4, seed-spec.md §2.4
- Priority: Must
- Acceptance criteria:
  1. For attachments not matching REQ-F-013 criteria (non-image extension OR oversized OR MIME mismatch), emit the attachment record with `cdn_url` populated and `local_path=null`.
  2. Increment `meta.json:attachments_skipped[]` counters per reason (`non_image`, `size_cap`, `mime_mismatch`).

**REQ-F-015: CDN downloader shares REST fingerprint**
- Source: spec.md §3.4, seed-spec.md §2.4, §4.1
- Priority: Must
- Acceptance criteria:
  1. The CDN downloader sends the same `User-Agent` + `Sec-Ch-Ua*` + `Sec-Fetch-*` headers as REST (REQ-NF-007..REQ-NF-021).
  2. Separate per-host token bucket at `config.http.per_host_rate_per_sec["cdn.discordapp.com"]` (default 1 req/s).
  3. The Discord user token is NOT sent in the Authorization header to `cdn.discordapp.com` / `media.discordapp.net` (CDN URLs are pre-signed) — asserted in unit test.

### 1.5 Gateway session (dormant-but-present)

**REQ-F-016: Connect gateway WebSocket**
- Source: spec.md §3.5, seed-spec.md §2.5
- Priority: Must
- Acceptance criteria:
  1. Scanner opens `wss://gateway.discord.gg/?v=10&encoding=json` before the first REST call.
  2. Scheme is `wss://` (TLS); `ws://` is rejected (SEC-P0-10).

**REQ-F-017: Send OPCODE 2 IDENTIFY**
- Source: spec.md §3.5, seed-spec.md §2.5
- Priority: Must
- Acceptance criteria:
  1. Within 5 s of connect, send OPCODE 2 with `properties` blob matching the REST `X-Super-Properties` byte-for-byte (SEC-P0-25).
  2. `properties` fields: `os`, `browser`, `browser_version`, `os_version`, `device`, `system_locale`, `browser_user_agent`, `client_build_number`, `release_channel`.

**REQ-F-018: Process HELLO heartbeat**
- Source: spec.md §3.5, seed-spec.md §2.5
- Priority: Must
- Acceptance criteria:
  1. On OPCODE 10 (HELLO), read `heartbeat_interval` (clamped `[1000, 120000]` ms per security-model.md §4.1).
  2. Schedule OPCODE 1 HEARTBEAT every `heartbeat_interval * jitter(0.8, 1.0)` ms.
  3. If an override `config.gateway.heartbeat_interval_ms` is set, use it instead.

**REQ-F-019: Send OPCODE 3 PRESENCE UPDATE once**
- Source: spec.md §3.5, seed-spec.md §2.5
- Priority: Must
- Acceptance criteria:
  1. On receipt of READY (OPCODE 0 with `t=READY`), send exactly one OPCODE 3 with `{status: config.gateway.presence, activities: [], afk: false}`.
  2. Default presence `online`; configurable to `idle | dnd | invisible`.

**REQ-F-020: Do NOT process events (dormant)**
- Source: spec.md §3.5, seed-spec.md §2.5
- Priority: Must
- Acceptance criteria:
  1. Only `heartbeat_interval` (from HELLO), `session_id` + `sequence` (from READY) are read and stored.
  2. All other event payloads are discarded silently (not logged at DEBUG; not stored; not counted beyond a single INFO "gateway_event_ignored").
- Notes: prevents accidental processing surface and reduces log volume.

**REQ-F-021: OPCODE 6 RESUME on disconnect**
- Source: spec.md §3.5, seed-spec.md §2.5
- Priority: Must
- Acceptance criteria:
  1. On disconnect mid-scan, if `config.gateway.resume_on_drop=true` (default), attempt OPCODE 6 RESUME with stored `session_id` + `sequence`.
  2. If RESUME fails (Discord closes with 4000/4007/4009), fall back to fresh IDENTIFY; increment `meta.json:gateway_resumes++` on success, `gateway_disconnects++` on every disconnect.

**REQ-F-022: Gateway stays up for whole scan**
- Source: spec.md §3.5, seed-spec.md §2.5
- Priority: Must
- Acceptance criteria:
  1. Gateway WS is NOT torn down between channels, guilds, or scans-within-daemon; only on clean shutdown (scan end, SIGINT/SIGTERM).
  2. If gateway fails to reconnect after `config.retry.attempts` (default 5), log ERROR and continue REST-only with `meta.json:gateway_disconnects` counter set.

### 1.6 Cursor / resumability

**REQ-F-023: Persist cursor per (guild_id, channel_id)**
- Source: spec.md §3.7, seed-spec.md §2.7
- Priority: Must
- Acceptance criteria:
  1. `state/cursor.sqlite` has table `cursor(guild_id TEXT, channel_id TEXT, last_message_id TEXT, updated_at TEXT, PRIMARY KEY(guild_id, channel_id))`.
  2. Cursor is advanced ONLY after a channel's `messages.jsonl.zst` line-write commits AND `fsync` returns.
  3. If the write crashes mid-channel, the cursor is unchanged; re-run resumes from the pre-crash value.
- Edge cases:
  - SQLite locked by a stale `filelock` → exit 1 with clear error (REQ-F-024).
  - Corrupt cursor DB → log ERROR, exit 1 with operator runbook entry ("delete `state/cursor.sqlite` to force full re-scan").

**REQ-F-024: Single-writer filelock on state**
- Source: spec.md §3.7, seed-spec.md §2.7, security-model.md §2.4
- Priority: Must
- Acceptance criteria:
  1. `filelock.FileLock(state/cursor.lock)` acquired with `timeout=0` at scan start.
  2. Second invocation of `scan` / `daemon` while the first holds the lock exits 1 with error "another scan in progress (state/cursor.lock held)".
  3. Cross-platform: works identically on Windows, macOS, Linux. `fcntl` is forbidden (SEC-P0-29).

### 1.7 Output artefacts

**REQ-F-025: Write messages.jsonl.zst**
- Source: spec.md §3.8, seed-spec.md §2.8, §7.1
- Priority: Must
- Acceptance criteria:
  1. Per-channel messages flushed to `output/{guild_id}/{YYYY-MM-DD}/messages.jsonl.zst`.
  2. One JSON object per line, schema per seed-spec §7.1; `schema_version=1`.
  3. zstandard compression; decompressed JSONL MUST be byte-identical across two runs with unchanged cursor (REQ-NF-034).
- Edge cases:
  - Zero new messages this run → empty `messages.jsonl.zst` (valid zstd frame with no content) is still written.

**REQ-F-026: Write pinned.jsonl (plain)**
- Source: spec.md §3.8, seed-spec.md §2.8
- Priority: Must
- Acceptance criteria:
  1. Plain JSONL (no compression) at `output/{guild_id}/{YYYY-MM-DD}/pinned.jsonl`, same schema as messages.
  2. Sorted by `(channel_id ASC, timestamp ASC, message_id ASC)` (REQ-NF-032).

**REQ-F-027: Write threads.jsonl (plain)**
- Source: spec.md §3.8, seed-spec.md §2.8
- Priority: Must
- Acceptance criteria:
  1. Plain JSONL at `output/{guild_id}/{YYYY-MM-DD}/threads.jsonl`, same schema as messages, `parent_channel_id` populated.

**REQ-F-028: Write meta.json**
- Source: spec.md §3.8, seed-spec.md §2.8, §7.2
- Priority: Must
- Acceptance criteria:
  1. After all channels/threads fetched for a guild, write `output/{guild_id}/{YYYY-MM-DD}/meta.json` with schema per seed-spec §7.2 (`schema_version=1`).
  2. `scan_started_at`, `scan_completed_at` are ISO 8601 UTC.
  3. All counters (`message_count`, `pinned_count`, `thread_count`, `attachment_count`, `attachments_downloaded`, `attachments_skipped`, `rate_limit_hits`, `gateway_disconnects`, `gateway_resumes`) are populated.
  4. `channels_skipped[]` contains every 403 / max-messages-reached / captcha-skipped channel with `{id, reason, [thread_id]}` entries.
- Edge cases:
  - Scan aborted mid-guild by captcha → `meta.json` still written with `scan_completed_at=null` and `errors[]` populated (REQ-F-044).

**REQ-F-029: Write prior.txt**
- Source: spec.md §3.8, seed-spec.md §2.8, §7.3
- Priority: Must
- Acceptance criteria:
  1. At scan start for each guild, scan `output/{guild_id}/` for subdirectories matching `YYYY-MM-DD`, pick the most-recent that is NOT today's date.
  2. Write that date string (or empty on first scan) to `output/{guild_id}/{YYYY-MM-DD}/prior.txt`.

**REQ-F-030: Attachment directory**
- Source: spec.md §3.8, seed-spec.md §2.8
- Priority: Must
- Acceptance criteria:
  1. `output/{guild_id}/{YYYY-MM-DD}/attachments/` directory created `0o700` on first write.
  2. Each attachment file created `0o600` immediately after write (REQ-NF-037).

### 1.8 Admin / operator features

discord-scanner has no classic admin surface (single-user tool). The "admin features" table collapses to:

| Admin feature | Status | Notes |
|---|---|---|
| User management | Not applicable | Single user |
| Role management | Not applicable | No roles |
| Billing / orgs / API keys / feature flags / broadcast / health dashboard / email templates | Not applicable | Single user, local CLI |
| Audit log viewer | Not required | Structured logs to stdout / file (REQ-NF-029) suffice |
| Usage analytics | Rejected | No telemetry (security-model.md §8.3) |
| Data export | Handled by retention-purge command | REQ-F-045 |
| Data deletion (GDPR self-service) | Required via Phase 1 `retention --purge-all` | REQ-F-045 |
| Integration management | Not applicable | Static Discord REST + gateway only |

### 1.9 CLI surface (8 commands + 4 global flags)

**REQ-F-031: `discord-scanner resolve` command**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Takes invite codes from `config.discovery.*` OR from `--invite` flag (repeatable).
  2. Calls `/api/v10/invites/{code}` via the burner; prints `{invite_code (redacted), guild_id, guild_name}` per invite to stdout (rich-formatted table).
  3. Caches resolutions to `state/invite_cache.sqlite` (REQ-F-005).
  4. Exit code 0 on success; 1 on config error; 2 on network/captcha.

**REQ-F-032: `discord-scanner list-guilds` command**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Calls `GET /api/v10/users/@me/guilds`; prints `{guild_id, guild_name}` per joined guild.
  2. Token is loaded via REQ-F-039 auth mechanism; exit 3 ("detected-ban heuristic") if 401.

**REQ-F-033: `discord-scanner scan` command (full)**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Full scan of all configured guilds that pass the filter (REQ-F-002..REQ-F-003) AND are in the burner's membership (REQ-F-006).
  2. Writes all output artefacts (REQ-F-025..REQ-F-030) per guild.
  3. Exit 0 success; 1 config; 2 runtime (captcha, network exhausted); 3 detected-ban.

**REQ-F-034: `discord-scanner scan --guild <guild_id>` (scoped)**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Scans exactly the specified guild (one value, not repeatable); skips guild enumeration step.
  2. All other scan semantics identical to REQ-F-033.

**REQ-F-035: `discord-scanner daemon` command**
- Source: spec.md §3.9, §3.10, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Loop: wait until next scan window → scan → sleep `config.daemon.interval_hours + random.uniform(*config.daemon.jitter_hours)` → repeat.
  2. Scan start time picked randomly within `config.daemon.scan_start_window` (default `"02:00-06:00 UTC"`).
  3. SIGINT / SIGTERM triggers clean shutdown: complete current channel's dump + cursor commit, then exit 0.
- Edge cases:
  - scan exits 2 (captcha) → daemon logs ERROR and exits 2 (no auto-recovery on captcha — operator must rotate burner).
  - scan exits 3 (ban) → daemon exits 3.
  - scan exits 1 (config) on day 1 → daemon exits 1 (config is static; retry won't help).

**REQ-F-036: `discord-scanner status` command**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Prints cursor state per `(guild_id, channel_id)` from `state/cursor.sqlite`.
  2. For each row: `guild_id, channel_id, last_message_id, updated_at, days_since_update`.
  3. Zero HTTP calls (same as `--offline`).

**REQ-F-037: `discord-scanner store-token` command**
- Source: spec.md §3.9, seed-spec.md §6, security-model.md §1.1, SEC-P0-02
- Priority: Must
- Acceptance criteria:
  1. Prompts via `getpass.getpass()` (never echoed, never via Typer's echoing prompt).
  2. Stores in keyring with `service=config.auth.keyring_service`, `username=config.auth.keyring_username`.
  3. Refuses to store if the keyring backend is a plaintext fallback (SEC-P0-06).
  4. Does NOT log the token at any level; logs only `token_stored=true`.

**REQ-F-038: `discord-scanner version` command**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Prints semantic version from `__version__` + git SHA (build-embedded) + Python version.
  2. Exit 0 always.

**REQ-F-039: Global flag `--config PATH`**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Default: `./config.yaml` if present, else bundled `config.example.yaml`.
  2. `--config foo.yaml` overrides; `Path.resolve()` checked for no `..` or system-path escape (SEC-P0-23; security-model.md §2.4).

**REQ-F-040: Global flag `--verbose/-v`**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Raises log level from INFO to DEBUG for this invocation; does NOT override `config.run.log_level` persistently.

**REQ-F-041: Global flag `--dry-run`**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. Prints planned actions: `N invites × M guilds × K channels`, estimated HTTP calls, estimated runtime.
  2. Makes zero HTTP calls and zero gateway connections.
  3. Exit 0.

**REQ-F-042: Global flag `--offline`**
- Source: spec.md §3.9, seed-spec.md §6, acceptance criterion 10
- Priority: Must
- Acceptance criteria:
  1. State-inspection mode: reads `state/cursor.sqlite` and prints its contents per REQ-F-036.
  2. Makes zero HTTP calls AND zero WebSocket connections.

**REQ-F-043: Exit codes**
- Source: spec.md §3.9, seed-spec.md §6
- Priority: Must
- Acceptance criteria:
  1. `0` success; `1` user/config error; `2` runtime (network/captcha); `3` detected-ban.
  2. Detected-ban heuristic (spec.md Known Gaps): 401 on a previously-working endpoint (e.g. `/users/@me/guilds`) AND `config.yaml` mtime older than 24 hours → exit 3. Otherwise 401 → exit 2.

### 1.10 Error + observability features

**REQ-F-044: Captcha hard abort**
- Source: spec.md §3.6, seed-spec.md §2.6, SEC-P0-16
- Priority: Must
- Acceptance criteria:
  1. On 401 OR 403 with response body containing `captcha_key`, `captcha_sitekey`, OR `captcha_service` JSON key, scan aborts immediately with exit code 2.
  2. `meta.json:errors[]` gets `{stage: "fetch_messages", reason: "captcha", channel_id: ..., timestamp: ...}`.
  3. No further HTTP calls; gateway cleanly closed.
  4. Operator-actionable message printed to stderr naming the runbook in `docs/claude/development.md`.
  5. Captcha auto-solve is explicitly NOT implemented (security-model.md §8.3).

**REQ-F-045: `retention --purge-all` command (Phase 1)**
- Source: security-model.md §SEC-P1-02, §5.1 (GDPR right to erasure)
- Priority: Should (Phase 1)
- Acceptance criteria:
  1. `discord-scanner retention --purge-all --confirm` bulk-deletes `output/`, `state/cursor.sqlite`, `state/cookies-*.json`, and removes keyring entry.
  2. Requires `--confirm` flag; without it, prints a plan and exits 0.
- Notes: Phase 0 workaround is manual `rm -rf output/ state/` + `keyring erase ...`, documented in `docs/claude/development.md`.

---

## 2. Non-Functional Requirements

### 2.1 Runtime + stack

**REQ-NF-001: Python 3.12+**
- Source: spec.md §8.2, seed-spec.md §3, brainstorm.md §6
- Priority: Must
- Verification: `pyproject.toml` has `requires-python = ">=3.12"`; CI matrix runs 3.12 on Ubuntu + Windows.

**REQ-NF-002: Library allowlist (required)**
- Source: spec.md §8.3, seed-spec.md §3, brainstorm.md §6
- Priority: Must
- Verification: `pyproject.toml` pins `httpx[http2]>=0.27`, `websockets>=13`, `tenacity>=8.2`, `pydantic>=2.6`, `pydantic-settings>=2.2`, `keyring>=24.0`, `zstandard>=0.22`, `filelock`, `typer>=0.12`, `rich>=13.0`, `structlog>=24.0`, `pytest>=8`, `pytest-asyncio>=0.23`, `respx>=0.21`, `pytest-websocket` (or hand-rolled fixtures), `ruff>=0.4`, `mypy>=1.10`, `hatchling`. `sqlite3` from stdlib.

**REQ-NF-003: Forbidden libraries (merge-blocker CI grep)**
- Source: spec.md §8.3, seed-spec.md §3, §9, brainstorm.md §6
- Priority: Must
- Verification: CI grep guard rejects any `import requests | import aiohttp | import discord | import anthropic | from selenium | from playwright | import fcntl` anywhere in `src/`. Also `obsidian-*` package names.

**REQ-NF-004: Package path**
- Source: spec.md §8.2, seed-spec.md §8
- Priority: Must
- Verification: Code lives under `src/discord_scanner/`; entry point `discord-scanner = "discord_scanner.cli:app"` in `pyproject.toml`.

**REQ-NF-005: Cross-platform**
- Source: spec.md §9.5, seed-spec.md §3, brainstorm.md §2
- Priority: Must
- Verification: CI matrix `python-3.12 × {ubuntu-latest, windows-latest}` both green.

### 2.2 Anti-detection — full header set (one REQ-NF per header)

**REQ-NF-006: Authorization header (no Bearer prefix)**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: respx-based test asserts `Authorization: <token>` (no `Bearer `) present on every REST request.

**REQ-NF-007: User-Agent header (Chrome, config-driven)**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: UA matches `Mozilla/5.0 ({config.http.fake_os}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{config.http.user_agent_chrome_version} Safari/537.36`; asserted on every REST request.

**REQ-NF-008: Sec-Ch-Ua header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: Value `"Chromium";v="{VER}", "Google Chrome";v="{VER}", "Not?A_Brand";v="99"` where `VER` derives from `config.http.user_agent_chrome_version`.

**REQ-NF-009: Sec-Ch-Ua-Mobile header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `Sec-Ch-Ua-Mobile: ?0` on every REST request.

**REQ-NF-010: Sec-Ch-Ua-Platform header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: Value `"{config.http.fake_os_platform}"` (default `"Windows"`).

**REQ-NF-011: Sec-Fetch-Site header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `Sec-Fetch-Site: same-origin` on every REST request.

**REQ-NF-012: Sec-Fetch-Mode header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `Sec-Fetch-Mode: cors` on every REST request.

**REQ-NF-013: Sec-Fetch-Dest header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `Sec-Fetch-Dest: empty` on every REST request.

**REQ-NF-014: X-Super-Properties header**
- Source: spec.md §4.1, seed-spec.md §4.1, SEC-P0-09
- Priority: Must
- Verification: base64-encoded JSON decodes to object with keys `{os, browser, browser_version, os_version, device, browser_user_agent, system_locale, client_build_number, release_channel}`; all values derive from `config.http.*`.

**REQ-NF-015: X-Discord-Locale header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: Value `config.http.locale` (default `en-US`).

**REQ-NF-016: X-Discord-Timezone header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: Value `config.http.timezone` (default `Europe/Prague`).

**REQ-NF-017: X-Debug-Options header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `X-Debug-Options: logGatewayEvents` on every REST request.

**REQ-NF-018: Origin header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `Origin: https://discord.com`.

**REQ-NF-019: Referer header**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `Referer: https://discord.com/channels/@me`.

**REQ-NF-020: Accept / Accept-Encoding / Accept-Language headers**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `Accept: */*`, `Accept-Encoding: gzip, deflate, br`, `Accept-Language: en-US,en;q=0.9`.

**REQ-NF-021: Content-Type header (body requests only)**
- Source: spec.md §4.1, seed-spec.md §4.1
- Priority: Must
- Verification: `Content-Type: application/json` ONLY on requests with body (GETs do not set it — Stage 2 has no non-GET requests so this header is effectively absent; asserted by test).

### 2.3 Anti-detection — other

**REQ-NF-022: HTTP/2 mandatory**
- Source: spec.md §4.2, seed-spec.md §4.2, SEC-P0-11
- Priority: Must
- Verification: `httpx.AsyncClient(http2=True)` is the single construction site in `src/`; unit test asserts `client.http2 is True`.

**REQ-NF-023: Cookie jar persistence per burner**
- Source: spec.md §4.4, seed-spec.md §4.4, SEC-P0-12
- Priority: Must
- Verification: `httpx.AsyncClient(cookies=jar)` where jar loads/saves to `state/cookies-{config.auth.keyring_username}.json`. Test: two scans with different `keyring_username` → separate files.

**REQ-NF-024: MIME sniff on attachment download**
- Source: security-model.md §4.1, SEC-P0-19
- Priority: Must
- Verification: First 16 bytes validated against PNG `89 50 4E 47`, JPEG `FF D8 FF`, WebP `52 49 46 46 ... 57 45 42 50`, GIF `47 49 46 38`. Mismatch → discard bytes, record URL only, WARNING.

**REQ-NF-025: Streaming size cap**
- Source: security-model.md §4.1, SEC-P0-20
- Priority: Must
- Verification: Download aborts at `max_size_mb * 1.1` bytes; partial file deleted.

**REQ-NF-026: Filename sanitisation**
- Source: security-model.md §4.1, SEC-P0-21
- Priority: Must
- Verification: `pathlib.PurePosixPath(filename).name` → null-byte strip → replace `[<>:"/\\|?*\x00-\x1f]` with `_` → length cap 128 → prefix `{msg_id}_` → `os.path.realpath` within `output_root`.

**REQ-NF-027: Per-request jitter 1.5–4.0 s**
- Source: spec.md §4.5, seed-spec.md §4.5, SEC-P0-14
- Priority: Must
- Verification: `asyncio.sleep(random.uniform(*config.http.per_channel_delay_sec))` invoked before every REST call inside a channel. Integration test measures ≥1.5 s between consecutive mocked requests.

**REQ-NF-028: Burst pause 30–90 s between channels**
- Source: spec.md §4.5, seed-spec.md §4.5, SEC-P0-14
- Priority: Must
- Verification: See REQ-F-012.

### 2.4 Logging + redaction

**REQ-NF-029: Structured logging via structlog**
- Source: spec.md §9.3, §9.7, seed-spec.md §3
- Priority: Must
- Verification: Every REST request log record has `{stage, source, url_hash, cache_hit, http_status, duration_ms, attempt}`. Every gateway event: `{stage, event_type, timestamp}`. Per-guild summary log at end-of-scan with full counters.

**REQ-NF-030: Token redaction helper**
- Source: spec.md §8.6, seed-spec.md §3, security-model.md §1.1, SEC-P0-03
- Priority: Must
- Verification: `redact_token(t)` returns `f"{t[:6]}***{t[-4:]}"` for strings ≥10 chars, else `"***"`. Registered as structlog processor applied to every record.

**REQ-NF-031: Invite-code redaction helper**
- Source: spec.md §8.6, seed-spec.md §3, security-model.md §1.1
- Priority: Must
- Verification: `redact_invite_code(c)` returns `f"{c[:2]}***{c[-2:]}"`. CI grep merge-blocker: no `discord.gg/` or `discord.com/invite/` literal inside any `logger.*` / `structlog.*` call (SEC-P0-29).

### 2.5 Determinism + schema tolerance

**REQ-NF-032: Sort stability**
- Source: spec.md §8.8, seed-spec.md §3
- Priority: Must
- Verification: Messages within `messages.jsonl.zst` / `pinned.jsonl` / `threads.jsonl` ordered `(channel_id ASC, timestamp ASC, message_id ASC)`. JSON dict keys alphabetised. Test: two cold runs on unchanged cursor → byte-identical decompressed JSONL.

**REQ-NF-033: Idempotence**
- Source: spec.md §9.4, seed-spec.md §3, acceptance criterion 9
- Priority: Must
- Verification: Two consecutive mocked scans with unchanged cursor state produce byte-identical decompressed `messages.jsonl`.

**REQ-NF-034: Resumability**
- Source: spec.md §9.2, seed-spec.md §3
- Priority: Must
- Verification: Network drop mid-channel leaves cursor unchanged; re-run continues from the last-persisted cursor boundary; zero duplicate messages.

**REQ-NF-035: Pydantic schema tolerance**
- Source: spec.md §8.9, seed-spec.md §3, acceptance criterion 14
- Priority: Must
- Verification: Every model sets `model_config = ConfigDict(extra='allow')`. `ValidationError` on one message → WARNING + skip, run continues.

**REQ-NF-036: No NaN / Infinity in JSON**
- Source: spec.md §8.10
- Priority: Must
- Verification: JSON serialisation path raises on non-finite floats (json.dumps with default `allow_nan=False`).

### 2.6 Security (every SEC-P0 traces here)

**REQ-NF-037: File permissions 0o600 (0o700 dirs)**
- Source: security-model.md §3.2, SEC-P0-22
- Priority: Must
- Verification: `os.chmod(path, 0o600)` on every file in `state/` and `output/**` immediately after creation; dirs `0o700`. Best-effort on Windows (documented).

**REQ-NF-038: Retention prune at scan start**
- Source: spec.md §3.11, seed-spec.md §3, SEC-P0-23, SEC-P0-24
- Priority: Must
- Verification: `output/{guild_id}/{date}/` older than `config.retention.raw_dump_keep_days` (default 30) deleted at scan start. Symlinks refused; realpath stays within `output_root`.

**REQ-NF-039: Attachment retention**
- Source: spec.md §3.11, seed-spec.md §3
- Priority: Must
- Verification: `config.retention.attachment_keep_days` (default 14) prunes `attachments/` subdirs per guild.

**REQ-NF-040: URL allowlist (SSRF guard)**
- Source: spec.md §8.4, seed-spec.md §3, SEC-P0-17
- Priority: Must
- Verification: httpx `event_hooks["request"]` raises `SSRFViolation` on any netloc not in `{discord.com, cdn.discordapp.com, gateway.discord.gg, media.discordapp.net}`. Redirects out of allowlist also rejected.

**REQ-NF-041: TLS verify=True always**
- Source: spec.md §8.4, seed-spec.md §3, SEC-P0-18
- Priority: Must
- Verification: `httpx.AsyncClient(verify=True)` sole construction; CI grep merge-blocker on `verify=False` in `src/`.

**REQ-NF-042: SQL parameterisation**
- Source: spec.md §8.7, security-model.md §2.4
- Priority: Must
- Verification: Every SQLite query uses `?` placeholders; zero f-string or `%` interpolation. CI grep guard.

**REQ-NF-043: Token loading enumerated**
- Source: spec.md §8.6, seed-spec.md §3, security-model.md §1.1, SEC-P0-01
- Priority: Must
- Verification: `src/discord_scanner/session/auth.py::load_token()` enumerates exactly `keyring | env (DISCORD_TOKEN) | config.yaml.discord_token` in priority order. Test: unit-tests all three paths.

**REQ-NF-044: store-token uses getpass**
- Source: security-model.md §1.1, SEC-P0-02
- Priority: Must
- Verification: Unit test asserts `getpass.getpass` is invoked in `store-token` code path; Typer's default `prompt=True` is NOT used.

**REQ-NF-045: Token never written to artefacts**
- Source: spec.md §8.6, seed-spec.md §3, security-model.md §1.1, SEC-P0-05
- Priority: Must
- Verification: Integration test runs a mocked scan with a test token, greps every file in `output/`, `state/`, and logs for the token value, asserts zero matches.

**REQ-NF-046: Keyring plaintext-fallback refused**
- Source: security-model.md §4.1, SEC-P0-06
- Priority: Must
- Verification: At load, if `keyring.get_keyring()` class name contains `Plaintext` or module is `keyrings.alt`, raise RuntimeError with remediation message.

**REQ-NF-047: Chrome-UA staleness check at load**
- Source: security-model.md §SEC-P0-08
- Priority: Must
- Verification: If `config.http.user_agent_chrome_version` is older than 8 weeks (hardcoded ship date vs. today), scanner exits 1 at load.

**REQ-NF-048: Gateway fingerprint matches REST fingerprint**
- Source: security-model.md §SEC-P0-25
- Priority: Must
- Verification: Gateway IDENTIFY `properties` blob and REST `X-Super-Properties` decoded blob are byte-for-byte identical; unit test compares.

**REQ-NF-049: Plausible client_build_number**
- Source: security-model.md §SEC-P0-26
- Priority: Must
- Verification: `config.http.client_build_number` ≥ 300000 (numeric). Unit test.

**REQ-NF-050: Gateway concurrent-instance guard**
- Source: security-model.md §SEC-P0-13
- Priority: Must
- Verification: `filelock.FileLock(state/gateway-{burner}.lock)` acquired with `timeout=0` before WS connect; second invocation exits 1.

**REQ-NF-051: No sync time.sleep in async**
- Source: spec.md §9.1, §9.6, seed-spec.md §9, SEC-P0-29
- Priority: Must
- Verification: CI grep guard: no `time.sleep(` inside any `async def` block.

**REQ-NF-052: No print() in src (rich in cli.py only)**
- Source: spec.md §8.10
- Priority: Must
- Verification: CI grep guard. `rich.console.Console` allowed only in `src/discord_scanner/cli.py`.

**REQ-NF-053: No bare except**
- Source: spec.md §8.10
- Priority: Must
- Verification: ruff rule `E722` / `BLE001` enforced.

**REQ-NF-054: No fcntl in src**
- Source: spec.md §9.5, SEC-P0-29
- Priority: Must
- Verification: CI grep guard.

**REQ-NF-055: No raw discord.gg/ in logs**
- Source: spec.md §9.6, seed-spec.md §9, SEC-P0-29
- Priority: Must
- Verification: CI grep guard: no `discord.gg/` or `discord.com/invite/` literal inside any `logger.*` or `structlog.*` call.

**REQ-NF-056: No raw Discord token pattern anywhere in src or fixtures**
- Source: spec.md §9.6, SEC-P0-04
- Priority: Must
- Verification: CI grep guard on regex `[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}` in `src/` and `tests/fixtures/`.

### 2.7 Rate limiting + retries

**REQ-NF-057: Per-host token bucket**
- Source: spec.md §3.6, seed-spec.md §2.6, SEC-P0-14
- Priority: Must
- Verification: `config.http.per_host_rate_per_sec["discord.com/api"]=2`, `["cdn.discordapp.com"]=1` (defaults). Integration test: measured rate does not exceed bucket + 10%.

**REQ-NF-058: 429 Retry-After honouring**
- Source: spec.md §3.6, seed-spec.md §2.6, SEC-P0-15
- Priority: Must
- Verification: `Retry-After` header respected; after `MAX_429_RETRIES=3` consecutive 429s same endpoint, channel aborted (log WARNING, move to next, append to `meta.json:channels_skipped[]`).

**REQ-NF-059: 5xx tenacity retry**
- Source: spec.md §3.6, seed-spec.md §2.6
- Priority: Must
- Verification: `tenacity` retries up to `config.retry.attempts` (default 5) on `config.retry.retry_on_status` (default `[429, 500, 502, 503, 504]`) with exponential backoff `[config.retry.backoff_initial_sec, config.retry.backoff_max_sec]`.

**REQ-NF-060: 403 skip, no retry**
- Source: spec.md §3.6, security-model.md §2.2
- Priority: Must
- Verification: Plain 403 (no captcha indicators) → log WARNING, skip channel, no retry. Appends to `meta.json:channels_skipped[]` with `reason: "403_no_permission"`.

**REQ-NF-061: 401 → detected-ban heuristic**
- Source: spec.md Known Gaps, seed-spec.md §6 (exit code 3), security-model.md §1.3
- Priority: Must
- Verification: 401 on a previously-working endpoint (e.g. `/users/@me/guilds`) → compare `config.yaml` mtime to now; if older than 24 h, exit 3; else exit 2.

### 2.8 Quality gates

**REQ-NF-062: ruff check clean**
- Source: spec.md §9.6, seed-spec.md §9
- Priority: Must
- Verification: CI runs `ruff check src tests` → 0 errors; `ruff format --check src tests` clean.

**REQ-NF-063: mypy --strict clean**
- Source: spec.md §9.6, seed-spec.md §9
- Priority: Must
- Verification: CI runs `mypy --strict src` → 0 errors.

**REQ-NF-064: pytest coverage ≥ 70%**
- Source: spec.md §9.6, seed-spec.md §9
- Priority: Must
- Verification: CI runs `pytest -q --cov=src/discord_scanner --cov-fail-under=70` → green.

**REQ-NF-065: bandit severity-medium+ clean**
- Source: security-model.md §SEC-P0-31
- Priority: Must
- Verification: CI runs `bandit -r src/ --severity-level medium` → green. ruff rule-set includes `S` (flake8-bandit).

**REQ-NF-066: pip-audit / Dependabot clean**
- Source: security-model.md §SEC-P0-30
- Priority: Must
- Verification: CI runs `pip-audit`; Dependabot enabled; no known critical CVE at launch.

**REQ-NF-067: Lock file committed + hash-require in CI**
- Source: security-model.md §SEC-P0-30
- Priority: Must
- Verification: `uv.lock` or `poetry.lock` committed; CI uses `pip install --require-hashes`.

**REQ-NF-068: .env / .env.example hygiene**
- Source: security-model.md §SEC-P0-32
- Priority: Must
- Verification: `.env.example` committed with placeholders; `.env` gitignored.

**REQ-NF-069: output/ gitignored**
- Source: security-model.md §SEC-P0-28
- Priority: Must
- Verification: `.gitignore` contents asserted in CI.

**REQ-NF-070: Third-party PII docs section present**
- Source: security-model.md §SEC-P0-27
- Priority: Must
- Verification: `docs/claude/design-decisions.md` contains section "Third-party PII handling and operator obligations" covering (a)–(e) from SEC-P0-27. CI grep.

### 2.9 Performance

**REQ-NF-071: Mid-sized guild E2E ≤ 20 min**
- Source: spec.md §9.1, brainstorm.md §7
- Priority: Should
- Verification: On mocked 10 000-message × 10-channel guild, end-to-end completes in 10–20 min (accounting for jitter + burst pauses).

**REQ-NF-072: Timeout per request**
- Source: spec.md config §5
- Priority: Must
- Verification: `httpx.AsyncClient(timeout=config.http.timeout_sec)` (default 30 s).

### 2.10 Availability / RTO / RPO / backup

For a single-user local CLI, classic SLAs do not apply. Documented explicitly:

- **Availability target**: none (single-user, local; "availability" = "the operator is at their workstation").
- **RTO**: N/A — on crash, operator re-runs; resumability (REQ-NF-034) is the practical RTO.
- **RPO**: zero data loss on successful channel dump; up-to-one-channel of in-flight messages on crash (cursor not advanced).
- **Backup**: operator's responsibility (workstation backup via Time Machine / Windows Backup / rsync). Tool does not implement backup.
- **Status page / notification of downtime**: N/A.

### 2.11 Data retention — summary

- User account data: N/A (no user accounts).
- Dumps: 30 days default (`config.retention.raw_dump_keep_days`).
- Attachments: 14 days default (`config.retention.attachment_keep_days`).
- Cursor state: indefinite (until burner rotation).
- Invite-resolution cache: 7 days (REQ-F-005).
- Logs: operator discretion (stdout or file if redirected).
- Secrets: indefinite in keyring until `keyring erase` or `retention --purge-all` (REQ-F-045).

---

## 3. Integration Requirements

### 3.1 Boundary with civit-hf-scanner (Stage 1 → Stage 2)

**REQ-INT-001: Read `invites.enriched.json` on disk**
- Source: spec.md §3.1, brainstorm.md §3, seed-spec.md §2.1
- Priority: Must
- Acceptance criteria:
  1. Path: `config.discovery.invites_input` (default `../civit-hf-scanner/output/latest/invites.enriched.json`).
  2. Read-only. No write-back to Stage 1's tree.
  3. Schema (pydantic): each record has `{invite_code, score_pct, intent, confidence, ...}`; unknown extras tolerated (`extra='allow'`).
- Failure behavior: file missing → exit 1 with actionable error. Schema violation on whole file → exit 1. Schema violation on one record → skip with WARNING.
- Fallback: operator can add codes via `config.discovery.manual_invites` (REQ-F-003).
- Rate limits: N/A (local file read).
- Cost: 0.
- Compliance: none; this is an internal pipeline boundary.

**REQ-INT-002: No runtime coupling to civit-hf-scanner**
- Source: brainstorm.md §4, §9, spec.md §13, ROADMAP context
- Priority: Must
- Acceptance criteria:
  1. Zero imports of any `civit_hf_scanner` Python module. CI grep guard.
  2. Zero HTTP calls to civit-hf-scanner's internal artefacts. The only interface is the JSON file.
  3. Stage 1 + Stage 2 can be versioned independently; breaking changes to the JSON schema require a coordinated version bump on both sides.

**REQ-INT-003: Schema version of `invites.enriched.json`**
- Source: seed-spec.md §2.1 (implicit), Known Gaps (Stage 1.5 not yet shipped)
- Priority: Must
- Acceptance criteria:
  1. Scanner reads top-level `schema_version` field on `invites.enriched.json`; if absent, assume v1.
  2. Incompatible schema versions (future major bump) → exit 1 with "upgrade required" error.

### 3.2 Boundary with Discord (outbound)

**REQ-INT-004: Discord REST API v10 only**
- Source: spec.md §3.1..§3.3, seed-spec.md §2
- Priority: Must
- Acceptance criteria:
  1. Base URL `https://discord.com/api/v10/`. Endpoint set enumerated in security-model.md §4.3 and limited to: `/invites/{code}`, `/users/@me/guilds`, `/guilds/{id}/channels`, `/guilds/{id}/roles`, `/channels/{id}/messages`, `/channels/{id}/pins`, `/channels/{id}/threads/public_archived_threads`, `/guilds/{id}/threads/active`.
  2. No other Discord endpoint is called. CI grep guard.

**REQ-INT-005: Discord Gateway v10 only**
- Source: spec.md §3.5, seed-spec.md §2.5
- Priority: Must
- Acceptance criteria:
  1. URL `wss://gateway.discord.gg/?v=10&encoding=json`.
  2. Opcodes used: 1 (HEARTBEAT), 2 (IDENTIFY), 3 (PRESENCE UPDATE), 6 (RESUME). Opcode 11 (HEARTBEAT ACK) + 10 (HELLO) + 0 (with `t=READY`) received.
  3. All other opcodes NOT sent; other opcodes received are discarded.

**REQ-INT-006: Discord CDN — separate host**
- Source: spec.md §3.4, seed-spec.md §2.4
- Priority: Must
- Acceptance criteria:
  1. Base URLs `https://cdn.discordapp.com` + `https://media.discordapp.net`.
  2. Separate per-host token bucket.
  3. No Authorization header sent (CDN URLs are pre-signed).

### 3.3 Boundary with Stage 3 (discord-curator, future separate repo)

**REQ-INT-007: Contract surface = `output/{guild_id}/{YYYY-MM-DD}/`**
- Source: brainstorm.md §4, §5, §8; seed-spec.md §2.8; spec.md §1.2, §3.8
- Priority: Must
- Acceptance criteria:
  1. Only these files are Stage 2 → Stage 3 contract: `messages.jsonl.zst`, `pinned.jsonl`, `threads.jsonl`, `attachments/*`, `meta.json`, `prior.txt`.
  2. Stage 3 MUST NOT reach into `state/`, `cache/`, or Stage 2 internals.
  3. No code sharing beyond plain JSON on disk.

**REQ-INT-008: Schema versioning**
- Source: brainstorm.md §8 (2026-04-23 decision), seed-spec.md §7
- Priority: Must
- Acceptance criteria:
  1. Every output record has `schema_version` field (currently `1`).
  2. Breaking schema changes require a new major version bump + migration note in `docs/claude/design-decisions.md`.

**REQ-INT-009: No runtime coupling to Stage 3**
- Source: brainstorm.md §5, §8, spec.md §12
- Priority: Must
- Acceptance criteria:
  1. Zero imports of any `discord_curator` or Stage 3 module. CI grep guard (until Stage 3 exists, just asserts no `obsidian`, `anthropic`, `discord_curator`).
  2. Zero HTTP calls between Stage 2 and Stage 3.
  3. Stage 2's retention prune operates solely within `output/` — Stage 3 is responsible for snapshotting before it consumes (Known Gap in spec.md).

### 3.4 Boundary with OS keyring

**REQ-INT-010: Keyring backend per OS**
- Source: spec.md §8.6, seed-spec.md §3, security-model.md §3.2
- Priority: Must
- Acceptance criteria:
  1. Windows: DPAPI-backed (`keyring.backends.Windows`).
  2. macOS: Keychain Services.
  3. Linux: Secret Service via `libsecret`; if headless Linux lacks a DE keyring, keyring will fallback — this fallback MUST be detected and refused (REQ-NF-046).

**REQ-INT-011: Keyring service + username keys**
- Source: spec.md §5 config, seed-spec.md §5
- Priority: Must
- Acceptance criteria:
  1. `service=config.auth.keyring_service` (default `discord-scanner`).
  2. `username=config.auth.keyring_username` (default `burner-1`).
  3. Token round-trip via `discord-scanner store-token` → `discord-scanner list-guilds` proves the pairing (acceptance criterion 4 in spec.md §11).

### 3.5 Rejected integrations (documented to preempt re-litigation)

Per security-model.md §8.3: no captcha-solve service, no Claude/Anthropic, no Obsidian, no LLM API, no telemetry endpoint, no bug tracker integration.

---

## 4. Operational Requirements

### 4.1 Logging schema

**REQ-OPS-001: Per-HTTP-request log schema**
- Source: spec.md §9.3, seed-spec.md §3
- Priority: Must
- Acceptance criteria:
  1. Every REST request emits one structured log record with keys: `timestamp` (ISO 8601 UTC), `level`, `stage` (`resolve|list_guilds|scan_messages|scan_pinned|scan_threads|scan_attachments`), `source` (`discord_rest|cdn`), `url_hash` (SHA256 of URL), `http_status`, `duration_ms`, `attempt`.
  2. Token NEVER appears. Raw URL NEVER appears (hash only). Full invite code NEVER appears.

**REQ-OPS-002: Per-gateway-event log schema**
- Source: spec.md §9.3, seed-spec.md §3
- Priority: Must
- Acceptance criteria:
  1. Every gateway event emits a record with `{stage: "gateway", event_type, timestamp}`.
  2. `event_type` ∈ `{connect, identify, hello, ready, heartbeat, heartbeat_ack, presence_update, disconnect, resume, resume_ok, resume_fail, close}`.
  3. `session_id` / `sequence` appear ONLY at DEBUG level and are hashed (not raw).

**REQ-OPS-003: Per-scan-summary log record**
- Source: spec.md §9.3, seed-spec.md §3
- Priority: Must
- Acceptance criteria:
  1. At end-of-scan per guild, emit INFO record with all counters matching `meta.json` (REQ-F-028): `{stage: "scan_summary", guild_id, message_count, pinned_count, thread_count, attachment_count, attachments_downloaded, attachments_skipped, rate_limit_hits, gateway_disconnects, gateway_resumes, duration_sec, errors_count}`.

**REQ-OPS-004: Log retention**
- Source: security-model.md §3.1
- Priority: Should
- Acceptance criteria:
  1. If `config.run.log_file` is set (not default), log file chmod `0o600`.
  2. Log retention is operator-managed (manual rotate / `logrotate`); tool does not auto-rotate.

### 4.2 Metrics

Traditional SaaS metrics (DAU / churn / error rate alerts) are N/A. Operational metrics that MUST be tracked in `meta.json` per REQ-F-028:

- `message_count`, `pinned_count`, `thread_count`, `attachment_count`, `attachments_downloaded`, `attachments_skipped[]`, `rate_limit_hits`, `gateway_disconnects`, `gateway_resumes`, `errors[]`, `channels_skipped[]`.

**REQ-OPS-005: Rate-limit-hit counter**
- Source: spec.md §9.3, seed-spec.md §7.2
- Priority: Must
- Verification: Every 429 response increments `meta.json:rate_limit_hits`. Operator can inspect to detect emerging throttle pressure.

**REQ-OPS-006: Gateway-disconnect counter**
- Source: spec.md §9.3, seed-spec.md §7.2
- Priority: Must
- Verification: Every gateway close / reconnect cycle increments `meta.json:gateway_disconnects`; successful resume increments `gateway_resumes`.

### 4.3 Alerting

For a single-user workstation CLI, alerts are operator-observed rather than paged:

**REQ-OPS-007: Captcha alert to stderr**
- Source: spec.md §3.6, seed-spec.md §2.6
- Priority: Must
- Verification: On captcha detection (REQ-F-044), stderr gets a BOLD human-readable message with runbook path.

**REQ-OPS-008: Ban-heuristic alert**
- Source: seed-spec.md §6, security-model.md §1.3
- Priority: Must
- Verification: Exit 3 triggers stderr message "possible burner ban; see docs/claude/development.md burner-rotation runbook".

**REQ-OPS-009: Stale-UA alert**
- Source: security-model.md §SEC-P0-08, §SEC-P1-01
- Priority: Must
- Verification: On `config.http.user_agent_chrome_version` > 8 weeks old, exit 1 at load with stderr message "Chrome UA stale; update config.http.user_agent_chrome_version".

### 4.4 Operator runbooks (all lived in `docs/claude/development.md`)

**REQ-OPS-010: Burner account provisioning runbook**
- Source: brainstorm.md §2, §10; security-model.md §1.2
- Priority: Must
- Acceptance criteria:
  1. Documents: disposable email service, disposable VOIP number, VPN recommendation, clean browser profile, manually joining target guilds in a browser, never authoring content, weekly rotation optional.
  2. Explicit: "do NOT use real email or phone".

**REQ-OPS-011: Keyring provisioning per-OS runbook**
- Source: security-model.md §3.2, SEC-P0-06
- Priority: Must
- Acceptance criteria:
  1. Windows: run `discord-scanner store-token` → stored via Credential Manager.
  2. macOS: same command → Keychain Services.
  3. Linux: requires running DE keyring OR headless `gnome-keyring-daemon --unlock`; document failure mode + remediation.

**REQ-OPS-012: Daemon scheduling runbook**
- Source: spec.md §3.10, brainstorm.md §7
- Priority: Should
- Acceptance criteria:
  1. Windows: Task Scheduler XML snippet example.
  2. macOS/Linux: cron example + systemd-user unit example.
  3. All schedule in UTC; operator's local time is logged alongside in `meta.json.scan_started_at` (REQ-OPS-017).

**REQ-OPS-013: Retention auto-purge runbook**
- Source: spec.md §3.11, security-model.md §5.1 (GDPR erasure)
- Priority: Must
- Acceptance criteria:
  1. Default: 30-day dumps + 14-day attachments pruned at scan start.
  2. Manual purge: `retention --purge-all` (Phase 1) or `rm -rf output/ state/` + `keyring erase ...` (Phase 0).

**REQ-OPS-014: Log monitoring runbook**
- Source: spec.md §9.3, security-model.md §3.1
- Priority: Should
- Acceptance criteria:
  1. Document how to redirect structlog JSON output to a file.
  2. Document grep recipes for `level=ERROR`, `stage=gateway`, `rate_limit_hits>0`, `reason=captcha`.
  3. Document log-rotation with standard OS tools.

**REQ-OPS-015: Ban / captcha response runbook**
- Source: spec.md §10.1, §10.3; security-model.md §8.1
- Priority: Must
- Acceptance criteria:
  1. On captcha: rotate to fresh burner (new account, new email, new VOIP, new VPN IP).
  2. On suspected ban (exit 3): same procedure; also recommend a cooling-off period before scanning again.

**REQ-OPS-016: Token-rotation pool runbook**
- Source: seed-spec.md §5 config `auth.rotate_every`, Known Gaps
- Priority: Should
- Acceptance criteria:
  1. `config.auth.token_pool` semantics documented: `per_request | per_server | per_scan`.
  2. Gateway-per-burner constraint documented (one gateway session per token at a time — multi-token pools spawn gateway-per-token serially).
  3. Default is `pool=null`, `rotate_every=per_server`, `token_pool_size=1`.

**REQ-OPS-017: Timezone handling**
- Source: Gap-finding heuristic (timezone)
- Priority: Must
- Acceptance criteria:
  1. All internal storage (`meta.json`, cursor timestamps, logs) is UTC ISO 8601.
  2. `config.daemon.scan_start_window` is UTC by convention; operator's local time is reported in log prefix (structlog processor) for operator-readability.
  3. DST transitions: since everything is UTC, no DST drift inside the tool. The Task Scheduler / cron invocation may drift with the operator's local DST — documented.

**REQ-OPS-018: Fixture-refresh cadence**
- Source: security-model.md §SEC-P1-01
- Priority: Should (Phase 1)
- Acceptance criteria:
  1. Monthly CI job (Phase 1) probes current Chrome stable from a deterministic source; fails CI if `config.http.user_agent_chrome_version` is > 8 weeks behind.
  2. Documented source endpoint (Known Gap: TBD; recommend `chromiumdash.appspot.com/fetch_releases?channel=Stable`).

### 4.5 Distributed tracing / APM

N/A — single-process CLI, no cross-service tracing. structlog's `request_id`-style correlation is via per-request `url_hash` (REQ-OPS-001).

### 4.6 Audit trail

**REQ-OPS-019: Scan-start / scan-end audit events**
- Source: security-model.md §1.4
- Priority: Must
- Verification: INFO log entries at scan start (with config hash, burner alias, NOT token) and scan end (with counters).

**REQ-OPS-020: Token-load audit event**
- Source: security-model.md §1.4
- Priority: Must
- Verification: INFO log on every scan start: `{event: "token_loaded", source: "keyring|env|config", token_redacted: "abc123***wxyz"}`.

**REQ-OPS-021: Retention prune audit event**
- Source: security-model.md §1.4
- Priority: Must
- Verification: INFO log on prune: `{event: "retention_prune", deleted_paths: [...], file_count: N, bytes_freed: N}`.

---

## 5. Accessibility Requirements

**N/A.** This is a headless CLI with no UI surface. Accessibility requirements (WCAG, keyboard navigation, screen reader, colour contrast) apply to graphical/web interfaces. Operator's terminal of choice handles its own accessibility — out of scope for this tool. `rich` console output uses colour but has a fallback via `--no-color` / `NO_COLOR` env var (rich's built-in behaviour).

---

## 6. Internationalisation (i18n) Requirements

**N/A.** Single-user English-only tool; operator controls the deployment. Discord user content is stored verbatim (unicode-safe JSON). No user-facing strings in multiple languages; all log messages are English. If operator ever shares the tool, full i18n would be a 2–4 week refactor — accepted risk.

---

## 7. Browser and Device Support Matrix

**N/A.** CLI tool, no browser surface. Device support: Python 3.12+ on Windows 11 primary, macOS + Linux acceptable (REQ-NF-005). CI validates `python-3.12 × {ubuntu-latest, windows-latest}`.

---

## 8. Requirements Traceability Matrix

| Req ID | Source | Section | Verification method | Priority |
|---|---|---|---|---|
| REQ-F-001 | spec.md §3.1, seed-spec.md §2.1 | 1.1 | pydantic unit test + integration | Must |
| REQ-F-002 | spec.md §3.1, seed-spec.md §2.1 | 1.1 | unit test with fixture list | Must |
| REQ-F-003 | spec.md §3.1 | 1.1 | unit test | Must |
| REQ-F-004 | spec.md §3.1 | 1.1 | respx integration | Must |
| REQ-F-005 | seed-spec.md §2.1 | 1.1 | sqlite + time-mock unit test | Must |
| REQ-F-006 | spec.md §3.2 | 1.2 | respx integration | Must |
| REQ-F-007 | spec.md §3.2 | 1.2 | respx integration | Must |
| REQ-F-008 | spec.md §3.2 | 1.2 | respx + CI grep (no member endpoint) | Must |
| REQ-F-009 | spec.md §3.3 | 1.3 | respx pagination test | Must |
| REQ-F-010 | spec.md §3.3 | 1.3 | respx + file assert | Must |
| REQ-F-011 | spec.md §3.3 | 1.3 | respx + file assert | Must |
| REQ-F-012 | spec.md §3.3, §4.5 | 1.3 | timing assertion test | Must |
| REQ-F-013 | spec.md §3.4 | 1.4 | respx + filesystem assert | Must |
| REQ-F-014 | spec.md §3.4 | 1.4 | respx + meta.json assert | Must |
| REQ-F-015 | spec.md §3.4, §4.1 | 1.4 | respx header assert | Must |
| REQ-F-016..F-022 | spec.md §3.5, seed-spec.md §2.5 | 1.5 | websockets mock test | Must |
| REQ-F-023 | spec.md §3.7 | 1.6 | sqlite assert post-scan | Must |
| REQ-F-024 | spec.md §3.7, security-model.md §2.4 | 1.6 | filelock concurrency test | Must |
| REQ-F-025..F-030 | spec.md §3.8, seed-spec.md §2.8 | 1.7 | file-existence + schema test | Must |
| REQ-F-031..F-038 | spec.md §3.9, seed-spec.md §6 | 1.9 | typer CliRunner test per command | Must |
| REQ-F-039..F-042 | spec.md §3.9 | 1.9 | typer CliRunner flag test | Must |
| REQ-F-043 | spec.md §3.9, seed-spec.md §6 | 1.9 | exit-code table test | Must |
| REQ-F-044 | spec.md §3.6, SEC-P0-16 | 1.10 | respx captcha body test | Must |
| REQ-F-045 | security-model.md §SEC-P1-02 | 1.10 | typer CliRunner test | Should (P1) |
| REQ-NF-001 | spec.md §8.2 | 2.1 | pyproject.toml + CI matrix | Must |
| REQ-NF-002 | spec.md §8.3 | 2.1 | pyproject.toml inspection | Must |
| REQ-NF-003 | spec.md §8.3, §9.6 | 2.1 | CI grep guard | Must |
| REQ-NF-004 | spec.md §8.2, seed-spec.md §8 | 2.1 | path inspection | Must |
| REQ-NF-005 | spec.md §9.5 | 2.1 | CI matrix | Must |
| REQ-NF-006..NF-021 | spec.md §4.1, seed-spec.md §4.1 | 2.2 | respx header assert per-header | Must |
| REQ-NF-022 | spec.md §4.2, SEC-P0-11 | 2.3 | unit test client.http2 | Must |
| REQ-NF-023 | spec.md §4.4, SEC-P0-12 | 2.3 | per-burner filesystem test | Must |
| REQ-NF-024 | SEC-P0-19 | 2.3 | MIME-sniff unit test | Must |
| REQ-NF-025 | SEC-P0-20 | 2.3 | streaming size-cap test | Must |
| REQ-NF-026 | SEC-P0-21 | 2.3 | filename sanitisation test | Must |
| REQ-NF-027 | spec.md §4.5, SEC-P0-14 | 2.3 | timing assertion test | Must |
| REQ-NF-028 | spec.md §4.5 | 2.3 | timing assertion test | Must |
| REQ-NF-029 | spec.md §9.3 | 2.4 | structured-log schema test | Must |
| REQ-NF-030 | spec.md §8.6, SEC-P0-03 | 2.4 | unit test + processor registration | Must |
| REQ-NF-031 | spec.md §8.6, SEC-P0-29 | 2.4 | unit test + CI grep | Must |
| REQ-NF-032 | spec.md §8.8 | 2.5 | sort-stability integration test | Must |
| REQ-NF-033 | spec.md §9.4 | 2.5 | byte-identical-decompressed test | Must |
| REQ-NF-034 | spec.md §9.2 | 2.5 | network-drop simulation test | Must |
| REQ-NF-035 | spec.md §8.9 | 2.5 | pydantic extra-field test | Must |
| REQ-NF-036 | spec.md §8.10 | 2.5 | JSON allow_nan=False test | Must |
| REQ-NF-037 | SEC-P0-22 | 2.6 | chmod mode-bits test | Must |
| REQ-NF-038 | SEC-P0-23, SEC-P0-24 | 2.6 | symlink-escape test | Must |
| REQ-NF-039 | spec.md §3.11 | 2.6 | retention unit test | Must |
| REQ-NF-040 | SEC-P0-17 | 2.6 | respx SSRF + redirect test | Must |
| REQ-NF-041 | SEC-P0-18 | 2.6 | unit test + CI grep | Must |
| REQ-NF-042 | security-model.md §2.4 | 2.6 | CI grep + unit inspection | Must |
| REQ-NF-043 | SEC-P0-01 | 2.6 | three-path unit test | Must |
| REQ-NF-044 | SEC-P0-02 | 2.6 | CliRunner + getpass-mock test | Must |
| REQ-NF-045 | SEC-P0-05 | 2.6 | integration-grep test | Must |
| REQ-NF-046 | SEC-P0-06 | 2.6 | monkeypatch get_keyring test | Must |
| REQ-NF-047 | SEC-P0-08 | 2.6 | time-mock + stale UA test | Must |
| REQ-NF-048 | SEC-P0-25 | 2.6 | REST vs gateway fingerprint diff test | Must |
| REQ-NF-049 | SEC-P0-26 | 2.6 | client_build_number range test | Must |
| REQ-NF-050 | SEC-P0-13 | 2.6 | filelock concurrency test | Must |
| REQ-NF-051 | SEC-P0-29, spec.md §9.1 | 2.6 | CI grep guard | Must |
| REQ-NF-052 | spec.md §8.10 | 2.6 | CI grep guard | Must |
| REQ-NF-053 | spec.md §8.10 | 2.6 | ruff rule | Must |
| REQ-NF-054 | SEC-P0-29 | 2.6 | CI grep guard | Must |
| REQ-NF-055 | SEC-P0-29, spec.md §9.6 | 2.6 | CI grep guard | Must |
| REQ-NF-056 | SEC-P0-04 | 2.6 | CI grep guard | Must |
| REQ-NF-057 | SEC-P0-14 | 2.7 | token-bucket timing test | Must |
| REQ-NF-058 | SEC-P0-15 | 2.7 | respx 429 sequence test | Must |
| REQ-NF-059 | spec.md §3.6 | 2.7 | respx 5xx + tenacity test | Must |
| REQ-NF-060 | security-model.md §2.2 | 2.7 | respx 403 no-retry test | Must |
| REQ-NF-061 | Known Gaps, security-model.md §1.3 | 2.7 | 401-heuristic unit test | Must |
| REQ-NF-062 | spec.md §9.6 | 2.8 | CI job | Must |
| REQ-NF-063 | spec.md §9.6 | 2.8 | CI job | Must |
| REQ-NF-064 | spec.md §9.6 | 2.8 | CI job | Must |
| REQ-NF-065 | SEC-P0-31 | 2.8 | CI bandit job | Must |
| REQ-NF-066 | SEC-P0-30 | 2.8 | CI pip-audit job | Must |
| REQ-NF-067 | SEC-P0-30 | 2.8 | CI `--require-hashes` | Must |
| REQ-NF-068 | SEC-P0-32 | 2.8 | gitignore + CI assert | Must |
| REQ-NF-069 | SEC-P0-28 | 2.8 | gitignore assert | Must |
| REQ-NF-070 | SEC-P0-27 | 2.8 | docs CI grep | Must |
| REQ-NF-071 | spec.md §9.1 | 2.9 | integration timing test | Should |
| REQ-NF-072 | spec.md §5 config | 2.9 | httpx.timeout assertion | Must |
| REQ-INT-001 | spec.md §3.1 | 3.1 | fixture-file integration test | Must |
| REQ-INT-002 | brainstorm.md §4, §9 | 3.1 | CI grep guard | Must |
| REQ-INT-003 | seed-spec.md §2.1, Known Gaps | 3.1 | schema-version unit test | Must |
| REQ-INT-004 | spec.md §3.1..3.3, security-model.md §4.3 | 3.2 | CI grep on endpoint set | Must |
| REQ-INT-005 | spec.md §3.5 | 3.2 | gateway mock opcode test | Must |
| REQ-INT-006 | spec.md §3.4 | 3.2 | CDN base-URL unit test | Must |
| REQ-INT-007 | brainstorm.md §4, seed-spec.md §2.8 | 3.3 | output-file inventory test | Must |
| REQ-INT-008 | brainstorm.md §8, seed-spec.md §7 | 3.3 | schema_version assert per output | Must |
| REQ-INT-009 | brainstorm.md §5, §8 | 3.3 | CI grep guard | Must |
| REQ-INT-010 | security-model.md §3.2 | 3.4 | OS-specific CI smoke | Must |
| REQ-INT-011 | spec.md §5 config | 3.4 | round-trip integration test | Must |
| REQ-OPS-001..OPS-003 | spec.md §9.3 | 4.1 | structured-log schema test | Must |
| REQ-OPS-004 | security-model.md §3.1 | 4.1 | chmod assertion | Should |
| REQ-OPS-005..OPS-006 | spec.md §9.3, seed-spec.md §7.2 | 4.2 | meta.json counter test | Must |
| REQ-OPS-007..OPS-009 | spec.md §3.6, Known Gaps, SEC-P0-08 | 4.3 | stderr capture tests | Must |
| REQ-OPS-010..OPS-017 | brainstorm.md §2, §10, spec.md §5 config, security-model.md §1.2, §3.2 | 4.4 | docs CI grep for required runbook section titles | Must/Should |
| REQ-OPS-018 | SEC-P0-08, SEC-P1-01 | 4.4 | Phase-1 CI cron job | Should (P1) |
| REQ-OPS-019..OPS-021 | security-model.md §1.4 | 4.6 | log-schema + integration test | Must |

---

## 9. Security REQs → SEC-P0 cross-reference

Every Phase 0 blocking item from `security-model.md §6 (SEC-P0-01..32)` appears as a REQ-NF (or REQ-OPS where operationally-scoped). Mapping:

| SEC-P0-## | Mapped REQ | Title |
|---|---|---|
| SEC-P0-01 | REQ-NF-043 | Token loading enumerated (keyring\|env\|config) |
| SEC-P0-02 | REQ-NF-044 | store-token uses getpass |
| SEC-P0-03 | REQ-NF-030 | Token redaction helper + structlog processor |
| SEC-P0-04 | REQ-NF-056 | No raw Discord token pattern (CI grep) |
| SEC-P0-05 | REQ-NF-045 | Token never in artefacts |
| SEC-P0-06 | REQ-NF-046 | Keyring plaintext-fallback refused |
| SEC-P0-07 | REQ-NF-006..021 | Full Discord REST header set (one REQ per header) |
| SEC-P0-08 | REQ-NF-047, REQ-OPS-009 | Chrome-UA staleness check |
| SEC-P0-09 | REQ-NF-014 | X-Super-Properties header |
| SEC-P0-10 | REQ-F-016..022 | Gateway IDENTIFY + heartbeat + presence |
| SEC-P0-11 | REQ-NF-022 | HTTP/2 enforced |
| SEC-P0-12 | REQ-NF-023 | Per-burner cookie jar |
| SEC-P0-13 | REQ-NF-050 | Gateway concurrent-instance guard |
| SEC-P0-14 | REQ-NF-057, REQ-NF-027, REQ-NF-028 | Per-host bucket + jitter + burst pause |
| SEC-P0-15 | REQ-NF-058 | 429 Retry-After honouring |
| SEC-P0-16 | REQ-F-044 | Captcha hard abort |
| SEC-P0-17 | REQ-NF-040 | URL allowlist + redirect rejection |
| SEC-P0-18 | REQ-NF-041 | TLS verify=True always |
| SEC-P0-19 | REQ-NF-024 | MIME sniff on attachment |
| SEC-P0-20 | REQ-NF-025 | Streaming size cap |
| SEC-P0-21 | REQ-NF-026 | Filename sanitisation |
| SEC-P0-22 | REQ-NF-037 | File permissions 0o600 / 0o700 |
| SEC-P0-23 | REQ-NF-038 | Retention prune no symlink escape |
| SEC-P0-24 | REQ-NF-038 | Retention prune rejects symlinks |
| SEC-P0-25 | REQ-NF-048 | Gateway + REST fingerprint match |
| SEC-P0-26 | REQ-NF-049 | Plausible client_build_number |
| SEC-P0-27 | REQ-NF-070 | Third-party PII docs section |
| SEC-P0-28 | REQ-NF-069 | output/ gitignored |
| SEC-P0-29 | REQ-NF-003, REQ-NF-031, REQ-NF-041, REQ-NF-051, REQ-NF-054, REQ-NF-055, REQ-NF-056 | CI grep merge-blockers (bundle) |
| SEC-P0-30 | REQ-NF-066, REQ-NF-067 | Dependency lock + pip-audit + Dependabot |
| SEC-P0-31 | REQ-NF-065 | bandit SAST |
| SEC-P0-32 | REQ-NF-068 | .env.example / .env hygiene |

All 32 Phase 0 blocking items have a requirement ID and a verification method.

---

## 10. Known Gaps

These are gaps identified via the standard gap-finding heuristics. Each is recorded as a REQ where actionable, or as an accepted residual where operator-owned.

1. **Empty state**: invite list empty, guild list empty, channel list empty — all handled (REQ-F-001, REQ-F-006, REQ-F-007 edge cases).
2. **Error state**: captcha (REQ-F-044), 401 (REQ-NF-061), 403 (REQ-NF-060), 429 (REQ-NF-058), 5xx (REQ-NF-059), schema validation (REQ-NF-035).
3. **Concurrent user check**: N/A (single-user); still-relevant: concurrent `scan` invocations guarded by filelock (REQ-F-024, REQ-NF-050).
4. **Large dataset**: channel with 200 000 messages handled via `max_messages_per_scan` cap (REQ-F-009 edge); multi-scan resume via cursor (REQ-F-023).
5. **Permission boundary**: 403 channels logged + continue (REQ-NF-060); no member-list probing (REQ-F-008).
6. **Deletion cascade**: guild removed or burner kicked between scans → next scan sees guild absent in `/users/@me/guilds` (REQ-F-006), logs guild-level skip; output tree for prior dates retained per retention policy.
7. **Offline**: not applicable — Discord is network-only. `--offline` flag is state-inspection only (REQ-F-042).
8. **Timezone**: storage in UTC; scheduling window in UTC; operator's local time visible in log prefix (REQ-OPS-017). DST drift inside tool = zero.
9. **Stage-1.5 enrichment not yet shipped**: accepted; scanner falls back to empty-allowlist (effectively permissive) per REQ-F-002 edge case. (spec.md Known Gaps.)
10. **Monthly Chrome-version CI source endpoint**: TBD in architect/workplan phase; recommend `chromiumdash.appspot.com/fetch_releases?channel=Stable` (REQ-OPS-018).
11. **Token-pool rotation semantics with gateway-per-token**: for pool > 1, gateway sessions are spawned serially (one active at a time) — documented in REQ-OPS-016. Multi-token simultaneous gateway sessions is a post-MVP concern.
12. **Retention race with in-flight Stage 3 consumer**: out of scope for Stage 2; Stage 3's responsibility to snapshot before consuming. Documented REQ-INT-009 + spec.md Known Gaps.
13. **Detected-ban heuristic (exit 3)**: implemented as "401 on previously-working endpoint AND `config.yaml` mtime > 24 h" (REQ-NF-061, REQ-F-043). Residual false-positive risk accepted.
14. **CDN URL expiry (~24 h)**: recorded in `meta.json` + `schema_version=1` — Stage 3's problem to snapshot or re-fetch. Documented REQ-F-014.
15. **Linux headless keyring fallback**: plaintext fallback refused at load (REQ-NF-046); operator must run a DE keyring OR use `DISCORD_TOKEN` env var with loud WARNING.

**Total Known Gaps: 15.**

---

## 11. Open Requirements (unresolved decisions)

| # | Question | Options | Recommended | Decision needed by |
|---|---|---|---|---|
| 1 | Chrome stable source endpoint for monthly CI probe | `chromiumdash.appspot.com`, `chromereleases.googleblog.com`, vendored list | `chromiumdash` (deterministic API) | Phase 1 start |
| 2 | Exact 401 → exit 3 staleness threshold | 12 h, 24 h, 48 h | 24 h | Architect phase |
| 3 | `uv.lock` vs `poetry.lock` (SEC-P0-30) | uv / poetry | uv (faster, modern) | Stack-selector phase |
| 4 | `pytest-websocket` vs hand-rolled WS fixtures | lib vs in-repo | hand-rolled (one less dep) | Architect phase |
| 5 | Structured-log sink default (stdout vs stderr vs file) | stdout / stderr / config-flagged file | stdout (default) + `--log-file` optional | Architect phase |

---

## 12. Verification summary

- **Functional**: respx (REST) + websockets mock (gateway) + Typer CliRunner (CLI) + filesystem assertions (output/state).
- **Non-functional**: CI grep guards, ruff, mypy --strict, pytest coverage gate, bandit, pip-audit, timing assertions (jitter/bucket), byte-diff tests (idempotence).
- **Integration**: fixture-file reads (Stage 1 boundary), schema-version asserts, endpoint-set CI grep, per-OS keyring smoke.
- **Operational**: documentation CI grep for required runbook section titles, log-schema pydantic validators, stderr capture for alert messages.

---

## Document quality self-check

- [x] Every section addressed; N/A sections have reasoned one-line explanations.
- [x] Phase 0 blocking items (32) all mapped to REQ-NF / REQ-OPS.
- [x] Every requirement has a testable acceptance criterion or a verification method.
- [x] Anti-detection header set has one REQ-NF per header (REQ-NF-006..021).
- [x] All 8 CLI commands + 4 global flags + 4 exit codes have REQs (REQ-F-031..043).
- [x] All 5 output artefacts (3 JSONL + meta + prior + attachments) have REQs (REQ-F-025..030).
- [x] Gap-finding heuristics applied and recorded as REQs or Known Gaps.
- [x] Traceability matrix present and complete.
