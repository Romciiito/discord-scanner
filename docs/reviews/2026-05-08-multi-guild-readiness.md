# Multi-guild full-back-fill readiness audit — 2026-05-08

**Auditor:** general-purpose subagent (Claude Opus 4.7, 1M context)
**Scope:** discord-scanner Phases 13/14/15 + scan orchestrator + storage layout + Phase M (multi-burner)
**Trigger:** operator plan changed from single-guild Phase 12.c-f smoke to full N-guild back-fill (per `~/.claude/projects/-Users-trungle-Desktop-projects-workflow-ai-discord/memory/project_full_backfill_plan.md`).

## TL;DR

The orchestrator already iterates multi-guild and supports a multi-burner pool with scope→burner ownership (Phase M, shipped 2026-04-26). What was originally framed as "Phase 12.c-f single low-stakes guild smoke test" is in fact already wired to drive the full back-fill flow that the operator now wants — `--guild` is just an optional filter on top of the natural "all configured guilds" iteration. **However**, three real gaps deserve fix-on-Mac before the run begins, the largest being that the **adaptive rate-limit budget is per-burner-client (and global across all guilds that burner scans), not per-guild** — the operator must understand that 5 guilds on the same burner share one budget. The other two are observability-on-mid-run (no `--guilds=a,b,c` list, no live "currently scanning guild X of Y" footer) and **no disk-budget guard** before / during the run.

## Method

Read in order:
- `CLAUDE.md` (project rules, multi-burner status, M-phase shipped)
- `src/discord_scanner/cli.py` (CLI surface — `--guild`, `--burner`, `--dry-run` flags)
- `src/discord_scanner/scan/orchestrator.py` (the orchestrator — `run_one_pass`, `_run_one_pass_for_burner`, `_scan_one_guild`, `scan_one_channel`, attachment + thread + backfill helpers; M.1/M.2 ownership filter)
- `src/discord_scanner/scan/scopes.py` (`ScopeMap`, `get_burner_for_guild`, scope→burner ownership)
- `src/discord_scanner/session/rate_limit.py` (base `RateLimiter` — per-host token bucket, global semaphore)
- `src/discord_scanner/session/adaptive.py` (state machine NORMAL→DEGRADED→COOLDOWN, circadian, session-break)
- `src/discord_scanner/session/rest.py` (`make_client` — one limiter per scan, attached to client)
- `src/discord_scanner/cursor/state.py` (`CursorStore`, `CursorFrontier`, v3 `burner_id` column, single-writer FileLock)
- `src/discord_scanner/discovery/channels.py` (`list_channels`, `filter_channels`, `GuildSelector` — categories + globs)
- `src/discord_scanner/discovery/guilds.py` (`list_my_guilds`)
- `src/discord_scanner/fetch/attachments.py` (storage path = `output/{guild}/{date}/attachments/{msg_id}_{name}`)
- `src/discord_scanner/retention.py` (`prune_output` — date-dir aged delete)
- `src/discord_scanner/daemon.py` (loop, refused on >1 burner)
- `config.live.yaml.example`, `src/discord_scanner/config.py` (config surface)
- `docs/claude/development.md` Runbook 5 (single-guild first-live-run posture)
- `.tmp/preflight.md.template` and `.tmp/preflight.md` (existing single-guild template)
- `tests/test_multi_burner.py`, `tests/test_retention.py::test_prune_output_multiple_guilds` (multi-guild test coverage)
- `workplan.md` summary table (Phase M shipped; +393 tests)
- `~/.claude/projects/.../memory/project_full_backfill_plan.md` (operator's actual plan)

## Findings (one per audit dimension)

### Dim 1: Per-guild rate budgeting

- **Status:** ⚠️ PARTIAL
- **Evidence:**
  - `src/discord_scanner/session/rest.py:189-194` — exactly **one** `AdaptiveRateLimiter` (or base `RateLimiter`) is constructed per `make_client` call; it's stashed on the client at `client._discord_scanner_limiter`.
  - `src/discord_scanner/scan/orchestrator.py:317` — `make_client(...)` is called **once per `_open_shared_resources`**, which is invoked **once per burner pass** (`_run_one_pass_for_burner`).
  - `src/discord_scanner/scan/orchestrator.py:1176-1180` — inside one burner's pass, `_scan_one_guild` is called sequentially for each guild, all sharing the same client + limiter.
  - `src/discord_scanner/session/rate_limit.py:55-92` — `RateLimiter._buckets` is keyed on **host** (`discord.com/api`, `cdn.discordapp.com`), NOT guild_id.
  - `src/discord_scanner/session/adaptive.py:114-156` — adaptive state (DEGRADED/COOLDOWN, 429-streak, latency p95) is global across the limiter instance.
- **Detail:** Rate budget is **per-burner-client, per-host, scoped to one full scan pass**. When 5 guilds run on the same burner, they all share one 1.0 req/s `discord.com/api` token bucket, one DEGRADED→COOLDOWN state machine, one 429-streak counter, one circadian dampener. This is correct for ToS-detection avoidance (Discord rate-limits per-token, not per-guild — sharing the budget is exactly what we want). What's NOT in the design: no per-guild fairness (a chatty guild can starve others within the same pass), no per-guild 429-aware skip (a single noisy channel can push the global state into COOLDOWN and pause every other guild's work). For full back-fill on N>3 guilds, this is acceptable but the operator must explicitly know: a 429 cluster on guild #2 throttles guild #3 too, since they share the same burner+limiter.
- **Risk:** Discord's actual rate-limit is per-token-global with per-route sub-buckets. Sharing one limiter across guilds is correct; the audit just surfaces that the operator's mental model "I'm scanning 5 guilds in parallel" is wrong — they're scanning serially within one burner, and rate budget IS shared. Two burners scanning two disjoint guild sets DO get independent budgets.
- **Recommended action:** Document-only on Mac (see preflight template). No code change needed. **Hard rule for the operator's runbook:** if 1 burner is assigned ≥3 guilds, expect end-to-end runtime to scale linearly; do not parallelise within a burner.

