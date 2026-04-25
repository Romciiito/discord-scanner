# discord-scanner

Stage 2 — burner-token Discord scanner producing raw JSONL dumps for Stage 3 curator. Rebuild of dsc-smartscraper.

**Stack:** python-cli

---

## Behavioral Rules (mandatory every session)

1. **Workplan tracking** — Update `workplan.md` checkboxes immediately when any task completes. When all items in a phase are done, add ✅ to the phase header and the summary table. Never defer tracking to a later session.
2. **Security is non-negotiable** — Security items in workplan Phase 0 must be completed before Phase 1 begins. Never defer or skip them.
3. **Read before acting** — Before starting any task, read the relevant doc in `docs/claude/` listed in the pointer table below. Do not rely on memory.
4. **No secrets in code** — Credentials and keys live only in `.env` (gitignored). Never hard-code them, never commit them.
5. **Prefer editing over creating** — Always edit an existing file rather than creating a new one when possible.

---

## Pointer Table

| Doc | Read when you're about to... |
|-----|------------------------------|
| `docs/claude/architecture.md` | Understand the system, add a component, trace a data flow |
| `docs/claude/development.md` | Run locally, write a test, add a route/model/page, run an operator runbook (burner rotation, token compromise, captcha response, chmod degradation) |
| `docs/claude/design-decisions.md` | Question why something is built a certain way, propose alternatives |
| `docs/claude/env-vars.md` | Add a new config value, debug a missing env var, set up a new environment |
| `docs/claude/acceptance.md` | Verify the 15 acceptance criteria from `spec.md §11` still map to passing tests after a change |
| `../ROADMAP.md` | See where Stage 2 fits in the four-stage plan, what's next (Phase 12 hardening, Stage 3 bootstrap), and which TODOs are carried forward |

---

## Critical Gotchas

- All env vars use the `DISCORD_SCANNER_` prefix — never use bare names.
- Check `docs/claude/development.md` for exact run commands before assuming defaults.
- See `docs/claude/design-decisions.md` before proposing a stack or architecture change.


---

## Project-Specific Rules

# Project-Specific Rules — discord-scanner

**Project identity**: `discord-scanner` is **Stage 2** of a four-stage plan (Stage 1 = `civit-hf-scanner` invite discovery; Stage 3 = future Obsidian curator with LLM; Stage 4 = future content workflows). It is a **single-user, workstation-only, Python 3.12+ CLI** that sweeps public text / announcement / forum channels + threads of Discord guilds the operator's burner account has **manually joined**, via a **dormant gateway session + hardened REST** scraping pattern. It operates a burner Discord user account in **direct violation of Discord ToS §3 (self-bots)**; the operator has accepted this in writing. Output is a typed on-disk contract (`messages.jsonl.zst` + `pinned.jsonl` + `threads.jsonl` + `attachments/` + `meta.json` + `prior.txt` under `output/{guild_id}/{YYYY-MM-DD}/`) consumed by a **separate future repo** (Stage 3). There is no server, no daemon port, no multi-tenant, no auth inbound. Outbound HTTP only, to four allowlisted hosts (`discord.com`, `cdn.discordapp.com`, `gateway.discord.gg`, `media.discordapp.net`).

## MUST

