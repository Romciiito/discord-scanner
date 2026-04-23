# Technology Stack and Design Decisions: discord-scanner

**Version**: 1.0
**Date**: 2026-04-23
**Author**: stack-selector agent
**Status**: Approved
**Derived from**: spec.md v1.0, security-model.md v1.0, requirements.md v1.0, seed-spec.md (frozen), brainstorm.md (frozen)

---

## 1. Requirements Summary (Constraints That Drive Stack Selection)

| Constraint | Source | Eliminates |
|-----------|--------|------------|
| Single-user workstation CLI — no inbound HTTP surface, no server, no daemon port | spec.md §8.2, brainstorm.md §2 | `python-fastapi` (adds inbound attack surface with zero benefit), `python-fastapi-nextjs` (two deployable units for a single-user tool), `nextjs-fullstack` (wrong runtime entirely) |
| HTTP/2 mandatory — Discord's real clients negotiate HTTP/2 via ALPN; HTTP/1.1 requests are TLS-layer distinguishable | seed-spec.md §4.2, spec.md §4.2 | Any stack that cannot provide HTTP/2 natively in async context |
| Persistent WebSocket gateway session required (dormant-but-present anti-detection) | seed-spec.md §2.5, spec.md §3.5, security-model.md §4.1 | Serverless or request-scoped runtimes (Next.js API routes, lambda); Node.js ecosystem (no ws library with the surgical fingerprint control needed) |
| `discord.py` / `pycord` / `discord.py-self` FORBIDDEN (CI merge-blocker) | spec.md §8.3, brainstorm.md §6, security-model.md §4.1 | All Python Discord framework options — direct websockets is required for fingerprint control |
| `requests`, `aiohttp`, `selenium`, `playwright`, `anthropic` FORBIDDEN | spec.md §8.3, seed-spec.md §3 | Rules out aiohttp-based stacks; no browser automation possible |
| `fcntl` FORBIDDEN (Windows-incompatible) — Windows 11 is primary dev env | spec.md §8.7, brainstorm.md §10, seed-spec.md §3 | POSIX-only subprocess-based locking patterns |
| pydantic v2 + structlog + tenacity + keyring + zstandard + filelock explicitly locked | seed-spec.md §6, brainstorm.md §6 | — (stack is pre-decided; evaluation below validates the decision) |
| Zero LLM / ML / Obsidian calls — pure mechanical scraping | spec.md §8.5, brainstorm.md §5 | `anthropic`, `sentence-transformers`, any vector-DB dependency |
| Single operator, workstation-only, no cloud-hosting, no telemetry | brainstorm.md §2, spec.md §3.1.3 | Tauri desktop (unnecessary Rust complexity), any SaaS-hosted solution |
| Python 3.12+ alignment with civit-hf-scanner (Stage 1 sister project) | brainstorm.md §6, seed-spec.md §3 | Any non-Python runtime |

---

## 2. Options Evaluated

### Option A: python-cli (CHOSEN)

**Summary**: Python 3.12+ CLI using Typer for the command surface, httpx[http2] for REST, websockets>=13 for the dormant gateway session, pydantic v2 for schema tolerance, and the full locked dependency set from seed-spec. This matches civit-hf-scanner (Stage 1 sister project) exactly in tooling style. The entire product is a single pip-installable package with no server component.

**Key technologies**:
- Language/runtime: Python 3.12+
- CLI framework: Typer >= 0.12 + rich >= 13.0
- HTTP client: httpx[http2] >= 0.27 (async, single shared AsyncClient per run)
- WebSocket: websockets >= 13 (surgical gateway session — NOT discord.py)
- Schema / config: pydantic v2 >= 2.6 + pydantic-settings >= 2.2
- State: sqlite3 (stdlib) with filelock for cross-platform advisory locking
- Compression: zstandard >= 0.22
- Secrets: keyring >= 24.0
- Retries: tenacity >= 8.2
- Logging: structlog >= 24.0
- Testing: pytest >= 8, pytest-asyncio >= 0.23, respx >= 0.21, hand-rolled WS fixtures
- Quality: ruff >= 0.4, mypy >= 1.10 (strict), bandit, pip-audit
- Build: hatchling

**Evaluation scorecard**:

| Criterion | Weight | Score (1-5) | Justification |
|-----------|--------|-------------|---------------|
| Functional fit | High | 5 | httpx[http2] gives HTTP/2 natively; websockets>=13 gives surgical gateway fingerprint control; all 15 acceptance criteria in spec.md §11 are satisfiable. No criterion requires a framework feature this stack lacks. |
| Security tooling maturity | High | 5 | bandit + ruff-S (flake8-bandit) for SAST; pip-audit + Dependabot for dependency CVE scanning; keyring for cross-platform OS-native secret storage; filelock single-writer guard. All 32 SEC-P0 items from security-model.md §6 are implementable. |
| Performance at stated scale | High | 5 | Full asyncio throughout; httpx async with shared client; per-host token bucket satisfies spec §3.6 (2 req/s / 1 req/s). 10-20 min per mid-sized guild (spec §9.1) is achievable with asyncio.sleep jitter — no GIL problem because all work is I/O-bound. |
| Team cognitive load | Medium | 5 | Single person (data engineer). Exact same stack as civit-hf-scanner already in use. Zero new languages or frameworks to learn. |
| Operational overhead | Medium | 5 | Single pip-installable package. No containers, no second service, no background daemon beyond the optional daemon subcommand inside the same process. Deploy = `pip install -e .`. |
| Compliance coverage | High | 4 | chmod 0o600 (best-effort on Windows — documented); structlog redaction pipeline; retention prune; keyring OS-native encryption. Windows ACL limitation (os.chmod only sets read-only bit) is documented; BitLocker is the operator-side mitigation. Security-model.md §3.2 accepts this explicitly. |
| Ecosystem maturity | Medium | 5 | Python 3.12 (LTS path to 3.13/3.14); httpx 0.x well-maintained (encode/httpx, active); websockets 13 well-maintained; pydantic v2 stable since 2023; all deps have active release histories. |
| Time to MVP | High | 5 | ~40% of dsc-smartscraper tests port directly (brainstorm.md §9); session/rest.py, session/auth.py, cursor/state.py are near-rewrites of existing modules. MVP is measured in days not weeks. |
| Scale ceiling | Low | 5 | Single-user workstation tool; scale ceiling is irrelevant. 5-20 guilds × 10k messages each fits comfortably in RAM. |
| Test automation quality | Medium | 5 | respx for httpx request mocking; hand-rolled or pytest-websocket WS fixtures; pytest-asyncio for async tests; 70% coverage gate enforced in CI. All 15 acceptance criteria are machine-testable. |
| **Weighted total** | | **49/50** | Clear winner. |

**Pros**:
- HTTP/2 via ALPN is first-class in httpx — Discord's TLS-layer fingerprint check is satisfied (spec §4.2).
- `websockets>=13` gives direct access to raw WebSocket frame dispatch, enabling exact OPCODE 2 IDENTIFY `properties` field control to match the REST `X-Super-Properties` blob byte-for-byte (SEC-P0-25). No framework abstracts this away.
- `keyring>=24.0` is the cross-platform fix for the dsc-smartscraper's macOS-only Keychain shim (brainstorm.md §9). Works on Windows Credential Manager, macOS Keychain, Linux libsecret.
- `filelock` replaces `fcntl` for cross-platform advisory single-writer locking — the #1 Windows-incompatibility bug in the old scraper.
- `zstandard` for JSONL compression handles multi-gigabyte dumps with deterministic streaming and is available as a pure C extension wheel on all three platforms.
- Zero lock-in. Plain Python package, zero proprietary cloud dependency, migration path is trivial.
- Full parity with civit-hf-scanner's CI/quality gates (ruff, mypy --strict, pytest --cov 70%) — the operator maintains both repos with identical discipline.

**Cons**:
- Python GIL means CPU-bound tasks block the event loop. Accepted: ALL workloads in this tool are I/O-bound (HTTP, WebSocket, file I/O). No CPU-bound tasks exist.
- No built-in ALPN version pinning — HTTP/2 is negotiated opportunistically; httpx will fall back to HTTP/1.1 if the server doesn't offer h2. In practice `discord.com` always offers h2. The `http2=True` flag in AsyncClient makes h2 preferred; CI acceptance test can assert `response.http_version == "HTTP/2"` on mocked transport.
- `os.chmod(0o600)` is only partially effective on Windows (sets read-only bit; does not set NTFS ACLs). Documented in `docs/claude/development.md`. Operator must enable BitLocker for full at-rest protection (security-model.md §3.2, SEC-P0-22).

**Security tooling available**:
- SAST: `bandit -r src/ --severity-level medium` + `ruff` with `select = ["S"]` (flake8-bandit) in `pyproject.toml`
- Dependency scanning: `pip-audit` in CI + Dependabot + `pip install --require-hashes` from lock file
- Auth library: `keyring` >= 24.0 (OS-native backend; plaintext fallback rejected at runtime — SEC-P0-06)
- Secrets management: `pydantic-settings` with `env_prefix` + `keyring`; redaction via structlog processor chain
- Known security issues: no known unpatched CVEs in any required dependency as of 2026-04-23. `httpx` had CVE-2023-32681 (SSRF via redirect) patched in 0.24.1 — version lock >= 0.27 is safe. URL allowlist enforced independently via event hooks (SEC-P0-17) providing defence-in-depth.

