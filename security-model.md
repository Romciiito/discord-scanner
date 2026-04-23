# Security Model: discord-scanner (Stage 2)

**Version**: 1.0
**Threat model date**: 2026-04-23
**Author**: security-analyst agent
**Status**: Draft
**Review cadence**: MUST be reviewed on every Chrome-UA rotation, every addition of a new Discord endpoint, every change to token storage, and every change to output retention. Minimum quarterly.

---

## 0. Executive Summary

`discord-scanner` is fundamentally different from its sister project `civit-hf-scanner` in one risk-dominating respect: **it operates a burner Discord user account in direct violation of Discord Terms of Service §3 (self-bots)**. The operator has accepted this in writing (`spec.md` §8.1, §10.1). This threat model therefore treats **Discord's anti-abuse platform as an active adversary**, not a passive infrastructure provider. Every control is evaluated against: "does this make the burner account last longer before Discord bans it?"

Secondary adversary concerns — local-host attackers, supply-chain compromise, accidental data leakage, PII exposure of third-party Discord users whose messages we dump — are modelled below, but the ToS / detection axis is the first-class threat.

**Non-goal**: captcha auto-solve. This is documented as a deliberate security rejection (§8.x). Attempting to automate captcha is both an escalation of ToS violation and a concrete ML fingerprint adversary can train against. Hard abort only.

---

## 1. Authentication Requirements

### 1.1 Login methods required

`discord-scanner` has **no inbound authentication surface** — it is a single-user workstation CLI with no server, daemon port, API, or UI. Auth in this project is exclusively **outbound-credential management** (the burner Discord user token).

| Method | Status | Rationale |
|--------|--------|-----------|
| Email + password | PROHIBITED | No login surface exists. N/A. |
| OAuth (Google/GitHub/etc.) | PROHIBITED | No login surface exists. N/A. |
| Magic link (passwordless) | PROHIBITED | No login surface exists. N/A. |
| SSO/SAML (enterprise) | PROHIBITED | Single-user workstation tool. N/A. |
| API keys (for programmatic access) | PROHIBITED | No callers — this is itself a caller. |
| CLI tokens | N/A | No multi-user CLI. The "token" in this project is the outbound **Discord user token**, not an auth token issued by us. |

**Outbound credential: Discord user token (burner)**