### Dim 2: Multi-burner rotation

- **Status:** ✅ READY
- **Evidence:**
  - `src/discord_scanner/config.py:68-83` — `BurnerConfig` model with `keyring_username`, `keyring_service`, `proxy_url`, `schedule_offset_hours`, and per-burner `http_overrides` (UA / locale / client_build_number).
  - `src/discord_scanner/config.py:97` — `auth.burners: list[BurnerConfig]`.
  - `src/discord_scanner/scan/orchestrator.py:1218-1310` — `run_one_pass` iterates burners sequentially when populated; loads each burner's token via `load_token_for_burner`; passes per-burner `http_overrides` + `keyring_username` into `_open_shared_resources`.
  - `src/discord_scanner/scan/orchestrator.py:197-242` — `_settings_for_burner` produces a per-burner Settings snapshot so REST `X-Super-Properties` and gateway IDENTIFY blob match byte-for-byte (SEC-P0-25).
  - CLI `cli.py:519-528` — `--burner` flag with whitelist validation.
  - `tests/test_multi_burner.py` — 11 tests covering iteration, filter, scope→burner ownership, daemon refusal, http_overrides.
- **Detail:** Multi-burner pool is fully wired. The operator declares burners in `config.live.yaml::auth.burners[]`, stores each token via `discord-scanner store-token --burner burner-N`, and either (a) lets `scan` iterate them all in one pass (sequential, not parallel — single host, single egress IP from the Mac) or (b) runs `scan --burner burner-N` separately for each, switching the OS network interface between calls (Topology 2 in `CLAUDE.md`). Daemon mode is **refused** when burners > 1 (`cli.py:673-678`) precisely because a daemon can't switch network interfaces. This is correct.
- **Recommended action:** None. Ready to use. Operator just needs to understand:
  - One pass with all burners listed + same egress IP = burner anti-clustering relies entirely on `http_overrides` (UA/locale/build_number) — fingerprint-only differentiation, NOT IP differentiation. This is "Topology 1" by Phase M's terminology and is the natural Mac choice.
  - To get IP differentiation (Topology 2), use `scan --burner burner-1` from one network, `scan --burner burner-2` from another. Not automatable on Mac without VPN/iface switching.

### Dim 3: Backward-backfill resume / checkpoint per-guild

