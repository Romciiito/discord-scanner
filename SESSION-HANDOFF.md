# Session handoff — guilds-channels CSV workflow

**Written:** 2026-05-08 (Mac side)
**Target:** fresh Claude Code session on the operator's Windows PC after `git pull`
**Active branch:** `bootstrap` at commit `3bc5126` (origin pushed)
**Lifespan:** delete this file once the CSV → scope_snippet.yaml round-trip
is complete; it's a one-shot session bridge, not durable docs.

---

## What was just shipped (Mac side)

Three commits landed on `origin/bootstrap`:

| SHA | What |
|---|---|
| `9b411f9` | `tools/list_guilds_channels.py` — burner token → `guilds_channels.csv` |
| `4af3111` | `tools/csv_to_scan_scope.py` — picked CSV → `discovery.guilds[]` YAML snippet |
| `3bc5126` | `README.md` §"Ops tools" + Pointer Table row in `CLAUDE.md` |

Both scripts reuse `session.rest.make_client` — full Chrome UA header set,
http/2, URL allowlist, structlog token redaction, captcha hard-abort,
429 Retry-After. Nothing new on the security surface.

---

## What the operator wants from this session

**Primary ask:** "vygeneruj mi prázdné CSV s guildy a kanály" (or English
equivalent). Run the 5-step workflow in `README.md` §"Ops tools" exactly:

1. **Verify** `config.live.yaml` exists in repo root and `auth.token_source`
   resolves to a real burner token (keyring entry or `DISCORD_TOKEN` in
   `.env`). If anything is missing, **stop and ask** — do **not** invent or
   accept a token via chat.
2. Run:
   ```bash
   python tools/list_guilds_channels.py \
       --config config.live.yaml \
       --output guilds_channels.csv
   ```
3. Hand `guilds_channels.csv` to the operator. They will add a `pick`
   column with `1` (or `x`/`y`/`yes`/`true`/`*`) on rows they want scanned,
   save back as pipe-delimited.
4. Run:
   ```bash
   python tools/csv_to_scan_scope.py \
       --input guilds_channels.csv \
       --output scope_snippet.yaml
   ```
5. Show the snippet; the operator pastes it under `discovery:` in
   `config.live.yaml`.

Only step 3 is manual.

---

## Hard constraints

- **Token never goes through chat.** Always rely on keyring or `.env` on
  this machine. If the operator pastes a token, refuse and point them at
  `discord-scanner store-token` or `.env`.
- **This is the warm-up window** (memory: burner ages until ~2026-05-13).
  `list-guilds` and `list-channels` are client-typical calls (acceptable),
  but if `list_guilds_channels.py` exits **2** (captcha) or **3** (401),
  **STOP** — abort the warm-up, do not retry, tell the operator. The 5-day
  warm-up is gone if Discord challenges this IP.
- **This machine's IP must already be the warm-up IP.** If the operator
  is on a fresh / VPN'd / mobile-tethered IP that hasn't been used for
  daily browsing, captcha is near-certain. Confirm with the operator before
  running step 2 if in doubt.

---

## State of the wider project (compressed)

- Stage 2 (`discord-scanner`): Phases 0-15 shipped, 393 tests, ≥85% cov.
  Phase 12.c-f live run scheduled ~2026-05-13.
- Stage 3 (`discord-curator`): 60% (85/141 tasks). Phases 3.0-3.7 done.
  3.4-3.13 blocked on Stage 2 fixture.
- Stage 4 (`workflow-builder`): MVP COMPLETE. Waits on a Stage 3 vault.
- The `pick`-column round-trip exists so the operator can prepare a tight
  `discovery.guilds[]` scope **now**, before the live run on ~2026-05-13.

For deeper context see `../HANDOFF.md` + `../PROJECT_WORKPLAN.md` in the
parent workspace repo if cloned (workspace repo is separate:
`github.com/Romciiito/workflow-ai-discord-workspace`). Most operators only
clone `discord-scanner` on this machine — that is fine for the CSV
workflow.

---

## What NOT to do in this session

- Do not start Stage 2 scans, daemon runs, or any non-CSV work (that's the
  ~2026-05-13 live-run window, gated by Runbook 5).
- Do not modify `config.live.yaml` automatically; only show the snippet
  and let the operator paste.
- Do not commit `guilds_channels.csv` or `scope_snippet.yaml` — they may
  contain guild IDs / channel names the operator does not want in git.
  Both are gitignored by `output/` / `state/` patterns? Verify with
  `git check-ignore` if unsure; otherwise leave untracked.
- Do not edit `tools/list_guilds_channels.py` or `tools/csv_to_scan_scope.py`
  unless the operator reports a real bug. The Mac side already smoke-tested
  both with synthetic CSV.

---

## When you are done

Report back to the operator with:

1. Path to the generated `guilds_channels.csv` and its row count.
2. Any non-zero exit codes encountered (with full `stderr`).
3. A reminder to add the `pick` column and re-invoke the second tool.

If the operator asks to delete this file after the workflow lands, do so —
that is the intended lifecycle.
