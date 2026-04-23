# Brainstorm: discord-scanner (Stage 2)

**Status:** FROZEN — this document is the authoritative record of the project's motivating decisions. Downstream agents MUST NOT ask clarifying questions about items covered here. If a detail is missing below, prefer copying from `seed-spec.md` verbatim over asking.

---

## 1. Problem

The user has a curated list of **Discord invite codes** produced by a sister project (`civit-hf-scanner`, Stage 1 + Stage 1.5). For each high-scoring invite, they need to **connect via a burner Discord user account**, enumerate public channels / forums / threads of that guild, fetch message history + pinned messages + attachments, and write structured raw dumps to disk. The dumps feed a downstream curator (Stage 3, separate future repo) that uses Claude to filter and summarise into an Obsidian knowledge base.

A prior version of this tool exists (`github.com/Romciiito/dsc-smartscraper`, Python 3.11, 55 modules, 1,250 tests but no CI). It mixes Stage 2 (scrape) and Stage 3 (write-to-Obsidian) concerns in one monolithic `monitor.py`, is fragile against Discord's anti-abuse fingerprinting, and has several latent bugs (uses `fcntl` on Windows where dev happens, stale Chrome 124 UA, missing `X-Super-Properties` header, no gateway session → the account appears silent). This repo is a ground-up rebuild that **inherits the scraping-core logic** and **ejects the Obsidian / curation concerns** into a separate future repo.

## 2. User

One person — the project owner. Data engineer. Runs the tool on their own workstation (Windows 11 primary, macOS/Linux acceptable, Python 3.12+). Single-user, no team. Re-runs weekly or on demand against a list of ~5–20 target Discord servers. The output feeds their Stage 3 curator which runs later as a separate invocation.

The user **explicitly accepts** that using a Discord user token for automated scraping is against Discord's Terms of Service (§3 self-bots). Mitigations are designed into this tool; the user owns the blast radius via a disposable burner account.

## 3. Inputs & boundaries

**In-scope inputs (all static, on-disk):**
- `civit-hf-scanner/output/latest/invites.enriched.json` — ranked, intent-tagged invite list from Stage 1 + Stage 1.5. Filtered internally on `score_pct ≥ 70 AND intent IN {prompt_sharing, tutorials, workflows, collab}`.
- `config.yaml` — per-run config: burner token source, gateway options, rate-limit tuning, per-server channel filters.
- (optional) `manual_invites` config list — extra invites not from Stage 1.

**Outbound:**
- Discord public REST API (`discord.com/api/v10/*`) via burner user token.
- Discord gateway WebSocket (`gateway.discord.gg`) via burner user token — **minimal dormant session for behavioural fingerprint only**, no event processing.

**Boundaries (hard):**
- **Public-ish channels only.** The tool scrapes channels the burner account has permission to read. No permission escalation. No admin operations.
- **Read-only.** Zero POST / PATCH / DELETE / react / type / join / DM. Pure GET (REST) + receive (gateway).
- **No automated messaging.** Not even on the burner account's own behalf.
- **No member list enumeration.** No `/guilds/{id}/members` calls (loud + risky). Role names fetched once for ID resolution.

## 4. Pipeline (high-level)

```
civit-hf-scanner/output/latest/invites.enriched.json
              │
              │  filter (score_pct + intent)
              ▼
┌──────────────────────────────┐
│ discord-scanner              │
│                               │
│  session/  gateway + rest    │  REST http2 + gateway WS w/ heartbeat
│  discovery/                   │  invite → guild_id, list channels
│  fetch/                       │  paginate messages + pinned + threads
│  cursor/                      │  resume state per (guild, channel)
│  dump/                        │  zstd-compressed JSONL per day per guild
│                               │
└──────────────┬────────────────┘
               │
               ▼
        output/{guild_id}/{YYYY-MM-DD}/
          ├─ messages.jsonl.zst  ← the Stage 2 → Stage 3 contract
          ├─ pinned.jsonl
          ├─ threads.jsonl
          ├─ attachments/*       ← downloaded images only
          ├─ meta.json           ← scan stats, counters, prior scan date
          └─ prior.txt
```

Every inter-stage interface is **plain JSON / Markdown on disk**.

## 5. Non-goals

- **Semantic classification** (intent, topic, noise vs. useful) — Stage 3's job.
- **Any Claude / LLM / embedding calls.** This repo does pure mechanical scraping. Zero ML.
- **Obsidian vault writes, note templates, markdown synthesis.** Stage 3's job.
- **Topic clustering, author profiles, learner weights.** Stage 3's job.
- **Interactive review UI.** Stage 3 has a FastAPI review UI.
- **Multi-user, multi-tenant, hosted service.** Workstation-only CLI.
- **Bot-token operation.** We don't have server admin access for target guilds; bot tokens are impossible here. User token is the accepted (risky) path.
- **Data lifecycle management / analytics.** Raw dumps are raw. Stage 3 decides what's useful.

## 6. Stack lock

- **Python 3.12+** (align with civit-hf-scanner).
- **HTTP:** `httpx[http2]>=0.27` — HTTP/2 mandatory.
- **WebSocket gateway:** `websockets>=13` for the dormant minimal gateway session.
- **Retries:** `tenacity>=8.2`.
- **Config / secrets:** `pydantic>=2.6`, `pydantic-settings>=2.2`, `keyring>=24.0` (cross-platform token storage — replaces macOS-only Keychain).
- **Compression:** `zstandard>=0.22`.
- **CLI:** `typer>=0.12`, `rich>=13.0`.
- **Logging:** `structlog>=24.0`.
- **Tests:** `pytest>=8`, `pytest-asyncio>=0.23`, `respx>=0.21`, `pytest-websocket` (or hand-rolled WS fixtures).
- **Quality:** `ruff>=0.4`, `mypy>=1.10` (strict).
- **Build:** `hatchling`.
- **Forbidden:** `requests`, `aiohttp`, `discord.py`, `discord.py-self`, `pycord`, `selenium`, `playwright`, `obsidian-*` anything.