- **Status:** ✅ READY
- **Evidence:**
  - `src/discord_scanner/cursor/state.py:46-91` — schema PK is `(guild_id, channel_id)`; backfill columns `oldest_seen_message_id`, `newest_seen_message_id`, `backfill_complete`, `backfill_runs`, `backfill_started_at` are all per-row → per-channel.
  - `src/discord_scanner/cursor/state.py:217-303` — `get_frontier`, `advance_backward`, `mark_backfilled`, `increment_backfill_runs` all keyed on `(guild_id, channel_id)`; monotonic-decreasing guard via `_snowflake_less` (numeric snowflake compare).
  - `src/discord_scanner/scan/orchestrator.py:839-965` — `_run_backfill` reads `frontier.backfill_complete` per channel and skips already-done channels; reads `frontier.backfill_runs` against `settings.backfill.max_scan_runs_per_channel`.
  - `src/discord_scanner/cursor/state.py:79-91` — v3 `burner_id` column tracks which burner last advanced each row.
  - Single-writer model: `cursor/lock.py` uses `filelock.FileLock(state/cursor.lock, timeout=0)` — a second concurrent scan refuses to start, so SELECT+UPDATE+COMMIT+fsync are effectively atomic.
  - WAL mode: `state.py:126` — `PRAGMA journal_mode = WAL` + `synchronous = NORMAL` → durable single-writer, concurrent reader (status command can read while scan writes).
- **Detail:** Checkpoint state is per-`(guild_id, channel_id)` (not global, not per-guild only). If a 5-guild run dies on guild #3 channel #7 message #12000, the next run resumes channel #7 from its `last_message_id` (forward) or `oldest_seen_message_id` (backward). Channels in guilds #1, #2 already at `backfill_complete=1` are silently skipped. Guild #4, #5 channels never started simply have no row yet. **One subtlety**: the cursor advance happens per-page-of-messages within `fetch_channel_messages_backward` only at the end of the channel's backfill block (`orchestrator.py:932-940`), not after every page — so a crash mid-channel rolls back to the channel's previous `oldest_seen_message_id`, NOT to the most recent page seen. For 50k-message channels this is fine; for 500k-message channels the operator might lose hours of work. Mitigation: `backfill.per_scan_message_budget` (default 5000) bounds the work-loss window.
- **Recommended action:** None for the run. **Operator awareness:** if back-fill on a single channel exceeds ~30 minutes of wall-clock, lower `backfill.per_scan_message_budget` to 1000-2000 so per-run checkpoints land more often. This is a config tweak, not a code fix.

### Dim 4: Attachment storage scaling

- **Status:** ⚠️ PARTIAL
- **Evidence:**
  - `src/discord_scanner/fetch/attachments.py:88` — path = `output/{guild_id}/{date_str}/attachments/{msg_id}_{safe_name}`.
  - `src/discord_scanner/fetch/attachments.py:78` — `size_cap = max_size_mb * 1.1` per attachment (default 20 MB; smoke config 5 MB).
  - `src/discord_scanner/dump/video_triage.py` — non-GIF videos are NOT downloaded; metadata-only triage to `video-triage.jsonl` (operator decision 2026-04-26).
  - `src/discord_scanner/dump/video_triage.py:1-10` — GIFs are **silent-skipped** at orchestrator level (`orchestrator.py:689-695`) — not downloaded, not counted.
  - `src/discord_scanner/retention.py::prune_output` — deletes `<guild>/<date>/` dirs older than `retention.raw_dump_keep_days` (default 30; smoke config 30) and `attachment_keep_days` (default 14; smoke config 14).
  - **No disk-space guardrail anywhere.** `grep -rn "shutil.disk_usage\|free_bytes\|disk_full\|min_free_disk" src/` returns zero hits (verified during audit).
  - **No compression of old days** beyond the `.zst` already written for `messages.jsonl`. The `attachments/` dir holds raw image bytes uncompressed.