- **Runtime + language**: Python 3.12+ only. Windows 11 primary dev target; macOS / Linux CI parity.
- **Stack lock**: `httpx[http2]>=0.27` (single shared `AsyncClient` per run, `http2=True, verify=True`), `websockets>=13`, `tenacity>=8.2`, `pydantic>=2.6` + `pydantic-settings>=2.2`, `keyring>=24.0`, `zstandard>=0.22`, `filelock>=3.14`, `typer>=0.12`, `rich>=13.0`, `structlog>=24.0`, `pyyaml>=6.0`, stdlib `sqlite3`, `pytest>=8` + `pytest-asyncio>=0.23` + `respx>=0.21`, `ruff>=0.4`, `mypy>=1.10` (strict), `hatchling`. No other libs without a spec change.
- **Package path**: code lives under `src/discord_scanner/` with subpackages `session/`, `discovery/`, `fetch/`, `cursor/`, `dump/`. Entry point `discord-scanner = "discord_scanner.cli:app"`.
- **URL allowlist**: outbound only to `{discord.com, cdn.discordapp.com, gateway.discord.gg, media.discordapp.net}` via an httpx `event_hooks["request"]` raising `SSRFViolation` on any other netloc. Redirects out of allowlist are rejected.
- **TLS**: `httpx.AsyncClient(verify=True)` always. `verify=False` anywhere in `src/` is a merge-blocker. Gateway WS scheme is `wss://` always (never `ws://`).
- **Full Discord REST header set**: every REST request carries the full seed-spec §4.1 header set (`User-Agent`, `Sec-Ch-Ua`, `Sec-Ch-Ua-Mobile`, `Sec-Ch-Ua-Platform`, `Sec-Fetch-Site/Mode/Dest`, `X-Super-Properties`, `X-Discord-Locale`, `X-Discord-Timezone`, `X-Debug-Options`, `Origin`, `Referer`, `Accept`, `Accept-Encoding`, `Accept-Language`). Missing any one is a merge-blocker.
- **HTTP/2**: mandatory (`http2=True`); HTTP/1.1 is TLS-fingerprintable.
- **Gateway dormant presence**: every scan opens a WSS gateway session first, sends OPCODE 2 IDENTIFY with `properties` derived from the **same single fingerprint config source** as `X-Super-Properties` (byte-for-byte match), honours HELLO `heartbeat_interval` (clamped `[1000, 120000]` ms), sends OPCODE 3 PRESENCE UPDATE once, discards all other events, and attempts OPCODE 6 RESUME on disconnect before falling back to fresh IDENTIFY.
- **Per-burner cookie jar**: `state/cookies-{keyring_username}.json` — never shared across burners.
- **Token storage**: burner Discord user token loaded ONLY via (1) `keyring` (preferred, platform-native — Windows Credential Manager / macOS Keychain / Linux Secret Service), (2) `DISCORD_TOKEN` env var, (3) `config.yaml.discord_token` (last-resort with WARNING). `store-token` command uses `getpass.getpass()`. Refuse plaintext keyring fallback (`keyrings.alt.*` / `Plaintext*` classes).
- **Token redaction**: `redact_token(t) -> f"{t[:6]}***{t[-4:]}"` helper registered as a structlog processor; every log record passes through it. Token never written to `state/`, `output/**`, or log files at any level.
- **Invite-code redaction**: `redact_invite_code(c) -> f"{c[:2]}***{c[-2:]}"`. CI grep merge-blocker: no `discord.gg/` or `discord.com/invite/` literal inside any `logger.*` / `structlog.*` call.
- **SQLite parameterisation**: every query uses `?` placeholders. Zero string-interpolated SQL. All SQL in `src/discord_scanner/cursor/` and `src/discord_scanner/discovery/invite_cache.py`.
- **Pydantic tolerance**: every model that ingests upstream JSON sets `model_config = ConfigDict(extra='allow')`. A `ValidationError` on a single record logs WARNING + skips that record; the run continues.
- **Cross-platform locking**: `filelock.FileLock` for `state/cursor.lock` and `state/gateway-{burner}.lock`. `fcntl` is forbidden in `src/` (POSIX-only; breaks Windows).
- **Cross-platform chmod 0o600**: `os.chmod(path, 0o600)` on every file in `state/` and `output/**` immediately after creation; `0o700` on directories. Best-effort on Windows (only read-only bit; documented in `docs/claude/development.md`; operators told to enable BitLocker for at-rest).
- **Rate-limit + jitter**: per-host token bucket (`discord.com/api: 2 req/s`, `cdn.discordapp.com: 1 req/s`) + per-request jitter `random.uniform(1.5, 4.0)` s + inter-channel burst pause `random.uniform(30, 90)` s. Tenacity retries honour `Retry-After` on 429; `MAX_429_RETRIES=3` then channel skip.
- **Captcha hard-abort**: 401/403 with body containing `captcha_key`, `captcha_sitekey`, or `captcha_service` → exit 2 with operator runbook message. Never attempt to solve.
- **Attachment security**: stream download only; MIME-sniff first 16 bytes against declared extension (PNG/JPEG/WebP/GIF magic bytes); abort at `max_size_mb * 1.1` bytes and delete partial; sanitise filename via `PurePosixPath(name).name` + null-byte strip + `[<>:"/\\|?*\x00-\x1f]` → `_` + length cap 128; prefix with `{msg_id}_`; `realpath` check within `output_root`; **never send `Authorization` header to `cdn.discordapp.com` or `media.discordapp.net`**.
- **Sort stability / determinism**: every emitted collection sorted before serialise; within-file order is `(channel_id ASC, timestamp ASC, message_id ASC)`; JSON dict keys alphabetised. Two cold runs on unchanged cursor MUST produce byte-identical **decompressed** JSONL.
- **Output boundary**: writes only under `output/{guild_id}/{YYYY-MM-DD}/` and `state/`. Retention prune walks with symlink rejection + root-escape check; never follows symlinks.
- **Quality gates** (every PR): `ruff check src tests` (0 errors), `ruff format --check src tests` (clean), `mypy --strict src` (0 errors), `pytest -q --cov=src/discord_scanner --cov-fail-under=70` (green). Coverage ≥ 85% on `session/rest.py` and `session/gateway.py`. `bandit -r src --severity-level medium` passes.