**Example production systems using this stack**: `civit-hf-scanner` (Stage 1 of the same pipeline); Sentry CLI (Python CLI + httpx); AWS CLI v2 (Python 3.12+, Typer-adjacent); numerous data-engineering ETL pipelines using structlog + pydantic v2.

---

### Option B: python-fastapi (REJECTED)

**Summary**: FastAPI async web framework with Uvicorn/Gunicorn. Would add an inbound HTTP surface (routes, request handling, OpenAPI docs) that has zero value for a single-user CLI and actively increases attack surface.

**Key technologies**:
- Language/runtime: Python 3.12+
- Web framework: FastAPI + Uvicorn
- HTTP client: httpx (same)
- WebSocket: FastAPI WebSocket support (different model)
- Database + ORM: SQLAlchemy 2.0 async (overkill for sqlite cursor state)
- Auth library: python-jose / PyJWT (for inbound auth — N/A here)

**Evaluation scorecard**:

| Criterion | Weight | Score (1-5) | Justification |
|-----------|--------|-------------|---------------|
| Functional fit | High | 2 | FastAPI adds inbound HTTP routing that is completely unused. WebSocket support in FastAPI is for *serving* WebSocket endpoints, not *connecting to* a remote WebSocket as a client — it does not help with the dormant Discord gateway session at all. |
| Security tooling maturity | High | 3 | FastAPI's security ecosystem is for protecting inbound routes (OAuth2, JWT). That entire layer is irrelevant. The relevant security concerns (token redaction, URL allowlist, SSRF, rate-limiting outbound) require the same tools as Option A regardless. |
| Performance at stated scale | High | 4 | Same async httpx under the hood; performance parity with Option A. But introduces Uvicorn startup overhead with zero benefit. |
| Team cognitive load | Medium | 2 | Adds an entire web framework mental model for no functional gain. The operator already knows Python CLI patterns from civit-hf-scanner. |
| Operational overhead | Medium | 1 | Requires a running server process. `pip install` alone is insufficient — operator must manage a running Uvicorn instance, port binding, process management. Completely wrong model for a batch CLI. |
| Compliance coverage | High | 3 | Same controls available, but inbound HTTP surface opens new compliance concerns (HTTP header injection, route path traversal, OpenAPI disclosure) that must then be mitigated. Net-negative. |
| Ecosystem maturity | Medium | 5 | FastAPI is mature. Irrelevant — maturity of an inappropriate tool is no advantage. |
| Time to MVP | High | 2 | Would require designing and discarding a web server layer. More work for less value. |
| Scale ceiling | Low | 5 | Scales well — irrelevant for single-user batch tool. |
| Test automation quality | Medium | 3 | httpx test client is the testing model for FastAPI — which is the same client we already have directly in Option A. No gain. |
| **Weighted total** | | **30/50** | Clear rejection. |

**Primary rejection reason**: No inbound HTTP surface is needed. FastAPI's defining value proposition is inbound route handling; every feature it adds over a plain Python CLI is attack surface, operational overhead, or cognitive load for zero functional benefit. The dormant Discord gateway session is an *outbound client* concern — FastAPI's WebSocket support does not address it.

---

### Option C: raw async Python without Typer (REJECTED)

**Summary**: Plain `asyncio` + `argparse` (stdlib only) for CLI surface, replacing Typer. This was evaluated as a "minimal dependencies" alternative.

**Key technologies**:
- Language/runtime: Python 3.12+
- CLI framework: `argparse` (stdlib)
- Everything else: same as Option A

**Evaluation scorecard**:

| Criterion | Weight | Score (1-5) | Justification |
|-----------|--------|-------------|---------------|
| Functional fit | High | 4 | Functional parity achievable, but REQ-F-CLIa (--help per subcommand with types), REQ-F-CLIb (typed param validation with clear error messages), and the 8-command surface (spec §6) require significant boilerplate in argparse. Typer auto-generates these from type annotations. |
| Security tooling maturity | High | 5 | No difference — security tools are independent of CLI framework. |
| Performance at stated scale | High | 5 | No difference. |
| Team cognitive load | Medium | 3 | argparse subcommand trees with type-validated params are notoriously verbose. `discord-scanner --help` requirement (acceptance criteria §11.3) is harder to achieve cleanly. |
| Operational overhead | Medium | 5 | No difference. |
| Compliance coverage | High | 5 | No difference. |
| Ecosystem maturity | Medium | 3 | argparse is stdlib and stable, but Typer's type-annotation-driven design eliminates entire classes of CLI bugs (missing required arg, wrong type) with zero extra code. |
| Time to MVP | High | 3 | More boilerplate for 8 subcommands + global flags + exit codes. Typer handles `--dry-run`, `--offline`, `--verbose`, `--config PATH`, and subcommand registration in < 20 LOC. argparse equivalent is 100+ LOC of boilerplate. |
| Scale ceiling | Low | 5 | No difference. |
| Test automation quality | Medium | 4 | `typer.testing.CliRunner` and `click.testing.CliRunner` (Typer wraps Click) provide the same interface as argparse's test pattern. Slight advantage to Typer for structured test assertions. |
| **Weighted total** | | **42/50** | Workable but worse. |