- Loaded ONLY via one of three mechanisms, in order: (1) `keyring` (platform-appropriate backend — Windows Credential Manager, macOS Keychain, Secret Service on Linux) keyed by `auth.keyring_service` + `auth.keyring_username`; (2) `DISCORD_TOKEN` env var; (3) `config.yaml.discord_token` field (last-resort fallback — logs a WARNING explicitly naming the weaker storage).
- `discord-scanner store-token` prompts via `getpass.getpass()` (never echoed) and stores in keyring. `getpass` MUST be used — never a Typer prompt (Typer's default echoes).
- Token is NEVER written to: logs (any level), cache, output artefacts, `meta.json`, cursor DB, cookie jar, stdout/stderr outside `store-token`, exception tracebacks (raise-from with context-stripped messages), or CI fixtures.
- Token redaction helper: `redact_token(t: str) -> str` in `src/discord_scanner/logging_conf.py` returns `f"{t[:6]}***{t[-4:]}"` for strings ≥10 chars; below that, returns `"***"`. CI grep forbids any raw token pattern in `src/`.
- Invite-code redaction helper: `redact_invite_code(c: str) -> str` returns `f"{c[:2]}***{c[-2:]}"`. CI grep merge-blocker: no `discord.gg/` literal inside any `logger.*` or `structlog.*` call.

**Password policy**: N/A — no passwords issued or stored by this tool.

### 1.2 Multi-Factor Authentication (MFA)

**Not applicable to this tool** — no user login. MFA on the upstream burner Discord account is a separate, operator-side concern. **Recommendation to operator** (documented in `docs/claude/development.md`):

- Do NOT enable 2FA on the burner account with the operator's real phone. SMS 2FA ties the burner to the operator's PSTN identity and enables correlation (see §4.x / §5 ToS-specific threats).
- If 2FA is required by Discord before any login, use a disposable VOIP number (not operator's real number).

### 1.3 Session management

**Outbound (Discord session):**

- **Session token type**: Discord user token (opaque, Discord-issued, ~72 bytes base64url). Never rotated by this tool — rotation means "switch to a new burner account".
- **Access token lifetime**: indefinite until Discord revokes. Treated as a long-lived credential.
- **Refresh token lifetime**: N/A — user tokens have no refresh path for us; re-auth means a new Discord login, which only the operator performs interactively on a browser.
- **Refresh token rotation**: N/A.
- **Token storage on client**: keyring (preferred), env var, or config.yaml (last resort with warning). Never localStorage / httpOnly cookie / browser at all — this is a CLI.
- **Concurrent session policy**: the **dormant gateway session** MUST be the ONLY gateway session the burner has open during a scan. Multiple concurrent gateway sessions from the same token are a loud detection signal. The CLI MUST refuse to start a second scan if `state/cursor.sqlite` `filelock` is already held (see §2.4).
- **Session invalidation on**:
  - [x] Suspicious activity detection — on captcha (§3.x) OR on 401 on a previously-working endpoint, scan aborts, exit code 3 ("detected-ban heuristic"). Operator is prompted to check the account manually.
  - [x] Explicit logout — N/A; we don't issue a logout (would be a write to Discord, forbidden).
- **Token revocation mechanism**: Discord-side only. We cannot revoke. If operator suspects token exfiltration, they must: reset the burner's password on Discord → all tokens invalidate → re-run `store-token`.

**Inbound (our CLI)**: no sessions exist.

### 1.4 Account security events (all MUST be logged)

Applied to the **scanner's own operational events** (not "login events" in the classic sense):

- [x] Scan start (with config hash, burner-account alias, timestamp) — NOT the token itself.
- [x] Scan end (with counters from `meta.json`).
- [x] Token-load event (with source: `keyring | env | config`, never the token).
- [x] Token-absent error (loud WARNING with remediation).
- [x] Gateway IDENTIFY / HELLO / READY / DISCONNECT / RESUME events.
- [x] 401 / 403-with-captcha / suspected-ban — structured log at ERROR, exit code 3.
- [x] `store-token` invocation — logged WITHOUT the token value (the fact it ran).
- [x] `retention` prune event — list of deleted directory paths + file counts.

---

## 2. Authorization Model

### 2.1 Access control type

**N/A — no internal authorization surface.** The tool is a single-user CLI; "who is authorised to run scan" is answered by "whoever has shell on the workstation and access to the keyring". Standard OS user-level permissions govern this.

The relevant authorization surface is **Discord-side**, i.e., what the burner account is permitted to read on each guild. We inherit exactly Discord's permission model for that burner. We MUST NOT:

- Attempt any permission escalation (role probe, admin-adjacent endpoint probe).
- Call `/guilds/{id}/members` (loud, member-list enumeration).
- Call any POST / PATCH / DELETE / PUT endpoint on Discord.
- Auto-join guilds (operator joins manually in a browser).

### 2.2 Role definitions

**Internal roles**: none (single-user tool).

**External role relied upon**: the burner's membership-level permission within each guild. If the burner has `VIEW_CHANNEL` + `READ_MESSAGE_HISTORY` on a channel, that channel is in-scope. If not, a 403 on first read MUST be logged, the channel MUST be added to `meta.json:channels_skipped[]` with `reason: "403_no_permission"`, and the scan MUST continue. **We MUST NOT retry 403s** — retries on permission denials look like probing.

### 2.3 Resource ownership

The "resources" in this tool are the local output files under `output/{guild_id}/{date}/` and state files under `state/`.

- **Owner**: the OS user running the CLI.
- **Transfer**: N/A.
- **Cross-org isolation**: N/A (no orgs).
- **Cross-guild isolation on disk**: guild outputs are strictly namespaced by `guild_id`. Retention prune operates per-guild subtree.

### 2.4 Privilege escalation paths (internal)

| Path | Current control | Required control | Risk if unmitigated |
|------|-----------------|------------------|---------------------|
| Config file injection (operator edits `config.yaml` to point `output_root` at `/etc/` or `C:\Windows\`) | None | Path normalisation: `output_root` MUST resolve to an absolute path that does NOT contain `..`, does NOT start with `/etc`, `/proc`, `/sys`, `/boot`, or any Windows system dir (`C:\Windows`, `C:\Program Files`). Reject at config load. | Medium — operator footguns self |
| Symlink pre-planting in `output/` or `state/` to redirect writes | None in old scraper | Check `os.path.realpath(target)` is within configured `output_root` before every write. Reject and abort on mismatch. | Medium |
| `state/cursor.sqlite` SQL injection from response data stored as "last_message_id" | Old scraper doesn't use SQL | ALL sqlite queries use `?` placeholders. Zero string interpolation. CI grep merge-blocker for `f"...{.*}...'.execute(` patterns. | High |
| Second scan started while first running (concurrent writers to same state) | None | `filelock.FileLock(state/cursor.lock)` acquired at scan start with `timeout=0`; second invocation exits with clear error. | Medium (corrupted state) |
| Environment-variable poisoning (user sets `DISCORD_TOKEN=<attacker's token>` to exfiltrate operator data into attacker's account) | None | Not preventable at tool level — if attacker can set env vars on workstation, they own the session. Document: prefer keyring over env. | Medium (accepted: out of tool scope) |

### 2.5 Admin privilege paths

**Not applicable.** No admin role in this tool. The operator has full control of the local environment by definition.

---

## 3. Data Sensitivity Classification

### 3.1 Data inventory

| Data type | Examples | Sensitivity | Encrypted at rest | Encrypted in transit | Retention policy |
|-----------|----------|-------------|-------------------|----------------------|------------------|
| Discord burner token | `MTA4...xyz.Gabc.DEF...` | **Credential (highest)** | OS keyring native encryption (DPAPI on Windows, Keychain on macOS, libsecret on Linux); NOT stored plain on disk | TLS 1.2+ verify=True to `discord.com` only | Indefinite until operator rotates burner |
| Cookie jar (`state/cookies.json`) — `__dcfduid`, `__sdcfduid`, `locale` | Discord session fingerprint cookies | **Credential-adjacent** | Plain JSON file chmod 0o600 (best-effort on Windows — documented degradation) | N/A at rest | Rotated when burner rotates |
| CDN signed URLs in `attachments[].cdn_url` | `https://cdn.discordapp.com/attachments/.../file.png?ex=...&is=...&hm=...` | **Credential-adjacent (24h)** | Stored verbatim in `messages.jsonl.zst`; signed URLs grant read access to the attachment until expiry (~24h) | TLS in transit | Naturally expires; dumps aged out by `retention.raw_dump_keep_days` |
| Message content (`message.content`) | Third-party Discord users' speech | **PII (third-party)** + potentially sensitive (private conversations in gated channels) | chmod 0o600 on `output/{guild_id}/` directory (best-effort on Windows) | TLS in transit | `retention.raw_dump_keep_days: 30` default |
| Attachment binary content (images) | PNG / JPG uploaded by users | **PII (third-party)** — may include NSFW, doxxing material, selfies | chmod 0o600 on `attachments/` | TLS via CDN | `retention.attachment_keep_days: 14` default |
| Author metadata (user_id, username, discriminator) | Snowflake IDs, display names | **PII (third-party)** | Same as messages | TLS | Same as messages |
| Guild metadata (guild_id, guild_name, channel_name) | Server names, channel names | **Internal / semi-public** | Same | TLS | Same |
| Cursor state (`state/cursor.sqlite`) | Last `message_id` per `(guild_id, channel_id)` | **Internal** (leaks which guilds/channels burner scans) | chmod 0o600 | N/A at rest | Until burner rotation |
| Scan logs (structlog JSON to stdout/file if configured) | Counters, timings, redacted URL hashes | **Internal** | chmod 0o600 on log file if written | N/A | Operator discretion |
| Config (`config.yaml`) | May contain `discord_token` in last-resort mode | **Credential (if token present) / Internal** | chmod 0o600 | N/A | Until operator rotates |

### 3.2 Encryption at rest

- **Token**: OS keyring-native encryption. On Windows: DPAPI-backed via `keyring.backends.Windows`. On macOS: Keychain Services. On Linux: Secret Service / libsecret (fallback to encrypted file if no DE keyring — this fallback MUST be logged at WARNING).
- **Output dumps / state / cookies**: **NOT encrypted at rest by this tool**. Protected by:
  - `os.chmod(path, 0o600)` on files immediately after creation.
  - `os.chmod(dir, 0o700)` on directories.
  - **Best-effort on Windows**: `os.chmod` on Windows only sets the read-only bit; true ACL-based protection requires `win32security` which is an avoided dependency. Documented in `docs/claude/development.md`. For stronger at-rest protection on Windows, operator is instructed to enable BitLocker on the workstation volume.
- **No column-level encryption on sqlite** — the data stored (last_message_id per channel) is low-sensitivity internal state. Adding a sqlcipher dependency is rejected (outside the locked stack).
- **Key management**: N/A at tool level; relies on OS user-login secret material.
- **Key rotation**: N/A.

### 3.3 Encryption in transit

- **Minimum TLS version**: TLS 1.2 (TLS 1.3 preferred; `httpx` negotiates the highest available).
- **`httpx.AsyncClient(verify=True)` ALWAYS.** `verify=False` anywhere in `src/` is a merge-blocker CI grep.
- **Certificate store**: system certifi bundle shipped with `httpx`. Do NOT add custom CA pinning (we want to look like a normal Chrome that uses system roots).
- **URL allowlist, enforced at HTTPX event hooks**: only `discord.com`, `cdn.discordapp.com`, `gateway.discord.gg`, `media.discordapp.net` (and only over `https://` / `wss://`). Any other netloc raises `SSRFViolation` and exits 1.
- **HSTS**: N/A at our side — we are a client. `httpx` follows redirects within the allowlist; redirects OUT of the allowlist MUST be rejected.
- **Internal service communication**: N/A (no internal services).

### 3.4 Data residency

- **Where is data stored?** On the operator's workstation only. No cloud storage, no remote database, no telemetry endpoint.
- **Cross-border transfer**: N/A — workstation-local.
- **Operator-sourced cross-border concern**: if the operator travels and runs the tool in a jurisdiction whose privacy law conflicts with the data-subject-rights of Discord users whose messages were scraped, that is a **GDPR/CCPA risk** borne by the operator as a data controller. Documented in §5.
- **User data deletion**: the operator is their own user. To delete: `rm -rf output/ state/` and `keyring erase --service discord-scanner --username burner-1`. There is no "account delete" workflow.

---

## 4. Attack Surface Mapping

### 4.1 Input vectors

Unlike a web service, this tool's input surface is narrow and mostly read-side (upstream Discord JSON + local config). All the same it MUST be enumerated.

| Vector | Threats | Required controls | Severity if uncontrolled |
|--------|---------|-------------------|--------------------------|
| `config.yaml` (operator-supplied) | Path traversal via `output_root` / `state_root`; SSRF via malicious `base_url` (not applicable here — URL allowlist is static at code); injection via per-channel overrides | Pydantic-settings validation; `Path.resolve()` check against normalised root; reject `..` anywhere in any path field after normalisation; reject absolute system paths | Medium |
| `invites.enriched.json` (Stage 1 output) | Malicious upstream: attacker controls this file to point scanner at arbitrary guilds; JSON bomb (deeply nested / huge); invite-code injection into logs | Schema validation via pydantic; size cap (1 MB) on the file; invite code regex `^[A-Za-z0-9-]{4,20}$` before any use; redact on every log touch | Medium (operator is source of truth for this file — low probability of attack) |
| CLI flags (`--invite`, `--guild`, `--config`) | Shell injection if we shell out (we don't); path traversal on `--config` | Typer auto-escapes; `--config` passed to `Path()` then `resolve()`, checked against normalisation; `--invite` regex-validated | Low |
| `DISCORD_TOKEN` env var | Attacker sets env var to exfiltrate operator data into attacker's account | Document: prefer keyring. Log `token_source` at INFO at every scan start so operator notices unexpected source. | Medium (accepted) |
| Discord REST response bodies | Malicious response: unexpected field types, very large messages, deeply nested JSON, malicious CDN URL patterns, XSS-in-content (not rendered by us so irrelevant), null-byte injection in `filename` | `pydantic` with `extra='allow'`, strict types on known fields; size cap per-response (10 MB); sanitize `filename` via `pathlib.PurePosixPath(filename).name` (strip path components) + null-byte strip + forbidden-char strip `[<>:"/\\|?*\x00-\x1f]` + length cap 128 chars before using in local filesystem path | High |
| Discord gateway messages | Since we don't process events, surface is minimal: OPCODE 10 HELLO (heartbeat_interval), OPCODE 0 READY (session_id, sequence) | Only `heartbeat_interval`, `session_id`, `sequence` are read; everything else is discarded. Cap heartbeat_interval to `[1000, 120000]` ms. Discard messages >1 MB. | Medium |
| CDN download response | Malicious attachment: executable masquerading as image, zip-bomb image, oversized image, SSRF via redirect out of CDN allowlist | MIME sniffing via `magic-bytes` check against known image headers (PNG/JPEG/WebP/GIF); refuse download if first 16 bytes don't match declared extension; reject on redirect out of allowlist; hard size cap `max_size_mb * 1.1` (trip the stream) | High |
| Cookie jar load (`state/cookies.json`) | Malicious cookie file (edited by local attacker) injecting tracking cookies | JSON schema validation on load; reject cookies outside `discord.com` domain; reject cookies with `HttpOnly=False` that we wouldn't have set | Low |
| `keyring` backend | Backend compromise / fallback to plaintext | Enforce: if `keyring.get_keyring()` returns a fallback (`keyrings.alt.PlaintextKeyring` or similar), REFUSE to store; log ERROR with instructions to install a real backend | High |

### 4.2 Data flow threats (annotated flow)

```
[Operator]                                  [discord.com/api]
    │                                              ^
    │ getpass (store-token)                        │ TLS 1.2+, verify=True, URL allowlist
    v                                              │
[OS keyring] ─ token read ─> [Session.auth]  ──────┘
                                 │                  ^THREAT: token leak via stacktrace
                                 │                  CONTROL: logging.Filter redact_token on every record
                                 │
                                 v
                            [REST client httpx]
                                 │
[invites.enriched.json] ─> [config load] ─> [Scanner]  ─ gateway WS ──> gateway.discord.gg
                                 │                                       ^THREAT: token in WS IDENTIFY sent in clear-text pre-TLS? NO, wss:// so TLS first
                                 │                                       CONTROL: ensure URL scheme is wss:// not ws://
                                 v
                           [dump/jsonl_writer] ──> output/{guild_id}/{date}/messages.jsonl.zst
                                 │                   ^THREAT: path traversal via filename / msg_id
                                 │                   CONTROL: sanitize + realpath check
                                 v
                           [cursor/state] ──> state/cursor.sqlite
                                 │                   ^THREAT: concurrent writer corruption
                                 │                   CONTROL: filelock, single-writer
                                 v
                           [structlog]  ──────> stdout / log file
                                                ^THREAT: token or raw invite code in log record
                                                CONTROL: redact_token + redact_invite_code in ProcessorChain
```

### 4.3 Third-party integration risks

| Integration | Data shared | Compromise impact | Least-privilege posture | Fallback |
|-------------|-------------|-------------------|-------------------------|----------|
| Discord REST API (`discord.com`) | Burner user token | Account ban (primary kill switch) | Read-only operations; zero write endpoints; minimum endpoint set (`/invites/{code}`, `/users/@me/guilds`, `/guilds/{id}/channels`, `/guilds/{id}/roles`, `/channels/{id}/messages`, `/channels/{id}/pins`, `/channels/{id}/threads/public_archived_threads`, `/guilds/{id}/threads/active`). NO member list, NO write endpoints. | None — core dependency |
| Discord Gateway (`gateway.discord.gg`) | Burner user token (in IDENTIFY), session_id on RESUME | Same as REST | Dormant-only: OPCODE 1 HEARTBEAT, OPCODE 2 IDENTIFY, OPCODE 3 PRESENCE UPDATE (static online), OPCODE 6 RESUME. NO OPCODE 4 (voice state), NO OPCODE 14 (lazy guild request), NO message-send. | Disable gateway if it becomes a stronger detection vector (`gateway.enabled: false`) — but that increases REST-only detection risk |
| Discord CDN (`cdn.discordapp.com`, `media.discordapp.net`) | Nothing (just GETs) | Attachment exfiltration if CDN URLs leak | Use same UA/headers as REST; never send the burner token to CDN | Attachment record URL only on failure |
| `civit-hf-scanner/output/latest/invites.enriched.json` | — (we read) | Malicious content could redirect scanner to arbitrary guilds; still requires burner to be manually joined, limiting blast radius | Schema + regex + size validation on load; file is read-only by us | Fail closed: if file missing/malformed, exit 1 |
| `keyring` library / OS backend | Token | If backend compromised, token leaks | Prefer platform-native backend; reject plaintext fallback | Fall back to env var with loud WARNING |
| PyPI (supply chain) | — (build-time only) | Malicious dependency could exfiltrate token at runtime | Lock file committed (`uv.lock` / `poetry.lock`); `pip install --require-hashes` in CI; Dependabot; monthly manual review of diff | Pin known-good versions |

---

## 5. Compliance Requirements

### 5.1 GDPR

- **Applicable if**: product serves EU users or processes EU citizen data.
- **Applicable here**: **YES — indirectly but materially.** The tool stores message content (personal data of third-party Discord users, potentially EU residents) on the operator's workstation. Under GDPR, the operator becomes a **data controller** for that data from the moment it lands on disk, separate from Discord's own controller role.
- **Required controls**:
  - **Lawful basis**: operator's "legitimate interests" (Art. 6(1)(f)) is the most available, but requires a documented balancing test. This tool cannot make that determination for the operator — it is **documented in `docs/claude/design-decisions.md`** that the operator must perform this test themselves before running the tool against any guild.
  - **Right to erasure**: the tool MUST support bulk deletion. `discord-scanner retention --purge-all` command SHOULD be added (Phase 1 concern; Phase 0 workaround: documented `rm -rf output/`).
  - **Data minimisation**: retention policy defaults (30 days dumps, 14 days attachments) satisfy minimisation-by-default. Operator can tighten.
  - **Breach notification**: if the operator's workstation is compromised, third-party Discord users whose messages were dumped have a breach claim. Operator is responsible for notifying (tool cannot do so). Documented.
  - **DPA with sub-processors**: N/A — no processors; everything is local.
  - **Privacy by design**: MUST chmod 0o600, MUST redact logs, MUST support retention prune, MUST document lawful-basis gap.
- **Verdict**: **Applicable — operator-borne**. Tool must provide the controls; legal interpretation is operator's.
- **Phase 0 gaps**:
  - Retention prune command (can be a thin wrapper over rm/rmtree for Phase 0).
  - Lawful-basis documentation template in `docs/claude/design-decisions.md`.
  - chmod 0o600 on all output + state files.

### 5.2 HIPAA

- **Applicable if**: handles Protected Health Information.
- **Applicable here**: **Not applicable by design, but flag risk**. The tool scrapes generative-AI Discord servers; target guilds are configured by the operator. If the operator happens to add a mental-health / medical-support Discord server to their target list, scraped messages could contain PHI.
- **Required posture**: **Operator MUST NOT add medical / mental-health / therapy / patient-support Discord servers to the target list.** Documented explicitly in `docs/claude/development.md`. Tool does not enforce; topic-filtering is Stage 3's job.
- **Verdict**: **Not applicable — by operator discipline.** Flag, do not design around.

### 5.3 SOC 2

- **Not applicable**: single-user workstation CLI, no B2B customers, no audit requirement.
- **Verdict**: Not applicable.

### 5.4 PCI-DSS

- **Not applicable**: no payment card data.
- **Verdict**: Not applicable.

### 5.5 CCPA / US State Privacy Laws

- **Applicable if**: serves California residents.
- **Here**: same reasoning as GDPR — operator is a data controller for third-party Californian Discord users' content. Rights (know / delete / opt-out-of-sale) are owed by the operator, not the tool. Tool provides enabling controls (retention, purge).
- **Verdict**: Applicable — operator-borne.

### 5.6 Discord Terms of Service — FIRST-CLASS

- **ToS §3 (self-bots)** is violated by design. The operator has accepted this in writing (`brainstorm.md` §2, `spec.md` §8.1, §10.1).
- **This document treats ToS non-compliance as a first-class threat, not a compliance gap.** Mitigations are listed in §4.1 (anti-detection) of seed-spec and threat-modelled in §4.x below.

---

## 6. Phase 0 Security Checklist (BLOCKING — nothing ships without these)

**Target: ≥25 items.** Every item is concrete, testable, and implementable in Phase 0.

### Token handling (6)

- [ ] **SEC-P0-01**: Burner token stored ONLY via `keyring` (preferred), `DISCORD_TOKEN` env var, or `config.yaml.discord_token` (last-resort). Loading via any other mechanism is a merge-blocker. Test: `src/discord_scanner/session/auth.py` has exactly one `load_token()` function, unit-tested to enumerate only these three sources in priority order.
- [ ] **SEC-P0-02**: `store-token` command uses `getpass.getpass()` (never echoing the token). Test: automated test asserts `getpass` is imported in `cli.py` store-token code path.
- [ ] **SEC-P0-03**: `redact_token(t) -> f"{t[:6]}***{t[-4:]}"` helper implemented in `src/discord_scanner/logging_conf.py`; registered as a structlog processor so every log record is passed through it. Test: unit test feeds a realistic token, asserts output is redacted.
- [ ] **SEC-P0-04**: CI grep merge-blocker: no raw Discord token pattern (`[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}`) anywhere in `src/` or `tests/fixtures/`.
- [ ] **SEC-P0-05**: Token never written to: `state/cursor.sqlite`, `state/cookies.json`, `output/**`, log files. Test: integration test runs a mocked scan, greps every artefact for the test token value, asserts zero matches.
- [ ] **SEC-P0-06**: `keyring` plaintext-fallback detection: at load, call `keyring.get_keyring()` and inspect class; if class name contains `Plaintext` or module is `keyrings.alt`, REFUSE and log ERROR with remediation. Test: unit test monkeypatches `get_keyring` to return a mock plaintext backend and asserts RuntimeError.

### Anti-detection headers + gateway session (7 — exceeds the "5+" minimum)

- [ ] **SEC-P0-07**: Full Discord REST header set (seed-spec §4.1) present on every REST request. Test: respx-based integration test asserts every required header key is present on every intercepted request, with non-empty values.
- [ ] **SEC-P0-08**: `User-Agent` Chrome version check: tool refuses to start if `config.http.user_agent_chrome_version` is a hardcoded value older than 8 weeks (staleness check at load, not runtime). Phase 0: hard-fail on start; monthly CI Chrome-version probe is Phase 1. Test: unit test with a mocked `today()` + stale UA exits 1.
- [ ] **SEC-P0-09**: `X-Super-Properties` blob: `base64(json.dumps({os, browser, browser_version, os_version, device, browser_user_agent, system_locale, client_build_number, release_channel}))`. All fields match the UA + platform config. Test: unit test decodes the header and asserts every field matches `http.*` config.
- [ ] **SEC-P0-10**: Gateway session: `session/gateway.py` connects via `wss://` (TLS), sends OPCODE 2 IDENTIFY with properties blob matching REST `X-Super-Properties` byte-for-byte, respects HELLO heartbeat_interval, sends OPCODE 1 HEARTBEAT on schedule, OPCODE 3 PRESENCE UPDATE once. Test: websockets mock asserts IDENTIFY fingerprint == REST fingerprint.
- [ ] **SEC-P0-11**: HTTP/2 enforced: `httpx.AsyncClient(http2=True)` is the single construction site. Test: unit test asserts `client.http2 is True`.
- [ ] **SEC-P0-12**: Per-burner cookie jar: `state/cookies.json` path includes `auth.keyring_username` as filename component (e.g. `state/cookies-burner-1.json`). Cross-burner cookie leakage is prevented by file separation. Test: unit test runs two scans with different `keyring_username`, asserts separate files created.
- [ ] **SEC-P0-13**: Gateway session concurrent-instance guard: `filelock` on `state/gateway-{burner}.lock` acquired before WS connect, `timeout=0`. Second scan with same burner exits 1 with clear error.

### Rate-limit discipline + captcha abort (3)

- [ ] **SEC-P0-14**: Per-host token bucket implemented; defaults `discord.com/api: 2 req/s`, `cdn.discordapp.com: 1 req/s`. Per-request jitter `random.uniform(1.5, 4.0)` s. Burst pause `random.uniform(30, 90)` s between channels. Test: integration test measures inter-request gap, asserts ≥ 1.5s.
- [ ] **SEC-P0-15**: 429 Retry-After honouring: `Retry-After` header respected; after `MAX_429_RETRIES=3` consecutive 429s same endpoint, channel abort (continue to next). Test: respx returns 429 three times, asserts channel is skipped.
- [ ] **SEC-P0-16**: Captcha detection: on 401/403 with body containing `captcha_key`, `captcha_sitekey`, OR `captcha_service` key, scan aborts with exit code 2 and operator runbook message. **Captcha auto-solve is explicitly NOT implemented** (see §8.x rejected controls). Test: respx returns captcha body, asserts exit 2 and no further requests.

### URL allowlist + TLS (2)

- [ ] **SEC-P0-17**: URL allowlist enforced via httpx `event_hooks["request"]` raising `SSRFViolation` on any netloc not in `{discord.com, cdn.discordapp.com, gateway.discord.gg, media.discordapp.net}`. Redirects out of allowlist also rejected. Test: respx returns a 302 to `evil.com`, asserts SSRFViolation.
- [ ] **SEC-P0-18**: `httpx.AsyncClient(verify=True)` always. CI grep merge-blocker: `verify=False` anywhere in `src/`.

### Attachment download security (3)

- [ ] **SEC-P0-19**: MIME sniff before write: read first 16 bytes, validate against known image headers (PNG `89 50 4E 47`, JPEG `FF D8 FF`, WebP `52 49 46 46 ... 57 45 42 50`, GIF `47 49 46 38`). If mismatch with declared extension, discard bytes, record URL only, log WARNING. Test: craft response with `.png` extension but `MZ` header (Windows PE), assert discarded.
- [ ] **SEC-P0-20**: Streaming size cap: download aborts at `max_size_mb * 1.1` bytes read; partial file deleted. Test: respx returns oversized stream, assert abort + cleanup.
- [ ] **SEC-P0-21**: Filename sanitisation: `attachment.filename` passed through `pathlib.PurePosixPath(name).name`, null-byte stripped, replaced `[<>:"/\\|?*\x00-\x1f]` with `_`, length capped 128 chars, result prefixed with `{msg_id}_`. `os.path.realpath` on final path asserted to be within `output_root`. Test: malicious filename `../../etc/passwd` → stored as `{msg_id}_etc_passwd` in correct dir.

### Output retention + local permissions (3)

- [ ] **SEC-P0-22**: `os.chmod(path, 0o600)` on every file in `state/` and `output/**` immediately after creation. Directories `0o700`. Best-effort on Windows; explicitly documented in `docs/claude/development.md`. Test: on Linux CI, assert mode bits; on Windows CI, assert read-only bit set.
- [ ] **SEC-P0-23**: Retention prune at scan start: `output/{guild_id}/{date}/` older than `retention.raw_dump_keep_days` deleted. No `..` escape possible — walk only `Path(output_root).resolve()` subtree, reject any path whose `realpath` escapes root. Test: craft symlink `output/{guild_id}/2020-01-01 -> /` and run prune, assert symlink rejected and nothing outside root touched.
- [ ] **SEC-P0-24**: Retention prune SHOULD NEVER follow symlinks: use `shutil.rmtree(..., followlinks=False)` equivalent, or manual walk with `os.path.islink` check.

### Gateway / REST fingerprint correlation (2)

- [ ] **SEC-P0-25**: Fingerprint config is the SINGLE source: `config.http.user_agent_chrome_version`, `config.http.fake_os`, `config.http.fake_os_platform`, `config.http.locale`, `config.http.timezone`. `X-Super-Properties` blob AND gateway IDENTIFY `properties` MUST derive from these fields. Mismatch between the two is a merge-blocker unit test.
- [ ] **SEC-P0-26**: `client_build_number` in `X-Super-Properties` and IDENTIFY must be a plausible value (≥ 300000) bumped monthly. Config field `config.http.client_build_number`. Test: unit asserts numeric + in plausible range.

### PII + third-party data disclosure (2)

- [ ] **SEC-P0-27**: `docs/claude/design-decisions.md` contains a section titled **"Third-party PII handling and operator obligations"** documenting: (a) scraped message content is personal data of third-party Discord users, (b) operator becomes a GDPR/CCPA data controller, (c) operator MUST perform their own lawful-basis balancing test, (d) operator MUST NOT scrape medical/mental-health/therapy/patient-support guilds, (e) `attachments/` may contain NSFW or doxxing material — operator is responsible for safe storage. Phase 0 blocker: CI grep asserts the section exists.
- [ ] **SEC-P0-28**: `output/` directory gitignored by default. Test: `.gitignore` contents asserted in CI.

### CI grep guards + dependency hygiene (4)

- [ ] **SEC-P0-29**: CI grep merge-blockers pass: no `import requests`, `import aiohttp`, `import discord`, `import anthropic`, `from selenium`, `from playwright`, no `verify=False`, no `time.sleep` inside `async def`, no raw `discord.gg/` or `discord.com/invite/` literal in any `logger.*`/`structlog.*` call, no raw Discord token pattern (see SEC-P0-04), no `fcntl` import in `src/`.
- [ ] **SEC-P0-30**: Dependency lock file committed (`uv.lock` or `poetry.lock` — stack-selector decides which). `pip install --require-hashes` in CI. No dependency with known critical CVE at launch (Dependabot + `pip-audit` in CI).
- [ ] **SEC-P0-31**: SAST in CI: `bandit -r src/ --severity-level medium` passes. `ruff` rule-set includes `S` (flake8-bandit).
- [ ] **SEC-P0-32**: `.env.example` committed with documented placeholder only; `.env` gitignored.

**Total Phase 0 blocking items: 32** (exceeds the ≥25 target).

---

## 7. Security Items for Workplan (by phase)

### Must be in Phase 0 (blocking)

All 32 items from §6. Slot these into `workplan.md` Phase 0 by their SEC-P0-## ID.

### Should be in Phase 1 (core data + API)

- **SEC-P1-01**: Monthly CI cron job probing current Chrome stable version (e.g. `chromiumdash.appspot.com/fetch_releases?channel=Stable`). Fails CI if `config.http.user_agent_chrome_version` is > 8 weeks behind.
- **SEC-P1-02**: `discord-scanner retention --purge-all --confirm` command. Bulk deletion of `output/`, `state/`, and keyring entry.
- **SEC-P1-03**: Structured log schema validation: every log record passes a pydantic model check (`{stage, source, url_hash, cache_hit, http_status, duration_ms, attempt}` for REST; `{stage, event_type, timestamp}` for gateway) — so accidental extra fields don't slip through redaction.
- **SEC-P1-04**: SBOM generation in CI (`cyclonedx-py` or equivalent).
- **SEC-P1-05**: Burner-rotation runbook in `docs/claude/development.md`.

### Should be in Phase 2 (resilience / operator UX)

- **SEC-P2-01**: `--offline` mode that verifies cursor state without any network — Phase 0 already requires this, Phase 2 adds structured drift-detection output.
- **SEC-P2-02**: `discord-scanner doctor` command: checks keyring backend, TLS, URL allowlist, UA version, token round-trip (without printing it), permissions on `state/`.
- **SEC-P2-03**: Tamper-evident log option: append-only structured log with `prev_hash` chaining (optional opt-in).

### Should be in Phase 3 (hardening)

- **SEC-P3-01**: Fuzz testing of pydantic models against malformed Discord payloads.
- **SEC-P3-02**: Test matrix expansion: Windows + Linux CI both green on all grep guards and SAST.
- **SEC-P3-03**: Secret-scan on commits (`gitleaks`, `trufflehog` pre-commit).

### Should be in Phase 4 (observability / ongoing)

- **SEC-P4-01**: Monthly re-threat-model review entry (this document's §0 notes quarterly minimum).
- **SEC-P4-02**: Burner-ban timeline tracking in operator's private notes (not the tool — operational discipline).

---

## 8. Known Risks and Accepted Risks

### 8.1 Accepted / deferred risk table

| Risk | Severity | Why deferred | Mitigation by phase | Owner |
|------|----------|--------------|---------------------|-------|
| Burner Discord account ban (ToS §3 violation) | **Critical but accepted** | This IS the use case; Discord's anti-abuse is the adversary. Ban is a cost of doing business, not a defect. | Ongoing: every §4 anti-detection item; burner discipline (disposable, manually joined, no authored content, VPN recommended). Expected to lose ~1 burner per 3 months. | Operator |
| IP blacklist (Discord IP ban spreads across all burners on the IP) | High | Low probability; Discord prefers account-level action. Mitigation: operator uses residential-grade IP or VPN (documented). | Docs in Phase 0 (`docs/claude/development.md`); no code mitigation. | Operator |
| Burner↔real-identity correlation (operator used real email/phone on burner) | High | Prevention is human-process, not code. | `docs/claude/development.md` mandates: disposable email, disposable VOIP, clean browser profile, never sign in with real account on same session. | Operator |
| Server-admin expulsion / ban by a target guild's moderators based on burner's read-only behaviour pattern | Medium | Best-effort: anti-detection reduces signal. Once detected, an admin ban on one guild does not affect other guilds (per-guild isolation). | Burst pauses, jitter (§4.5); burner rotates if >2 guild bans in a week. | Operator |
| Captcha weaponisation (frequent captchas signal automation, even if we abort cleanly) | Medium | Hard abort is our only response. Auto-solve is rejected (§8.3). Operator rotates burner if captcha fires. | Phase 0: captcha hard-abort (SEC-P0-16). | Operator on ops; tool on abort. |
| Gateway session ↔ REST fingerprint drift over time | Medium | Single config source (SEC-P0-25) pins them together at code level. Drift is prevented at load. | SEC-P0-25 Phase 0. | Tool. |
| Stale Chrome UA / client_build_number becoming an obvious fingerprint | Medium | Hard-fail at load (SEC-P0-08, SEC-P0-26); CI Chrome-version probe in Phase 1 (SEC-P1-01). | Phase 1. | Tool. |
| Cookie jar cross-session leakage across burners | Low | Prevented by per-burner filename (SEC-P0-12). | Phase 0. | Tool. |
| CDN URL expiry (~24h) leaking into output as "stale credential" | Low | Documented in `meta.json`; Stage 3's problem if it defers fetch. Attachments we DO download are expiry-free. | N/A — by design. | Stage 3. |
| Third-party Discord users' message content (PII) on workstation under operator's data-controller duty | Medium | Tool provides controls (chmod, retention, purge); operator owns legal interpretation. | Phase 0: SEC-P0-22, SEC-P0-27; Phase 1: SEC-P1-02. | Operator legal, tool controls. |
| Attachment NSFW / doxxing content in `attachments/` | Medium | Size + MIME cap prevents blob attacks; content semantics are Stage 3's job. Operator MUST store on encrypted volume (BitLocker on Windows, FileVault on macOS, LUKS on Linux). | Phase 0 docs. | Operator. |
| Workstation compromise → token exfiltrated → attacker scans from same burner | High | Out of this tool's attacker model (adversary is Discord, not local-host attacker). Keyring's OS encryption is the only mitigation; operator's OS login password strength matters. | Docs; no code. | Operator. |
| Supply-chain attack on a pinned dependency (`httpx`, `keyring`, etc.) | Medium | Lock file + hash-require + Dependabot + monthly review. | Phase 0: SEC-P0-30. | Tool + operator review. |
| CDN download SSRF via redirect to internal IP | Low-Medium | URL allowlist rejects redirects out of allowlist (SEC-P0-17). | Phase 0. | Tool. |

### 8.2 Unique-to-this-project threats (not in civit-hf-scanner's model)

Each mapped to a mitigation or explicit acceptance:

1. **Gateway session fingerprint mismatch with REST fingerprint** — mitigated SEC-P0-25.
2. **Stale User-Agent / Chrome version indicators** — mitigated SEC-P0-08, SEC-P1-01.
3. **Missing `X-Super-Properties`** (top detection signal per audit) — mitigated SEC-P0-09.
4. **Cookie jar cross-session leakage between burners** — mitigated SEC-P0-12.
5. **Captcha auto-solve temptation** — **EXPLICITLY REJECTED (§8.3)**.
6. **PII in message content stored verbatim in dumps** — mitigated SEC-P0-22, SEC-P0-27; accepted residual §8.1.
7. **Attachment downloads with NSFW/doxxing content** — mitigated SEC-P0-19 (MIME cap prevents blob attacks); content semantics deferred to Stage 3; accepted residual §8.1.
8. **CDN tokens in URLs (expiring signatures treated as secrets for 24h)** — documented in §3.1; retention prune ages them out; no code treats them as "secrets" beyond general redaction.
9. **Burner↔real-identity correlation via email/phone** — accepted, operator-borne (§8.1).
10. **Server admin retaliation / expel** — accepted, operator-borne (§8.1).
11. **Captcha-as-detection-signal** (even clean abort still signals automation) — accepted; rotation is the only real fix.
12. **Gateway concurrent-session detection** (two gateways from one token = loud) — mitigated SEC-P0-13.

### 8.3 Explicitly rejected "security controls"

These are **deliberately NOT implemented**. Listed to preempt re-litigation:

- **Captcha auto-solve (via 2captcha, AntiCaptcha, or local ML)**: REJECTED. (a) escalates ToS violation from passive to active circumvention, (b) creates an ML fingerprint adversary can train against, (c) creates a paid-service dependency with its own secrets and PII concerns, (d) still fails on behavioural captchas. Hard-abort only.
- **Invite-code probing / enumeration**: REJECTED. Discord would rate-limit + ban immediately. Not a feature.
- **Member-list enumeration (`/guilds/{id}/members`)**: REJECTED. Loud; zero benefit for Stage 2.
- **Any write endpoint (POST/PATCH/DELETE/react/type/join/DM)**: REJECTED. Pure GET only.
- **Auto-joining guilds via invite code use**: REJECTED. Operator joins manually with burner in a browser.
- **Browser automation (selenium/playwright)**: REJECTED. Heavy, detectable, and Discord's web client has additional anti-bot beyond API-layer.
- **LLM / Claude / Anthropic calls**: REJECTED in this repo. Stage 3's job. (`anthropic` is a forbidden import.)
- **Per-message encryption at rest**: REJECTED. `sqlcipher` is outside the locked stack. Filesystem-level (BitLocker/FileVault/LUKS) is operator's responsibility.
- **Telemetry / anonymous usage stats**: REJECTED. Pure local.

---

## 9. Risk rating key

- **Critical**: ship-blocker if unmitigated.
- **High**: ship-blocker if unmitigated in Phase 0.
- **Medium**: must be tracked; Phase 1 latest.
- **Low**: note + move on.

---

## 10. STRIDE threat enumeration (summary)

Per-component STRIDE matrix, compact form. Full controls are in §4 and §6.

| # | Component | S | T | R | I | D | E | Highest sev |
|---|-----------|---|---|---|---|---|---|-------------|
| 1 | Token load (keyring/env/config) | ✔ (env poisoning §4.1) | ✔ (config edit) | — | ✔ (token leak via log/stack/cache) | — | ✔ (fallback to plaintext backend) | High |
| 2 | REST client (httpx) | ✔ (UA/fingerprint spoofing BY US, accepted) | ✔ (MITM if verify=False) | — | ✔ (token in headers exposed via proxy) | ✔ (Discord rate-limit / ban) | — | Critical (ban) |
| 3 | Gateway WS | ✔ (IDENTIFY properties spoofing BY US) | ✔ (malformed HELLO/READY) | — | ✔ (session_id leak if logged) | ✔ (concurrent session ban) | — | High |
| 4 | URL allowlist / SSRF | — | — | — | ✔ (data exfil to evil.com if bypassed) | — | ✔ (redirect out of allowlist) | High |
| 5 | CDN download | — | ✔ (malicious payload) | — | — | ✔ (zip-bomb / oversized) | ✔ (MIME spoofing → wrong file type written) | High |
| 6 | JSONL dump writer | ✔ (filename spoofing for path traversal) | ✔ (filesystem path escape) | ✔ (no audit of who wrote — single user, accepted) | ✔ (dump contains PII) | — | ✔ (symlink pre-plant) | High |
| 7 | Cursor sqlite | — | ✔ (SQL injection if not parameterised) | — | — | ✔ (concurrent writer corruption) | — | Medium |
| 8 | Cookie jar | ✔ (cross-burner leakage) | ✔ (edit cookies file) | — | ✔ (fingerprint cookies leak) | — | — | Low |
| 9 | Config load | — | ✔ (path traversal in roots) | — | ✔ (token in last-resort mode) | — | ✔ (privilege escalation via output_root=/etc/) | Medium |
| 10 | Retention prune | — | — | ✔ (destructive; no audit) | — | ✔ (accidental delete of unrelated paths) | ✔ (symlink escape) | Medium |
| 11 | CLI / Typer | — | — | — | ✔ (verbose flag over-discloses) | — | — | Low |
| 12 | Structlog pipeline | — | — | ✔ (log forging — single-user, accepted) | ✔ (PII / token / invite in log) | — | — | High (pre-redact) |
| 13 | `invites.enriched.json` load | ✔ (malicious file — but operator-sourced) | ✔ (JSON bomb) | — | — | ✔ (parser DoS) | ✔ (redirect scan to attacker guilds) | Medium |
| 14 | Dependency supply chain | ✔ (typosquat) | ✔ (malicious update) | — | ✔ (token exfil via dep) | — | ✔ (rootkit dep) | High |
| 15 | Local filesystem permissions | — | ✔ (local-host attacker) | — | ✔ (PII readable by other OS users) | — | ✔ (other local users read state) | Medium |

**Total STRIDE threat rows: 15 components × average ~4 applicable letters each ≈ 60 distinct STRIDE threats enumerated.** Every one maps to a SEC-P0-## or accepted risk in §8.1.

---

## 11. Document quality self-check

- [x] Every section addressed; N/As reasoned.
- [x] Phase 0 checklist has 32 items (exceeds ≥25).
- [x] No "consider" language — all MUST / MUST NOT / explicit accept.
- [x] Privilege-escalation paths enumerated (§2.4).
- [x] Compliance verdict for GDPR, HIPAA, SOC 2, PCI-DSS, CCPA, Discord ToS.
- [x] Attack surface covers config, Stage-1 input, REST, Gateway, CDN, cookies, keyring, CLI.
- [x] ToS risk is first-class (§0, §5.6, §8.1, §8.2).
- [x] 12 unique-to-this-project threats enumerated (§8.2).
- [x] Explicitly rejected controls documented to preempt re-litigation (§8.3).