- **Detail:** Per-guild estimation: ~50 channels × avg ~500 images per channel × avg ~500 KB per image = ~12 GB per guild's full back-fill. For 10 guilds = 120 GB. The Mac dev box typically has 200-500 GB free, so this fits, but: there's no in-process check. If disk fills mid-run, the `secure_mkdir` call on the per-channel attachments dir succeeds but the next `httpx` stream-write fails with `OSError: [Errno 28] No space left on device`, which today is caught by the per-channel exception handler (`orchestrator.py:633-643`) as `unexpected_error` and the channel is marked skipped. The orchestrator continues into the NEXT guild — which also tries to write attachments — which also fails. **No early hard-stop on ENOSPC.**
- **Curator-side `.exif_stripped` collision (linked to discord-curator review #4):** discord-scanner writes ONLY under `output/{guild_id}/{date}/`. discord-curator's `.exif_stripped/` directory lives in **its own** vault tree, not the scanner's output. They are sibling repos with separate filesystems. **No collision risk on the scanner side.** This concern is local to discord-curator and out of scope for this audit.
- **Recommended action:**
  1. **Pre-flight:** the multi-guild preflight template (this audit produces it) MUST require operator to run `df -h <output_root>` and forecast ≥ 2× expected disk usage before starting.
  2. **Optional fix-on-Mac (post-back-fill):** add a `shutil.disk_usage(output_root).free < min_free_gb` check at the top of `_scan_one_guild` and raise `FatalScanError(code=1, reason='disk_low')` if violated. Roughly 10 lines in `scan/orchestrator.py`. **Operator can skip this for the first run if they've eyeballed `df -h`.**
  3. **Document-only:** retention prune deletes the date-dir whole-cloth on the daemon's next iteration. For one-shot `scan` runs, the operator must run `discord-scanner daemon --once` (or similar) afterwards to garbage-collect, OR manually `rm -rf output/<guild>/<old-date>` after the back-fill is dumped.

### Dim 5: Channel-selector behavior at scale

- **Status:** ✅ READY
- **Evidence:**
  - `src/discord_scanner/discovery/channels.py:38-65` — `list_channels` is one HTTP GET per guild (not per channel); returns full list.
  - `src/discord_scanner/discovery/channels.py:132-229` — `filter_channels` with selector is pure in-memory: `_resolve_categories` builds a name→id dict O(N), then a single iteration over channels with fnmatch globs. No O(N²) or accidental per-channel API call.
  - `src/discord_scanner/scan/orchestrator.py:999-1028` — `list_channels` is called **once per guild** (one HTTP), then `filter_channels` runs **once per guild** in memory, then `scan_one_channel` is called sequentially for each scannable channel.
- **Detail:** ~500 channels (50 channels × 10 guilds) costs ~10 list_channels calls + ~5000 message-pagination calls (one per channel × ~50-100 pages each). All sequential. The bottleneck is rate-limit (1 req/s × ~5000 = ~83 minutes minimum, before jitter / burst-pause / backfill). Channel-selector resolution itself is O(N) per guild, which scales fine. **No bottleneck.**
- **Recommended action:** None.

### Dim 6: Hard-stop semantics across guilds

- **Status:** ⚠️ PARTIAL
- **Evidence:**
  - `src/discord_scanner/scan/orchestrator.py:1183-1206` — inside `_run_one_pass_for_burner`, the per-guild loop catches `TokenInvalid` / `CaptchaAborted` / `SSRFViolation` / `GatewayConcurrencyError` and **re-raises as `FatalScanError`**. This kills the whole burner pass (and all remaining guilds for that burner).
  - `src/discord_scanner/scan/orchestrator.py:570-581` — inside `scan_one_channel`, the same five fatal exceptions propagate UP from a single channel.
  - `src/discord_scanner/scan/orchestrator.py:599-643` — `ChannelAbort`, `RetryableResponseError`, `httpx.TransportError`, `GatewayError`, `Exception` are isolated per-channel.
  - `src/discord_scanner/scan/orchestrator.py:1218-1310` — `run_one_pass` does NOT catch `FatalScanError` between burners; so captcha on burner-1's guild #3 also kills burner-2's untouched work. This is **intentional** (operator must triage before any further burner traffic), but worth flagging.
  - Daemon mode: `daemon.py:97-102` — `Exception` (any) is caught at the loop level and the daemon survives, but daemon is REFUSED for multi-burner pools (`cli.py:673-678`).
- **Detail:** Hard-stop semantics:
  - Captcha on guild #2 → kills the entire scan (all remaining guilds, all remaining burners). Exit code 2. Correct: the operator must run Runbook 3 (24-72h cool-down) before any further traffic.
  - 401 on `/users/@me/guilds` → kills the entire scan. Exit code 3. Correct: token is dead; Runbook 2.
  - SSRF violation → kills entire scan. Exit code 2. Correct: indicates a code bug or misconfig; halt all to investigate.
  - Cursor-lock contention → kills entire scan. Exit code 1. Correct: another scan is running.
  - 3× consecutive 429 on a single channel → that channel is skipped (`ChannelAbort`); next channel runs. Correct.
  - Adaptive RL state COOLDOWN (5× consecutive 429 OR captcha-key-detection-without-CaptchaAborted) → all subsequent requests inside that burner pass pause for 10-300s, then resume. The next guild on the same burner is delayed but not killed. ✅
- **Risk:** Runbook 5 in `docs/claude/development.md` is single-guild-framed: "if `captcha_aborted` appears, abort." For multi-guild this is still correct, but the operator may wonder "do I lose burner-2's already-scanned guild #1 data?" Answer: **no** — every channel that completed forward-fetch already wrote `messages.jsonl.zst` and advanced its cursor before the captcha hit. Resume is per-channel, not per-run.
- **Recommended action:**
  1. **Hard-stop matrix in the multi-guild preflight template** (this audit produces it): document each fatal class, "halts all guilds: yes/no", "data preserved up to halt: yes/no", "next-run resumes from: cursor".
  2. **No code change needed.**

### Dim 7: Logging / observability

- **Status:** ⚠️ PARTIAL
- **Evidence:**
  - `src/discord_scanner/scan/orchestrator.py:1168-1174` — `run.start` log line includes `burner_id`, `guild_count`, `guild_filter`, `scan_date`.
  - `src/discord_scanner/scan/orchestrator.py:1077-1088` — `run.summary` log line per guild: `guild_id`, `channels_scanned`, `channels_skipped`, `channels_failed`, `messages_fetched`, `pinned_fetched`, `duration_sec`.
  - `src/discord_scanner/scan/orchestrator.py:1207-1214` — `run.finish` per burner with totals.
  - `src/discord_scanner/scan/orchestrator.py:425-431, 646-656` — `channel.start` and `channel.summary` per channel.
  - **No live "currently scanning guild X of Y" footer.** The structured logs let the operator `tail -F` and grep for `run.summary`, but there's no progress bar or top-N visualisation.
  - **`--guild` accepts ONE id only**: `cli.py:518` — `guild: Annotated[str | None, typer.Option("--guild", help="Restrict to one guild_id")] = None`. **No comma-list, no glob.** Multi-guild is implicit (no flag = all configured); explicit subset of two guilds requires either (a) running `scan` twice with `--guild a` then `--guild b`, or (b) editing scope YAML to scope each guild to a different burner and running `scan --burner` per burner, or (c) hand-editing `scopes/` to include only the desired guild list and running plain `scan`.
  - `discord-scanner status --offline` reads cursor.sqlite and shows per-channel frontier (`cli.py:712-794`). Useful for between-run inspection. Not a live mid-run stream.
- **Detail:** During a 5-guild run on one burner, the operator gets:
  - `run.start` once at the top.
  - One `list_channels_ok guild_id=... count=...` per guild.
  - One `guild.selector_applied` per guild (scope filter outcome).
  - Per channel: `channel.start` then `channel.summary` then `backfill.summary` (if backfill enabled).
  - One `run.summary` per guild.
  - One `run.finish` at the end of each burner pass.
  - Inside a guild, no "guild X of Y" — operator counts `run.summary` events themselves.
  - In `tmux`, the natural play is: split the pane, run `scan` in top, `watch -n 30 'discord-scanner status --offline | tail -30'` in bottom.
- **Recommended action:**
  1. **Document-only:** the multi-guild preflight template (this audit produces) MUST include the recommended `tmux` layout + the grep recipe for "what's running now" (e.g. `tail -f scan.log | grep -E 'run.start|run.summary|run.finish|channel.summary'`).
  2. **Optional fix-on-Mac (low priority):** add `--guilds a,b,c` (comma-list) to `cli.py::scan` and pass through to `run_one_pass(guilds_filter=set | None)`. The orchestrator would extend its current `if guild_filter: guilds = [g for g in guilds if g.id == guild_filter]` to support a set membership test. ~15 LOC. **Skip for first run** — work around with scope YAMLs or back-to-back `scan --guild` invocations.

### Dim 8: Storage layout idempotency

- **Status:** ✅ READY
- **Evidence:**
  - `src/discord_scanner/scan/orchestrator.py:417-549` — forward fetch uses `cursor.get(guild_id, channel_id)` as the `after` parameter. Already-fetched messages are excluded by Discord's `?after=...` server-side filter. **No duplicates.**
  - `src/discord_scanner/scan/orchestrator.py:540-548` — cursor is advanced ONLY after fsync.
  - `src/discord_scanner/scan/orchestrator.py:530-536` — daily output dir = `output/{guild_id}/{scan_date}/`. Re-running the same scan on the same UTC day appends to the same dir; running on a different UTC day creates a fresh dir.
  - **Sort stability**: `CLAUDE.md` MUST "Two cold runs on unchanged cursor MUST produce byte-identical decompressed JSONL." Verified by `tests/test_dump.py` and Runbook 5 step 4 idempotence test.
  - **Wednesday vs Monday scenario:** Mon scan of guild A writes `output/A/2026-05-11/messages.jsonl.zst`, advances cursor to msg #1000. Wed scan of guild A writes `output/A/2026-05-13/messages.jsonl.zst` containing ONLY msg #1001+ (cursor-anchored), AND a separate `output/A/2026-05-13/messages.jsonl.zst` for guild C (new). Guild A is **NOT** re-fetched from msg #0 — `cursor.get('A', channel_id)` returns #1000 and forward-fetch starts at `?after=1000`.
  - **Backfill idempotency:** `cursor.advance_backward` has a monotonic-decreasing guard (`state.py:277-281`) — passing a NEWER id than the current oldest is silently ignored. Re-running backfill on a partially-done channel resumes from the last `oldest_seen_message_id`.
  - **Backfill complete flag:** `backfill_complete=1` is checked at the top of `_run_backfill` (`orchestrator.py:862-868`); already-complete channels are skipped on every subsequent pass. Idempotent.
- **Detail:** Wednesday's guild A back-fill correctly skips Monday's already-fetched messages. Guild C is fully scanned. Guild B (if added Wednesday) starts from scratch. **No duplicates.** This is the strongest finding in the audit.
- **Recommended action:** None.

## Critical-path summary

| Dim | Status | Blocker for back-fill? | Recommended timing |
|---|---|---|---|
| 1 — Per-guild rate budget | ⚠️ PARTIAL | No (correctness OK) | doc-only on Mac before run |
| 2 — Multi-burner rotation | ✅ READY | No | — |
| 3 — Backfill checkpoint per-guild | ✅ READY | No | — |
| 4 — Attachment storage scaling | ⚠️ PARTIAL | Soft yes (no disk guardrail) | doc-only on Mac; optional `disk_usage` check post-run |
| 5 — Channel selectors at scale | ✅ READY | No | — |
| 6 — Hard-stop semantics across guilds | ⚠️ PARTIAL | No (semantics correct, doc gap) | doc-only on Mac before run |
| 7 — Logging / observability | ⚠️ PARTIAL | No (works, ergonomics gap) | doc-only on Mac; optional `--guilds` comma-list later |
| 8 — Storage layout idempotency | ✅ READY | No | — |

**Net:** 4 ✅ READY / 4 ⚠️ PARTIAL / 0 ❌ MISSING. **No code-blocker for first multi-guild back-fill.** All four ⚠️ items can be closed by the multi-guild preflight template (Output 2 of this audit) without touching `src/`.

## Recommendations (action items)

### Fix-on-Mac BEFORE 2026-05-13 run

1. **Adopt the multi-guild preflight template** at `.tmp/preflight-multi-guild.md` (this audit produces it). Operator fills it out instead of the single-guild `.tmp/preflight.md` template. Closes Dims 1, 4, 6, 7 via documentation.
2. **Pre-flight `df -h` self-check.** Operator runs `df -h $OUTPUT_ROOT` and forecasts ≥2× expected disk usage. Hard-fail the run if free < forecast. Captured in the new template's "Aggregate self-checks" section.
3. **Decide topology BEFORE writing config.** Single-burner-all-guilds (Topology 1, easy, no IP differentiation) vs split-by-scope (Topology 2, requires scope YAMLs with `burner:` field + manual interface switching between `scan --burner` invocations). Captured in the new template's "Per-guild row table" header.

### Acceptable-risk / Windows-side fix (post first run)

1. **Optional: add `shutil.disk_usage()` guard at top of `_scan_one_guild`** (`scan/orchestrator.py:973`) raising `FatalScanError(code=1, reason='disk_low')` if free < `settings.run.min_free_gb` (new config field). ~15 LOC + test. Operator can ship after Mac-side back-fill if disk pressure surfaces.
2. **Optional: add `--guilds a,b,c` comma-list to `cli.py::scan`** + pass through `run_one_pass(guild_filter)` accepting `set[str] | str | None`. ~15 LOC + test. Quality-of-life only; workaround via scope YAMLs is fine.
3. **Optional: explicit "guild N of M" log line** at the top of each `_scan_one_guild` invocation. ~3 LOC. Nice-to-have for `tail -f` ergonomics during a 10-guild run.

### Operator runbook addition (mandatory)

1. **Append "Runbook 6: First multi-guild back-fill" to `docs/claude/development.md`** that points at `.tmp/preflight-multi-guild.md`. Should be ~30 lines. **Operator writes this after the first run, capturing real timings and disk usage.** This audit pre-stages the lessons-capture template (Output 3).

## Operator decisions surfaced

1. **Topology choice (Topology 1 vs Topology 2 — burners-on-one-machine vs burners-across-networks).** Memory says ≥3 burners exist. For the first multi-guild back-fill on Mac, **Topology 1 with all burners on one egress IP** is the path of least resistance — fingerprint-only differentiation. Topology 2 (different egress IPs per burner) requires VPN / network-iface switching, which the operator cannot easily automate from Mac. **Recommendation: do Topology 1 first; switch to Topology 2 only after a burner ban / captcha event.**
2. **Burner-to-guild assignment (homogeneous-load vs scope-aware).** Two reasonable patterns:
   - **Homogeneous:** put all burners in `auth.burners[]`, leave scopes unowned (`burner: null` everywhere). Each burner scans every guild on its pass — 3 burners × N guilds = 3× duplicate work but 3× resilience. **Wasteful for back-fill** (the cursor dedupes but the rate-limit budget burns).
   - **Scope-aware:** each scope YAML's `burner:` field assigns ownership; each burner scans only its share. **Recommended for back-fill.** Operator must split guilds across burners in scope files before run-day.
   - Decision: **scope-aware**. Operator pre-fills the per-guild row table in the new preflight template with explicit burner assignments.
3. **Total-disk envelope.** Operator must estimate total attachment bytes BEFORE the run. Field is in the per-guild row table. Aggregate row at the bottom forecasts total.
4. **Adaptive RL on or off?** `config.live.yaml.example` ships `http.adaptive.enabled: false` for the smoke. For multi-guild back-fill, **enable it** — the DEGRADED→COOLDOWN transitions are exactly what protects the burner across a multi-hour run. Captured in the new template's CLI smoke section.
5. **Backfill on or off?** `config.live.yaml.example` defaults `backfill.enabled: false`. For the multi-guild **back-fill** plan, **enable it** — that IS the work. Captured in the new template's CLI smoke section.
6. **`max_messages_per_scan` cap.** The smoke config has `200`. For real back-fill, **bump to `10_000` (default)** or higher. Captured in the new template's CLI smoke section.
7. **Where does the lessons-capture template land after the first run?** Suggested: `decisions.md` Phase 12.f appendix (consistent with Runbook 5 step 6's existing instruction). The template at `.tmp/lessons-12-f-template.md` is the working artifact; the consolidated narrative gets folded into `decisions.md`.

---

**End of audit.**