**Primary rejection reason**: The `--help` auto-generation, type-validated params, and `CliRunner` test interface that Typer provides reduce boilerplate for the 8-subcommand surface defined in spec §6. civit-hf-scanner already uses Typer — diverging here for no gain increases maintenance burden. The seed-spec explicitly names Typer (brainstorm.md §6); there is no technical justification to drop it.

---

## 3. Recommendation

**Recommended stack**: `python-cli`

**Rationale**:

1. **HTTP/2 mandatory for anti-detection (spec §4.2)**: `httpx[http2=True]` negotiates HTTP/2 via ALPN — the only Python async HTTP client that does so natively without vendoring. Discord's TLS fingerprint distinguishes HTTP/1.1 requests; this is non-negotiable.
2. **Surgical WebSocket fingerprint (security-model.md §4.1, SEC-P0-25)**: `websockets>=13` exposes raw frame dispatch, enabling OPCODE 2 IDENTIFY `properties` fields to match the REST `X-Super-Properties` blob byte-for-byte. Any Discord bot framework (`discord.py`, `discord.py-self`, `pycord`) is forbidden by CI merge-blocker AND would abstract away the exact fingerprint fields we need to control.
3. **Windows-first, cross-platform**: `filelock` (replacing `fcntl`) and `keyring` (replacing macOS Keychain) are the two primary portability fixes over `dsc-smartscraper`. Both are first-class citizens in this stack.
4. **Zero new learning, maximum inheritance**: the operator runs civit-hf-scanner (Stage 1) on the exact same stack. ~40% of the old `dsc-smartscraper` test suite ports directly. MVP is days away, not weeks.

**Why the other options were not selected**:

| Option | Primary reason rejected |
|--------|------------------------|
| python-fastapi | Adding inbound HTTP routes for a batch CLI that has no callers is pure attack surface with zero functional benefit. FastAPI's WebSocket support is for *serving* WebSocket endpoints, not connecting to a remote gateway. The defining feature of FastAPI is exactly what we don't need. |
| raw async Python without Typer | argparse 8-subcommand tree with typed params and `--help` per subcommand requires 5× more boilerplate than Typer for identical functionality. No security, performance, or operational benefit. civit-hf-scanner parity requires Typer. |

**Risks and mitigations for the recommended stack**:

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Discord gateway v10 protocol breaking change (new OPCODE, changed IDENTIFY shape, new required heartbeat fields) | Medium (Discord has changed v9→v10 once) | Medium — gateway session breaks; REST-only scan is more detectable | `config.http.user_agent_chrome_version` monthly CI check (SEC-P1-01); gateway is optional (`gateway.enabled: false`) as fallback; spec's websockets wrapper is a thin abstraction — protocol updates are < 1 day to patch |
| httpx h2 negotiation failure on `discord.com` (server removes h2 from ALPN) | Low | Medium — falls back to HTTP/1.1 transparently; detection risk increases | CI acceptance test asserting `response.http_version == "HTTP/2"` on mocked transport catches silent fallback; httpx ALPN extension negotiation is well-tested |
| websockets 14 breaking API changes | Low (websockets project has stable API since v10) | Low — pinned to `>=13`; breaking changes require major version bump | Pin to `>=13,<15` in `pyproject.toml`; Dependabot alerts on drift |
| Stale Chrome UA / `X-Super-Properties` blob becoming an obvious fingerprint (config drift) | Medium — Chrome releases every 4-6 weeks | High — detection risk rises sharply if UA is > 8 weeks stale | SEC-P0-08 hard-fail at start for stale UA; SEC-P1-01 monthly CI probe of `chromiumdash.appspot.com` for current stable |
| Burner Discord account ban (Discord anti-abuse evolves) | Certain over time | Low — accepted cost; account is disposable | All §4 anti-detection controls; burner rotation runbook (SEC-P1-05); ban is not a tool defect |
| `os.chmod(0o600)` insufficient on Windows (only sets read-only bit; no NTFS ACL) | Certain on Windows | Medium — other local OS users can read dumps | Documented; operator instructed to enable BitLocker on workstation volume (security-model.md §3.2) |
| keyring plaintext fallback (e.g. `keyrings.alt.PlaintextKeyring` installed) | Low-Medium | High — token written unencrypted to disk | SEC-P0-06: detect plaintext backend at load, REFUSE with ERROR and remediation instructions |
| Supply-chain compromise of a critical dependency | Low | High — token exfiltration possible | Lock file + `pip install --require-hashes` in CI; `pip-audit` + Dependabot; monthly manual diff review |