## 7. Success criteria

A run of `discord-scanner run --config config.smoke.yaml` against a small set of 2–3 target guilds:
- completes end-to-end in 10–20 min per mid-sized guild (~10k messages),
- writes `output/{guild_id}/{YYYY-MM-DD}/{messages.jsonl.zst, pinned.jsonl, threads.jsonl, meta.json}` + `attachments/`,
- produces byte-identical outputs on re-run with unchanged cursor state,
- survives a network drop and resumes from last cursor,
- never triggers a captcha prompt (if captcha detected, aborts cleanly with actionable error),
- gateway session stays connected for the whole scan,
- all structured logs respect the redaction rules from `claude-rules.md`.

The tool can be scheduled via cron / Task Scheduler with randomised intervals and leave the burner account unbanned for at least 3 months of weekly runs (best-effort — Discord's abuse detection is a moving target).

## 8. Decisions log (dated)

- **2026-04-23** — Stack locked: Python 3.12+, httpx http2, websockets, keyring, zstandard. Same tooling surface as civit-hf-scanner.
- **2026-04-23** — Stage 2 scope: raw JSONL dumps per guild per day. No Claude. No Obsidian. Stage 3 (separate future repo) owns those.
- **2026-04-23** — ToS risk acknowledged in writing. Burner account discipline: disposable, VPN recommended, joined only to target guilds, no DMs, weekly rotation optional.
- **2026-04-23** — Gateway session is **dormant-but-present**: IDENTIFY + HEARTBEAT + static ONLINE presence. No event processing. This is the #2 anti-detection fix per the audit of `dsc-smartscraper`.
- **2026-04-23** — Behavioural fingerprint: full `X-Super-Properties` blob + `Sec-Ch-Ua*` + `Sec-Fetch-*` + current Chrome UA + cookie jar. Config-driven Chrome version (≤4 weeks behind current stable).
- **2026-04-23** — Rate limit discipline: per-host token bucket from civit-hf-scanner port, plus per-channel inter-request jitter `[1.5, 4.0] s`, plus burst pause `[30, 90] s` after each channel.
- **2026-04-23** — Attachment policy: images downloaded locally to `output/{guild_id}/{date}/attachments/` with `{msg_id}_{filename}` naming; non-image files get URL-only records (CDN links expire in ~24h — flagged in frontmatter for Stage 3).
- **2026-04-23** — State store: SQLite `state/cursor.sqlite` (per-channel last_message_id, keyed by guild_id + channel_id). Replaces the old scraper's JSON-per-channel file approach. Cross-platform locking via `filelock` library (replacing `fcntl` which only works on POSIX).
- **2026-04-23** — Stage 2 → Stage 3 contract: `messages.jsonl.zst` + auxiliaries with versioned schema (`schema_version: 1`). Breaking schema changes require a new major version bump + migration note.
- **2026-04-23** — Foundation is used in hybrid mode: Phase 0 Socratic is skipped (this document replaces it); Phase 1A onwards runs with `seed-spec.md` as authoritative input. Same pattern as civit-hf-scanner.
- **2026-04-23** — Post-MVP: Stage 3 (discord-curator) will be its own repo with its own Foundation bootstrap, consuming only the `messages.jsonl.zst` files + sidecars. No shared code with this repo beyond shared design principles.

## 9. Inheritance from `dsc-smartscraper`

Modules to port with minor edits:
- `client.py` → `session/rest.py` — replace `urllib` with `httpx[http2]`, add full Discord header set (see seed-spec §4).
- `filters.py`, `categorizer.py`, `scorer.py`, `tags.py`, `image_context.py`, `context.py` — **move wholesale to Stage 3** (they're curation concerns, not scraping).
- `keychain.py` → `session/auth.py` — replace macOS-only Keychain + plaintext fallback with cross-platform `keyring` library.
- `state.py` → `cursor/state.py` — migrate from JSON-per-file to sqlite; replace `fcntl.flock` with `filelock`.

Modules to rewrite from scratch:
- `monitor.py::run_scan` (570 LOC, entangled) — split into `session/`, `discovery/`, `fetch/`, `cursor/`, `dump/` per architecture diagram.
- `learner.py` — **move wholesale to Stage 3** (curation concern).
- `web/app.py` — **move wholesale to Stage 3** (review UI for curator, not scraper).
- `obsidian.py`, `topics.py`, `digest.py` — **all Stage 3**.
- `webhook.py` — not used in Stage 2's flow.

Expect ~40% of the old repo's 1,250 tests to port to Stage 2.

## 10. Known caveats

- **ToS risk is real.** Discord may ban the burner account without warning. Acceptable — the account is disposable, no user content authored from it, no network of follow-on accounts tied to it.
- **Detection landscape changes.** Discord's anti-abuse evolves. This repo will need periodic updates to UA strings, header blobs, and behavioural patterns. A monthly CI job is planned to check Chrome version currency.
- **No `fcntl`** means cross-platform locking via `filelock` — slightly different semantics (advisory vs. mandatory) but covers our single-writer use case.
- **Captcha is a hard abort.** If Discord returns a captcha challenge, the scan stops cleanly and surfaces an operator runbook step. There is no auto-solve.