## MUST NOT

- **No Discord client libraries**: zero imports of `discord`, `discord.py`, `discord.py-self`, `pycord`, `disnake`, `nextcord`. CI grep merge-blocker.
- **No alternative HTTP libs**: zero imports of `requests`, `aiohttp`, `urllib3` (stdlib `urllib` is also avoided — httpx is the sole client).
- **No browser automation**: no `selenium`, `playwright`, `puppeteer`, `pyppeteer`, headless Chrome. CI grep merge-blocker.
- **No LLM SDKs in this repo**: no `anthropic`, `openai`, `langchain`, `llama-index`, `google-generativeai`, `cohere`, embedding libraries. Semantic work belongs in Stage 3. CI grep merge-blocker on `anthropic`.
- **No Obsidian writers, note templating, markdown synthesis**: no `obsidian-*` packages; no write-side vault logic. Belongs in Stage 3.
- **No `time.sleep` inside `async def`**: CI grep merge-blocker. Use `asyncio.sleep`.
- **No `verify=False`**: anywhere in `src/`. CI grep merge-blocker.
- **No `print()` in `src/`**: use structlog logger. `rich.console.Console` allowed only for user-facing CLI output in `cli.py`.
- **No bare `except:`**: always name the exception class; re-raise or log with context.
- **No commented-out code** in merged commits.
- **No raw Discord token pattern** (`[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}`) anywhere in `src/` or `tests/fixtures/`. CI grep merge-blocker.
- **No raw invite URL / code in any log call**: CI grep merge-blocker on `discord.gg/` and `discord.com/invite/` inside `logger.*` / `structlog.*`.
- **No writes outside `output/` and `state/`**: no operations on `~`, `/tmp`, `/etc`, `C:\Windows`, or any path outside the configured roots (after `Path.resolve()` check).
- **No NaN / Infinity in JSON output**: raise instead of serialising.
- **No write endpoints to Discord**: zero POST / PATCH / PUT / DELETE to `discord.com/api/*`. Zero reactions, typing indicators, joins, DMs, channel-create, message-send.
- **No `/guilds/{id}/members` enumeration**: member-list calls are a loud detection signal; hard-forbidden.
- **No auto-joining guilds**: operator joins manually with burner in a browser. Invite-code use via `/api/v10/invites/{code}` is **resolve-only** (with `with_counts=true&with_expiration=true`), never `POST` to join.
- **No captcha auto-solve**: no `2captcha`, `anti-captcha`, local OCR/ML captcha solver. Hard-abort only.
- **No model-weight or binary-blob downloads**: images only, subject to MIME + size cap.
- **No telemetry / anonymous usage stats**: pure local operation.
- **No global mutable state** besides the single shared `httpx.AsyncClient`, the configured structlog logger, and the cursor/cookie/keyring-backed stores accessed through their module APIs.
- **No `fcntl` import** anywhere in `src/`: use `filelock` (cross-platform). CI grep merge-blocker.

## Project-specific commands

- Install: `pip install -e ".[dev]"`
- Lint: `ruff check src tests`
- Format check: `ruff format --check src tests`
- Type-check: `mypy --strict src`
- Test + coverage gate: `pytest -q --cov=src/discord_scanner --cov-fail-under=70`
- CLI help: `discord-scanner --help`
- Store burner token (prompted via getpass, written to keyring): `discord-scanner store-token`
- Dry-run (plans N invites × M guilds × K channels, zero HTTP): `discord-scanner scan --config config.smoke.yaml --dry-run`
- Offline cursor inspection (no network): `discord-scanner status --offline`
- Full scan (one guild): `discord-scanner scan --guild <guild_id>`
- Full scan (all configured): `discord-scanner scan`
- Daemon (weekly loop with jitter + scan-start window): `discord-scanner daemon`
- Resolve invite code(s) via burner: `discord-scanner resolve --invite <code>`
- List joined guilds: `discord-scanner list-guilds`
- Version info: `discord-scanner version`