---

## 4. Full Technology List (Recommended Stack)

### Backend (and everything — there is no frontend)

| Technology | Version | Purpose | Why chosen over alternatives |
|-----------|---------|---------|------------------------------|
| Python | 3.12+ | Runtime | Mandatory (seed-spec §6); civit-hf-scanner parity; walrus operator, structural pattern matching, exception groups all available |
| Typer | >= 0.12 | CLI framework — 8 subcommands, global flags, type-validated params | Click-based; `CliRunner` for tests; auto-generated --help; seed-spec §6 mandates it |
| rich | >= 13.0 | User-facing terminal output in `cli.py` only | Console formatting, progress bars; no `print()` allowed in `src/` per spec §8.10 |
| httpx[http2] | >= 0.27 | Async HTTP client for all Discord REST calls + CDN downloads | HTTP/2 ALPN negotiation (spec §4.2); single shared AsyncClient per run; event_hooks for URL allowlist enforcement |
| h2 | (transitive via httpx[http2]) | HTTP/2 state machine | Bundled with httpx[http2] — no direct dependency needed |
| websockets | >= 13 | Discord gateway dormant session | Raw WebSocket protocol access for surgical OPCODE fingerprinting; NOT discord.py (forbidden); NOT aiohttp (forbidden) |
| pydantic | >= 2.6 | Schema validation for Discord API responses, config, and output models | v2 with `extra='allow'` for schema drift tolerance; `ConfigDict(extra='allow')` mandatory |
| pydantic-settings | >= 2.2 | Config file + env var loading with type validation | `env_prefix` pattern; `DISCORD_SCANNER_` prefix for all env vars |
| tenacity | >= 8.2 | Retry discipline for REST 5xx + 429 with Retry-After | Same as civit-hf-scanner; `wait_chain` + `retry_if_exception_type`; honours `Retry-After` header |
| keyring | >= 24.0 | Cross-platform OS-native secret storage for burner token | Replaces macOS-only Keychain shim from dsc-smartscraper; supports Windows DPAPI, macOS Keychain, Linux libsecret |
| zstandard | >= 0.22 | Streaming compression for `messages.jsonl.zst` | Deterministic streaming; dumps can be gigabytes; C extension wheel available for all three platforms |
| filelock | latest stable | Cross-platform advisory file locking for cursor.sqlite and gateway session | Replaces `fcntl` (POSIX-only); works on Windows; single-writer guard |
| structlog | >= 24.0 | Structured logging with processor chain for token + invite-code redaction | Processor chain architecture enables `redact_token` + `redact_invite_code` as mandatory pipeline steps; JSON output |
| sqlite3 | stdlib | Cursor state store and invite-resolution cache | Zero dependency; adequate for single-writer use case; parameterised queries mandatory (`?` placeholders) |

### Data layer

| Technology | Version | Purpose | Why chosen over alternatives |
|-----------|---------|---------|------------------------------|
| sqlite3 (stdlib) | 3.45+ (via Python 3.12) | `state/cursor.sqlite` (last_message_id per channel) and `state/invite_cache.sqlite` (invite→guild_id resolution cache) | No ORM overhead; stdlib; filelock provides single-writer safety; parameterised queries prevent SQL injection |
| zstandard | >= 0.22 | `messages.jsonl.zst` output compression | Streaming compression at point of write; decompressed JSONL is the Stage 2→Stage 3 contract; deterministic on fixed input (idempotence assertion on decompressed content) |
| Plain JSON files | stdlib json | `state/cookies.json`, `meta.json`, `prior.txt` | Low volume; simple; no dependency; chmod 0o600 immediately on write |

### Infrastructure and DevOps

