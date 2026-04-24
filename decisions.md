# Decisions Journal: discord-scanner

This file records non-obvious implementation decisions made by build agents.
Read by the orchestrator on startup to give all agents cross-session continuity.

**Format:** Append new entries at the bottom. Never edit or delete existing entries.

---

## 2026-04-24 — Phase 1 CLI skeleton

**Context:** CLI callback needs to pass `--config / --verbose / --dry-run / --offline`
to every subcommand.

- **Decision:** use `typer.Context.obj` (a slotted `_CliContext` dataclass-like class)
  to carry invocation context. Rejected the alternative — a module-level `_CTX: dict`
  — because claude-rules "No global mutable state besides the single shared
  httpx.AsyncClient, the configured structlog logger, and the cursor/cookie/keyring-backed
  stores" forbids it. Code-reviewer (Opus) flagged this as a MAJOR finding during P1
  review; fix landed in the same commit.

- **Decision:** `_MIN_PLAUSIBLE_CHROME_MAJOR = 130` is a compile-time floor only.
  The dynamic "UA > 90 days stale" horizon from SEC-P0-08 is deferred to
  **TODO-P1-01** — it needs a monthly CI probe against chromestatus.com, which
  belongs in `.github/workflows/ci.yml` (devops-engineer, Phase 8).

- **Decision:** `store-token` silently falls back to default service/username
  when config load fails, but now logs a structlog `WARNING store_token_fallback_to_defaults`
  so the operator sees why. Code-reviewer MAJOR #3.

- **Decision:** `HttpSettings.fingerprint()` is the single source (SEC-P0-25) for
  both the REST `X-Super-Properties` blob (P2) and the gateway OPCODE 2 IDENTIFY
  `properties` payload (P3). Byte-for-byte parity is the responsibility of the
  P2 and P3 callers — enforced by a unit test that will land in each of those phases.

- **Decision:** Chrome UA default bumped from `134.0.0.0` to `134.0.0.0` (kept);
  the _floor_ was raised from `125` to `130` so the default sits comfortably above
  the rejection threshold. If future `discord-scanner` versions ship with a stale
  default UA, config-load will fail fast on the operator's first run.

- **Non-obvious test fixture choice:** `mock_keyring` in `tests/conftest.py`
  monkeypatches the **real** `keyring` module in-place (`monkeypatch.setattr(real_kr, ...)`)
  rather than substituting `sys.modules["keyring"]`. The earlier substitution
  approach broke `from keyring.errors import KeyringError` inside `store-token`,
  which needs the real submodule to exist. In-place patching keeps the CLI's
  error-handling reachable while still isolating tests from the OS credential store.

