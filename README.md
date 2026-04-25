# discord-scanner

Single-user Stage 2 scanner that produces raw, deterministic JSONL dumps of public Discord
text / announcement / forum channels for downstream curation. Burner-token, dormant gateway
session, hardened REST. Output feeds Stage 3 (a separate repo: Obsidian + LLM curator).

> **Discord ToS:** This tool operates a burner Discord user account in violation of
> Discord ToS §3 (self-bots). The operator accepts the consequences (account ban, IP/email
> blacklist) in writing. See [security-model.md](security-model.md) §8 and
> [docs/claude/development.md](docs/claude/development.md) "Third-party PII handling and
> operator obligations" before running.

---

## Quickstart

```bash
# 1. Clone + install
git clone https://github.com/Romciiito/discord-scanner.git
cd discord-scanner
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Store the burner token in your platform keyring (prompted via getpass)
discord-scanner store-token

# 3. Configure (copy the example then edit)
cp .env.example .env
cp config.yaml.example config.yaml   # if not present, see seed-spec.md §5

# 4. Dry-run — plans the work, makes zero HTTP calls
discord-scanner scan --config config.yaml --dry-run

# 5. Live scan
discord-scanner scan --config config.yaml
```

The first scan against a freshly-joined guild typically takes minutes per channel. The
daemon mode (`discord-scanner daemon`) loops scan → sleep with jitter; production runs
should always use the daemon, never a cron.

---

## What it produces

Per scan, per guild, per date:

```
output/{guild_id}/{YYYY-MM-DD}/
├── messages.jsonl.zst        # all messages (sorted, deterministic)
├── pinned.jsonl              # pinned messages
├── threads.jsonl             # thread metadata + index
├── prior.txt                 # previous scan date for this guild (or empty)
├── meta.json                 # scan counters, timings, schema_version
└── attachments/              # downloaded images (PNG/JPG/WebP/GIF)
    └── {msg_id}_{filename}
```

Two cold runs against an unchanged cursor produce **byte-identical decompressed JSONL**
(acceptance #9 in [spec.md](spec.md) §11).

---

## Commands

| Command | What it does |
|---|---|
| `discord-scanner store-token` | Prompts via `getpass`, writes the burner token to the OS keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service). Refuses plaintext keyring backends. |
| `discord-scanner version` | Prints version + git SHA + Python version. |
| `discord-scanner --help` | Lists all 8 commands and 4 global flags. |
| `discord-scanner resolve --invite <code>` | Resolves an invite code to guild ID via `/api/v10/invites/{code}` (read-only — never POSTs to join). |
| `discord-scanner list-guilds` | Lists guilds the burner has manually joined. |
| `discord-scanner scan` | Full scan of all configured guilds. |
| `discord-scanner scan --guild <id>` | Single-guild scan. |
| `discord-scanner scan --dry-run` | Plans the scan; makes zero HTTP calls. |
| `discord-scanner status --offline` | Inspects the cursor sqlite without making any network call. |
| `discord-scanner daemon` | Runs scan → sleep `interval_hours ± jitter_hours` forever. SIGINT/SIGTERM completes the current channel and exits cleanly. |

Exit codes: `0` success, `1` user/config error, `2` runtime error (incl. captcha hard-abort),
`3` detected ban / token invalid.

---

## Security baseline

- **TLS verify=True** everywhere; HTTP/2 mandatory; URL allowlist
  (`discord.com`, `cdn.discordapp.com`, `gateway.discord.gg`, `media.discordapp.net`).
- **Per-host token bucket** (2 req/s on `discord.com/api`, 1 req/s on CDN) wired as an
  httpx request hook — every outbound HTTP is rate-limited, not just the message paginator.
- **Captcha hard-abort** on `captcha_key` / `captcha_sitekey` / `captcha_service` in 401/403
  bodies — the tool exits 2 with an operator runbook message and never auto-solves.
- **Token + invite-code redaction** as structlog processors. CI grep-blocks any raw token
  pattern, `discord.gg/`, or `discord.com/invite/` in log calls.
- **Filename sanitisation + realpath containment** for every attachment.
- **`fsync` on every dump file** before `cursor.advance()` so a crash between disk-flush and
  cursor commit cannot desync state (seed-spec §2.7).
- **Cross-platform locking** (`filelock`); `fcntl` is forbidden in `src/`.
- **0o700 directories / 0o600 files** best-effort for everything under `state/` and `output/`.

Full register: [security-model.md](security-model.md) §6 (32 SEC-P0 items, all green).

---

## Development

See [docs/claude/development.md](docs/claude/development.md) for: running locally,
running tests, runbooks (burner rotation, token compromise, captcha response,
chmod degradation on Windows), and the full PII / operator obligations section.

Architecture lives in [docs/claude/architecture.md](docs/claude/architecture.md).
Design rationale in [docs/claude/design-decisions.md](docs/claude/design-decisions.md).
Env-var reference in [docs/claude/env-vars.md](docs/claude/env-vars.md).

Quality gates (every PR):

```bash
ruff check src tests
ruff format --check src tests
mypy --strict src
pytest -q --cov=src/discord_scanner --cov-fail-under=70
```

CI runs the same matrix on Ubuntu + Windows (Python 3.12) plus `bandit -r src --severity-level medium`,
`pip-audit`, and the CI grep guards (forbidden imports, `verify=False`, sync `time.sleep` in async,
raw token / invite leakage).

---

## Layout

- `src/discord_scanner/` — `session/`, `discovery/`, `fetch/`, `cursor/`, `dump/`, `models/`,
  `cli.py`, `config.py`, `daemon.py`, `retention.py`, `logging_conf.py`, `_paths.py`.
- `tests/` — unit + integration suites; `tests/e2e/test_smoke.py` is the full mocked
  end-to-end pass.
- `docs/claude/` — read on-demand from `CLAUDE.md`'s pointer table.
- `workplan.md` — phase-by-phase build log (Phases 0–10 ✅; Phase 11 in progress).
- `spec.md`, `seed-spec.md`, `security-model.md`, `requirements.md`, `decisions.md` —
  authoritative project documentation.