| Technology | Version | Purpose | Why chosen over alternatives |
|-----------|---------|---------|------------------------------|
| hatchling | latest | Build backend | Seed-spec mandates it; civit-hf-scanner parity; PEP 517/518 compliant; no setup.py |
| uv (or pip + pip-tools) | latest | Dependency resolution + lock file | `uv.lock` committed; `pip install --require-hashes` in CI for supply-chain integrity |
| GitHub Actions | — | CI matrix: ruff + mypy + pytest on Python 3.12 × {ubuntu-latest, windows-latest} | Existing pattern from civit-hf-scanner; free; YAML-based |
| Dependabot | — | Automated dependency PR on CVE or version drift | Security requirement (SEC-P0-30) |
| ruff | >= 0.4 | Lint + format, including `S` (bandit) rules | Replaces black + isort + flake8 in one tool; 100× faster; seed-spec mandates |
| mypy | >= 1.10 | Static type checking, `--strict` mode | Seed-spec mandates; zero Any leakage policy |

### Security tooling

| Tool | Stage | Purpose |
|------|-------|---------|
| ruff (select S) | CI + pre-commit | SAST via flake8-bandit rules embedded in ruff |
| bandit | CI | `bandit -r src/ --severity-level medium` — second pass beyond ruff-S |
| pip-audit | CI | Known CVE scan against resolved dependency graph |
| Dependabot | PR auto-open | Automated dependency vulnerability PRs |
| keyring | Runtime | OS-native secret storage; reject plaintext fallback at startup (SEC-P0-06) |
| structlog processor chain | Runtime | `redact_token` + `redact_invite_code` on every log record before emission |
| httpx event_hook | Runtime | URL allowlist enforcement — raises `SSRFViolation` on any non-allowlisted netloc |
| filelock | Runtime | Single-writer guard for cursor.sqlite + gateway session lock |
| CI grep guards | CI | No forbidden imports, no `verify=False`, no raw token pattern, no raw invite in logs, no `time.sleep` in async, no `fcntl` |
| gitleaks / trufflehog | Phase 3 pre-commit | Secret scan on commits (SEC-P3-03) |

### Testing

| Tool | Type | Coverage target |
|------|------|-----------------|
| pytest | Unit + integration | >= 70% line coverage (CI fail-under gate) |
| pytest-asyncio | Async unit | All async code paths |
| respx | HTTP mock | All Discord REST endpoints mocked; no real HTTP in tests |
| Hand-rolled WS fixtures (or pytest-websocket) | WebSocket mock | IDENTIFY→HELLO→HEARTBEAT→READY flow; OPCODE sequence assertions |
| mypy --strict | Static type | 0 mypy errors = 0 Any-typed paths through security-critical code |
| ruff check | Lint | 0 ruff errors including S (security) rules |
| CI grep guards | Merge-blocker | Forbidden imports, verify=False, raw token pattern, raw invite in logs |

---

## 5. Dependency Risk Assessment

| Dependency | Criticality | Maintenance status | Known CVEs | Fallback if abandoned |
|-----------|-------------|-------------------|------------|----------------------|
| httpx | Blocking | Active (encode/httpx; weekly releases) | CVE-2023-32681 patched in 0.24.1; version lock >= 0.27 safe | No practical fallback for HTTP/2 in pure-Python async without aiohttp (forbidden). If httpx were abandoned: fork or vendor. Lock-in is accepted. |
| websockets | Blocking | Active (Aymeric Augustin; v13 released 2024) | No known unpatched CVEs | `aiohttp` WebSocket (forbidden). `trio-websocket` possible but introduces trio dependency. Risk is low: websockets v13 API is stable. |
| pydantic v2 | Blocking | Active (pydantic/pydantic; Tiangolo team) | No known unpatched CVEs | v1 is incompatible; no real alternative for typed schema validation at this quality level. Lock-in accepted. |
| keyring | Blocking | Active (jaraco/keyring; maintained) | No known unpatched CVEs | env var fallback exists (with WARNING); config.yaml last-resort fallback. Token is still loadable; keyring abandonment reduces *storage* security but doesn't break functionality. |
| zstandard | Blocking | Active (indygreg/python-zstandard; stable) | No known unpatched CVEs | gzip (stdlib) is a fallback for compression; zstd decompressed JSONL remains the Stage 2→3 contract, so switching affects downstream. Lock-in for the output format is the real risk. |
| tenacity | Non-blocking (retry logic replaceable) | Active (jd/tenacity; maintained) | No known unpatched CVEs | Manual retry loops. Tenacity abandonment is a maintenance burden, not a ship-blocker. |
| filelock | Non-blocking (single-writer guard) | Active (tox-dev/filelock; maintained) | No known unpatched CVEs | Manual lockfile with `open(lockpath, 'x')` O_EXCL semantics. Fallback is straightforward. |
| structlog | Non-blocking (logging replaceable) | Active (hynek/structlog; maintained) | No known unpatched CVEs | stdlib logging + custom redaction. Structlog abandonment is a medium maintenance burden. |
| typer | Non-blocking (CLI replaceable) | Active (tiangolo/typer; maintained) | No known unpatched CVEs | argparse (see Option C rationale — workable, more boilerplate). |

---

## 6. Build vs. Buy Decisions

| Component | Decision | Choice | Rationale |
|-----------|----------|--------|-----------|
| Discord REST client | Build | `httpx[http2]` + custom header injection | No "buy" option exists that (a) supports HTTP/2, (b) injects the full `X-Super-Properties` header set, (c) doesn't require bot token. `discord.py` and forks are FORBIDDEN by CI merge-blocker. |
| Discord gateway session | Build | `websockets>=13` + hand-rolled OPCODE state machine | Same reason as REST client — surgical fingerprint control is impossible through any existing Discord library. Gateway is dormant-only (heartbeat + presence); the state machine is < 150 LOC. |
| Secret storage | Buy (OS-native) | `keyring` >= 24.0 | Cross-platform OS credential stores (Windows DPAPI, macOS Keychain, Linux libsecret) provide encryption keyed to OS login credentials. Building equivalent from scratch would require vendoring DPAPI bindings. `keyring` is the standard Python interface to all three. |
| Retry logic | Buy (OSS) | `tenacity` | Retry discipline with Retry-After honoring, exponential backoff, and per-exception routing is well-solved by tenacity. Re-implementing this is a maintenance burden with no differentiation. |
| Config + env var loading | Buy (OSS) | `pydantic-settings` | Type-validated config from YAML + env vars with env_prefix is exactly pydantic-settings' purpose. No custom INI/argparse config needed. |
| Compression | Buy (OSS) | `zstandard` | zstd at JSONL streaming scale; the Rust-backed C extension provides deterministic streaming. gzip (stdlib) would work but is 3–5× slower at these data volumes. |
| Cross-platform locking | Buy (OSS) | `filelock` | The single-writer guarantee for cursor.sqlite and gateway session lock. `fcntl` (POSIX-only) is forbidden. Rolling a cross-platform advisory lock from scratch is error-prone on Windows. |
| Schema validation | Buy (OSS) | `pydantic v2` | `extra='allow'` + `ValidationError` skip-on-single-record is the exact pattern needed for Discord API schema drift tolerance. Building equivalent from scratch has no benefit. |
| Rate limiting | Build | Token bucket + asyncio.sleep per host | No library matches the exact two-tier (per-host token bucket + per-channel jitter + burst pause) discipline required. `limits` or `pyrate-limiter` could be evaluated but the logic is < 50 LOC and a core correctness concern — building it keeps the control surface minimal and testable. |
| Structured logging | Buy (OSS) | `structlog` | Processor chain architecture is the right model for mandatory redaction. stdlib logging's Filter class is workable but structlog's composable processor pipeline is cleaner for the `redact_token → redact_invite_code → add_log_level → TimeStamper → JSONRenderer` chain. |
| Output serialisation | Build | `json.dumps` (stdlib) + `zstandard` streaming | No serialisation framework needed; messages are pydantic models with `.model_dump()`; sorting and determinism are control requirements that are cleaner to implement directly than to bolt onto a framework. |
| SAST | Buy (OSS) | `bandit` + `ruff` S rules | Mature, well-understood; no justification for building custom static analysis. |
| Dependency vulnerability scanning | Buy (OSS + SaaS-free) | `pip-audit` + Dependabot | pip-audit covers PyPI CVE database; Dependabot auto-opens PRs. No cost for an open-source repo. |
| Attachment MIME validation | Build | Magic-byte sniff (16 bytes) | 4 image types (PNG/JPEG/WebP/GIF); the check is 10 LOC with known byte signatures. `python-magic` (libmagic binding) would add a C library dependency for a trivial check. Not worth it. |

---

## 7. Upgrade and Migration Path

| Technology | Current version | End of support | Upgrade path |
|-----------|----------------|----------------|-------------|
| Python | 3.12 | Oct 2028 (security-only from Oct 2027) | 3.13 is drop-in compatible for this codebase; upgrade when CI matrix stabilises on 3.13 |
| httpx | 0.27.x | Actively maintained; no EoL policy | Follow semantic versioning; Dependabot handles minor/patch. Major bump: review ALPN and event_hook APIs |
| websockets | 13.x | Actively maintained | Major version (14+): review OPCODE dispatch and connection handler API |
| pydantic | 2.6+ | v2 is current; v1 EOL announced | Already on v2; no near-term migration needed |
| keyring | 24.x | Actively maintained | Follow releases; backend APIs rarely break |
| zstandard | 0.22+ | Actively maintained | Stable C extension interface; minor updates are safe |

**Framework version policy**:
- Security patches: applied within 48 hours of release
- Minor versions: applied monthly (Dependabot PR + review)
- Major versions: assessed quarterly; planned upgrade within 2 months of stable release
- End-of-life tracking: Dependabot + `pip-audit` in CI; explicit Python version EoL calendar entry

**Migration path if this stack is abandoned**: trivial. This is a single pip-installable Python package with no proprietary runtime, no cloud vendor, no database schema to migrate, and no SDK lock-in. Any future maintainer can `pip install -e .` and run tests. The Stage 2→3 contract is a plain JSONL-on-disk format (`messages.jsonl.zst`) versioned via `schema_version: 1` — a schema change requires a version bump and migration note, not a library migration.

---

## 8. Local Development Setup

```bash
# Prerequisites
# - Python 3.12+ (verify: python --version)
# - git (for clone)
# - Windows: Windows Credential Manager is available by default (no extra install)
# - macOS: Keychain is available by default (no extra install)
# - Linux: install libsecret-1-dev + python3-gi for Secret Service keyring backend

# Clone
git clone <repo-url>
cd discord-scanner

# Install (editable, with all dev dependencies)
pip install -e ".[dev]"

# Verify setup
discord-scanner --help          # should list all 8 subcommands
ruff check src tests            # should report 0 errors
mypy --strict src               # should report 0 errors
pytest -q --cov=src/discord_scanner --cov-fail-under=70   # should be green

# Store your burner token (prompted via getpass — token is not echoed)
discord-scanner store-token

# Offline smoke test (no network, inspects cursor state only)
discord-scanner --offline status

# Live smoke test (requires token stored + burner joined to a test guild)
discord-scanner scan --config config.smoke.yaml --dry-run
```

Expected time for a new developer to reach a working local environment: **5–10 minutes** (pip install + tests). The only manual step is `store-token` which requires the operator to have a burner Discord account ready.

---

## 9. Third-party PII Handling and Operator Obligations

This section is required by security-model.md SEC-P0-27 and must remain present to satisfy the CI grep assertion.

**The scraped data is personal data.** Discord message content, author usernames, author snowflake IDs, discriminators, and reaction counts are personal data of third-party Discord users as defined by GDPR (EU) and CCPA (California). By running `discord-scanner`, the operator becomes a **data controller** for that data from the moment it lands on disk under `output/`.

**Operator obligations**:

1. **Lawful basis (GDPR Art. 6(1)(f))**: Before running the scanner against any guild, the operator MUST perform their own legitimate-interests balancing test. The tool cannot make this determination; it only provides the data-handling controls. Key factors: the operator is not commercialising the data; the data is used for personal generative-AI research; the messages are already public within each guild. This does NOT automatically confer lawful basis — the test is the operator's to document.

2. **Data minimisation**: The default retention policy (`raw_dump_keep_days: 30`, `attachment_keep_days: 14`) is the minimum required for the Stage 3 curator to process the dumps. Do not increase these defaults without a documented reason.

3. **Right to erasure**: To honour an erasure request (unlikely for a personal tool, but possible if the operator's research is later published), run `discord-scanner retention --purge-all --confirm` (Phase 1 feature) or manually `rm -rf output/ state/`. There is no "per-user" deletion mechanism at the tool level — the operator must search and delete manually.

4. **MUST NOT scrape**: medical, mental health, therapy, patient support, or any guild where users have a reasonable expectation that their messages are not processed by third parties. Topic filtering is Stage 3's job; the scanner does not enforce this — it is the operator's responsibility to configure the target guild list appropriately.

5. **Attachment content**: `attachments/` may contain NSFW material, personal photographs, or doxxing-adjacent content uploaded by Discord users. The operator is responsible for storing this on an encrypted volume (BitLocker on Windows, FileVault on macOS, LUKS on Linux) and for deciding what to retain.

6. **Breach notification**: If the operator's workstation is compromised and `output/` is exfiltrated, the third-party Discord users whose messages were dumped may have a data-breach claim under applicable law. The operator is responsible for breach notification under applicable law (GDPR Art. 33/34, CCPA). This tool provides no breach notification mechanism.

7. **No telemetry**: This tool emits no data to any third party. All processing is local. There are no usage stats, no error-reporting endpoints, no analytics. The only outbound connections are to `discord.com`, `cdn.discordapp.com`, `gateway.discord.gg`, and `media.discordapp.net` as required for scraping.

---

## Revision History

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 1.0 | 2026-04-23 | Initial stack selection | stack-selector agent |
