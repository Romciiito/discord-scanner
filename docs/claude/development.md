# discord-scanner — Development Guide

Read this doc before: running the project locally, writing a test, adding a route/model/page, or deploying.

---

## Prerequisites


- **Python 3.12+** — `python3 --version`
- **uv** (recommended) or pip — `pip install uv`




- Copy `.env.example` to `.env` and fill in all required values (see `docs/claude/env-vars.md`)

---

## Running Locally


```bash
# 1. Install dependencies
uv sync              # or: pip install -e ".[dev]"

# 2. Run CLI
uv run discord-scanner --help
```



---

## Running Tests


### Backend

```bash

uv run pytest tests/ -v
uv run pytest tests/ -m unit -v          # unit tests only


**Test layout:**
```
tests/
  unit/           — pure logic, no I/O, no DB (fast)
  integration/    — real DB + Redis, use fixtures for setup/teardown
  conftest.py     — shared fixtures (async session, test client, factories)
```




---

## Common Tasks






### Add a CLI command

1. Add a new `@cli.command()` in `src/cli.py` (or a sub-group file)
2. Use `typer` types for arguments and options — never `sys.argv`
3. Add a test in `tests/test_cli.py` using `typer.testing.CliRunner`


---

## Deployment








### Package / distribute

```bash
uv build               # produces dist/*.whl + dist/*.tar.gz
pipx install dist/*.whl  # local install for testing
```

---

## Third-party PII handling and operator obligations

Traces to: security-model.md §6 (SEC-P0-27), §8 (accepted risks).

`discord-scanner` captures **public Discord message content produced by third-party
users who have not consented to the operator's collection**. The following applies
to every operator who runs this tool:

1. **The scraped content is personal data.** Message bodies, author usernames,
   display names, avatar URLs, embeds, and attachments are personal data under
   GDPR, CCPA, UK DPA, and most equivalent statutes. The operator — not the
   maintainers of this tool — is the **data controller** for any data stored on
   their workstation.
2. **Lawful-basis balancing is the operator's responsibility.** This tool
   performs no lawful-basis assessment. The operator MUST perform their own
   legitimate-interest balancing test (or obtain consent) before scanning any
   guild. If you cannot articulate a specific, narrow, documented purpose, do
   not run the tool.
3. **Prohibited guild classes.** The operator MUST NOT scrape:
   - medical, mental-health, therapy, or patient-support guilds;
   - addiction recovery or crisis-support guilds;
   - minors-focused guilds;
   - any guild where the community has an explicit no-archive / no-screenshot rule.
   These exclusions apply even if the guild is nominally public.
4. **`attachments/` may contain unsafe material.** Attachments include whatever
   users have posted — possibly NSFW, doxxing content, malware samples, or CSAM
   in the worst case. The operator MUST:
   - keep `attachments/` on an encrypted filesystem,
   - restrict OS-level access (`chmod 0700` on Linux/macOS; NTFS ACL on Windows),
   - never mirror to cloud storage that scans files automatically,
   - delete promptly if unsafe content is encountered (and report CSAM to NCMEC
     or the local equivalent per jurisdiction).
5. **Discord ToS acceptance.** Running this tool against Discord's REST or
   gateway endpoints with a user token violates Discord's Terms of Service.
   Security-model.md §8 documents this as an **accepted risk**. Possible
   consequences include: burner account ban, email hash blacklist, IP
   blacklist, and admin retaliation. The operator accepts these consequences
   by running the tool.
6. **Cross-platform chmod caveat (SEC-P0-22).** On Linux/macOS, `os.chmod(path,
   0o600)` is enforced. On Windows there is no POSIX permission equivalent;
   the implementation calls `os.chmod` on a best-effort basis (the read-only
   bit) and relies on the operator to place `state/` and `output/` inside a
   user-profile folder whose NTFS ACL already restricts access to the current
   user. If running on a shared Windows workstation, use BitLocker or an
   encrypted folder.
7. **Data-subject rights.** If a Discord user contacts you requesting erasure,
   access, or rectification of their data, you MUST honour it. This tool
   provides no automation for rights requests — operate manually:
   `grep -r <user_id> output/ state/ attachments/` and delete all hits.

If any of the above feels out of scope for your use case, the correct response
is to not run the tool.

---

## Runbooks

These are the operator playbooks for the four recurring incidents that have
opinionated responses. Treat them as load-bearing — not advisory.

### Runbook 1: Burner rotation (planned)

When to rotate: every 60–90 days under normal use, OR after any captcha event,
OR after any 401 on `/users/@me/guilds`, whichever comes first.

```bash
# 1. On a clean browser profile (do NOT reuse the previous burner's profile):
#    create a new Discord account, manually join every target guild, and
#    let the account "warm" for 7+ days with normal browsing activity. The
#    rotation MUST happen on a different IP than the burner being retired.

# 2. Update the keyring under a NEW username so cookies stay separate.
#    Bump auth.keyring_username in config.yaml first (e.g. burner-2 → burner-3),
#    then store the new token.
discord-scanner store-token

# 3. Verify the new burner is alive before retiring the old one.
discord-scanner list-guilds                  # should print the joined guilds
discord-scanner resolve --invite <one-code>  # should resolve

# 4. Retire the old burner: archive its `state/cookies-{old}.json` and
#    `state/gateway-{old}.lock` (move out of state/, do NOT delete — keep
#    for forensic timeline if a ban comes later).
mv state/cookies-burner-2.json archive/
mv state/gateway-burner-2.lock archive/

# 5. First scan with the new burner: use a small `max_messages_per_scan`
#    in config.yaml so a fingerprint divergence surfaces fast.
discord-scanner scan --config config.yaml --dry-run
discord-scanner scan --config config.yaml
```

Do NOT migrate the cursor sqlite across burners; it is keyed to the previous
burner's snowflake IDs. A fresh burner re-walks history from scratch.

### Runbook 2: Token compromise response

When triggered: the burner token has been written to a log, pasted into a chat,
appears in a public diff, or is otherwise off the workstation.

```bash
# 1. STOP the daemon immediately if running. From the terminal that owns it:
#    Ctrl-C. The daemon completes the current channel's dump + cursor commit
#    and exits clean (SIGINT handler).

# 2. Invalidate the token in Discord's web UI:
#    User Settings → My Account → Password → Change → save with a new password.
#    Discord cycles the user token on password change; the old token becomes 401.

# 3. Wipe local copies of the old token.
discord-scanner store-token   # overwrites the keyring entry
# Or, if you'd rather purge entirely:
keyring del discord-scanner burner-1   # adjust username

# 4. Audit the disk for stray copies. The redaction processors prevent
#    structlog output from containing the raw token, but defence-in-depth:
grep -rE '[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}' \
    state/ output/ logs/ 2>/dev/null
# Expected: zero matches.

# 5. Plan a burner rotation (Runbook 1) within 7 days. Treat the compromised
#    burner as half-burned even after password reset — Discord may have
#    already correlated the old token with your fingerprint.
```

### Runbook 3: Captcha response

When triggered: `discord-scanner` exits with code 2 and a log line like
`captcha_aborted opcode=403 keys=['captcha_key', 'captcha_sitekey']`.

```bash
# 1. STOP all scans immediately. Do NOT attempt to retry — the captcha was
#    raised because Discord flagged the burner's REST traffic. Retrying
#    accelerates the ban.

# 2. Check the burner's status manually in a browser (different IP if possible):
#    https://discord.com/login → log in with the burner's email/password.
#    If the login page shows a captcha, complete it ONCE, by hand, in the
#    browser. Do NOT screen-scrape, do NOT use 2captcha — auto-solving is a
#    merge-blocker for this codebase and a fast path to permanent ban.

# 3. Let the burner cool. 24-72 hours of zero scanner traffic, ideally with
#    occasional manual browser activity from a residential IP. Discord's
#    flag-window expires; scraper traffic on a flagged session does not.

# 4. Audit before resuming: rotate the burner (Runbook 1) if any of these
#    apply:
#    - Two captcha events within a 14-day window.
#    - Captcha + 401 on the same day.
#    - The burner's email is shared with another flagged account.

# 5. Resume only after a successful manual `discord-scanner list-guilds`
#    against the same burner. Start with `scan --dry-run`, then a single
#    guild, then full daemon mode.
```

### Runbook 4: Cross-platform `chmod` degradation

The codebase calls `os.chmod(path, 0o600)` for files and `0o700` for directories
under `state/` and `output/`. POSIX honours these. Windows does not — the call
silently sets only the read-only bit, and the directory remains readable to
other local users by default.

When the operator runs on Windows:

1. Place the project under a per-user profile path (e.g.
   `C:\Users\<you>\code\discord-scanner`). NTFS ACL on `C:\Users\<you>` already
   restricts access to the current user.
2. Enable BitLocker on the disk so at-rest encryption protects `state/cookies-*.json`
   and the cursor sqlite.
3. If using a shared workstation, place `state/` and `output/` inside an
   encrypted folder (VeraCrypt, 7-Zip-encrypted volume, etc.) instead of at
   project root.
4. Periodically audit:
   ```powershell
   icacls state output
   ```
   Anything other than the current user + SYSTEM in the ACL is a finding.

The `secure_mkdir` helper (`src/discord_scanner/_paths.py`) logs a DEBUG-level
`secure_mkdir_chmod_failed` event when chmod can't apply — this is expected on
Windows and is not an error condition.

### Runbook 5: First live run on a low-stakes guild (Phase 12.c–12.f)

This runbook walks the operator through the very first live scan against a
real Discord guild — the milestone that closes Stage 2 and produces the
fixture seed Stage 3 needs. It assumes Phase 11 quality gates are green and
Phase 12.a/12.b (Chrome-UA probe + SBOM) have been completed.

**Pre-conditions:**
- Burner token already stored (`discord-scanner store-token` from Phase 11).
- Target guild has been manually joined ≥ 7 days ago (Runbook 1 hygiene).
- Encrypted filesystem (FileVault / BitLocker / LUKS) is on.

#### Step 1: Pre-flight (Phase 12.c)

```bash
# Copy the gitignored template, fill in the slots.
cp .tmp/preflight.md.template .tmp/preflight.md
# Open .tmp/preflight.md in your editor — work top to bottom; tick every
# checkbox. Do NOT run the scan until every self-check is honestly green.
```

The template is at `.tmp/preflight.md.template` and is exempt from the
`.tmp/` ignore via `!.tmp/preflight.md.template` in `.gitignore`. The
filled-in `.tmp/preflight.md` stays gitignored — guild IDs, invite codes,
and friendly names never leave your workstation.

```bash
# Copy the smoke config example to the working filename and edit.
cp config.live.yaml.example config.live.yaml
# Replace <INVITE_CODE_HERE> with the real invite code under
# discovery.manual_invites. Leave everything else at the smoke defaults
# (max_messages_per_scan: 200, attachments.max_size_mb: 5, intent_allowlist: []).
```

```bash
# Final dry-run smoke — zero network calls, exits 0 if config parses.
discord-scanner scan --config config.live.yaml --guild <GUILD_ID> --dry-run
```

If the dry-run prints the expected plan (smoke caps + your guild filter),
proceed.

#### Step 2: Live scan (Phase 12.d) — two-terminal monitoring

Open two terminals before invoking the live scan:

```bash
# Terminal A — the actual scan. Structured logs stream to stdout.
discord-scanner scan --config config.live.yaml --guild <GUILD_ID>
```

```bash
# Terminal B — offline cursor inspection. Re-run between cycles to watch
# per-channel last_message_id populate. The --offline flag means it does
# not touch the network or contend on the cursor write lock.
watch -n 30 'discord-scanner status --offline --config config.live.yaml | head -40'
```

The scan typically completes in 5–15 minutes for a small guild with the
smoke caps in place. Watch terminal A for the structured-log shape; watch
terminal B for cursor progress.

#### Step 3: Hard-stop conditions

If any of the following appear in terminal A's output, **abort the scan**
(Ctrl-C in terminal A — the SIGINT handler completes the current channel
cleanly) and follow the linked runbook:

- `captcha_aborted` event or exit code 2 → **Runbook 3 (Captcha response)**.
  Do NOT retry. The burner cools for 24–72 hours minimum.
- `401` from `/users/@me/guilds` or any list-guilds-style endpoint →
  **Runbook 2 (Token compromise response)**. Token is dead or revoked.
- Repeated `gateway_invalid_session` (more than 2 in a single run) →
  fingerprint divergence likely; abort, capture the run's structured logs,
  and inspect `HttpSettings.fingerprint()` parity with the actual `IDENTIFY`
  payload sent on the gateway. Do NOT blindly rotate the burner — diagnose
  first.
- Any `ERROR`-level structlog event you cannot explain — same posture: stop,
  diagnose, then resume.

Soft signals (do NOT abort, but note them in `decisions.md`):

- `http_429_seen` rising fast (> 5% of `messages_fetched`) — the per-host
  bucket may need tightening. Carry forward into Phase 12.5 tuning.
- `attachments_skipped_mime` non-zero — expected on guilds with mixed
  attachment types; record the count for the Stage 3 fixture decisions.

#### Step 4: Post-run inspection

When the scan exits 0, walk the artefact set:

```bash
# File presence — every file in the contract must exist.
TODAY=$(date -u +%F)
ls -la "output/<GUILD_ID>/${TODAY}/"
# Expect:  messages.jsonl.zst  pinned.jsonl  threads.jsonl  meta.json  prior.txt  attachments/
```

```bash
# meta.json counters in plausible ranges.
jq '.counters' "output/<GUILD_ID>/${TODAY}/meta.json"
```

Counter sanity (all from PROJECT_WORKPLAN.md §2.12.d):

- `channels_scanned > 0`
- `messages_fetched > 0` (and ≤ `max_messages_per_scan` = 200 for the smoke)
- `http_429_seen` low: under 5% of `messages_fetched`
- `channel_aborts == 0`
- `attachments_skipped_mime` recorded (any non-negative integer)

```bash
# At least one image landed and is structurally valid.
ls "output/<GUILD_ID>/${TODAY}/attachments/" | head
file "output/<GUILD_ID>/${TODAY}/attachments/"* | head
# Expect: "PNG image data, ..." / "JPEG image data, ..." / "RIFF (little-endian)
# data, Web/P image" / etc. — the file utility is the cross-check that the
# MIME-sniff guard caught the right bytes.
```

```bash
# Cursor populated per channel.
discord-scanner status --offline --config config.live.yaml
```

```bash
# Negative scrub — no captcha / no 401 / no ERROR-level slipped through silently.
# (If the scan exit code was 0 and terminal A was clean, this is belt-and-braces.)
grep -E 'captcha_aborted|gateway_invalid_session|"level": "error"' \
    "output/<GUILD_ID>/${TODAY}/"*.json* 2>/dev/null || echo "clean"
```

**Idempotence test (mandatory):** re-run the same scan immediately:

```bash
discord-scanner scan --config config.live.yaml --guild <GUILD_ID>
```

The cursor is already at the latest `last_message_id`, so the second pass
should be a no-op walk. Verify byte-for-byte parity of the decompressed
JSONL (sort-stability invariant from CLAUDE.md):

```bash
# Save the first run's decompressed bytes, then compare after the second run.
cp "output/<GUILD_ID>/${TODAY}/messages.jsonl.zst" /tmp/run1.jsonl.zst
discord-scanner scan --config config.live.yaml --guild <GUILD_ID>
diff <(zstd -dc /tmp/run1.jsonl.zst) <(zstd -dc "output/<GUILD_ID>/${TODAY}/messages.jsonl.zst")
# Expect: zero output (byte-identical decompressed). Any diff is a determinism
# regression — file a decisions.md entry.
```

#### Step 5: Stage 3 fixture seed (Phase 12.e)

Pick a small, redactable slice from the live output and copy it into
`tests/fixtures/stage3_input/` for the Stage 3 curator's smoke tests.

Selection:
- 5 messages, picked for variety (one with mentions, one with embed, one
  with attachment reference, two plain).
- 1 pinned message.
- 1 attachment file (the smallest valid image from `attachments/`).

Redaction recipe (apply to the selected slice, NOT the source `output/`):

| Field | From (real) | To (redacted) |
|-------|-------------|---------------|
| `author.id`, `mentions[].id` | snowflake | `u_0`, `u_1`, … (0-indexed, deterministic per slice) |
| `author.username`, `author.global_name` | real handle | `user_0`, `user_1`, … (matching the user index above) |
| `author.avatar`, `author.discriminator` | real | drop or set to `null` |
| Attachment URL on `attachments[].url` and `proxy_url` | `https://cdn.discordapp.com/attachments/...` | `https://cdn.discordapp.com/redacted/{n}` |
| Attachment filename on disk | `{msg_id}_realname.png` | `{msg_id}_test.png` (rename the file too) |
| Guild ID in path | real snowflake | `g_fixture` (rename the directory) |
| Timestamps, message IDs, channel IDs, schema_version | real | **keep intact** — preserves sort order + structural shape |

Output destination layout:

```
tests/fixtures/stage3_input/
└── g_fixture/
    └── 2026-04-25/
        ├── messages.jsonl.zst    # 5 redacted messages, decompressed-equal across runs
        ├── pinned.jsonl          # 1 redacted pinned
        ├── threads.jsonl         # may be empty list "[]\n" if guild has no threads
        ├── meta.json             # counters reset to fixture values; schema_version=1
        ├── prior.txt             # cursor snapshot — redact channel_ids if you want, but optional
        └── attachments/
            └── 1000000000000000_test.png
```

After the redacted slice is in place, scaffold a tiny contract test at
`tests/test_stage3_contract.py` that asserts: files exist, `messages.jsonl.zst`
decompresses, `meta.json["schema_version"] == 1`, message count == 5. This
test is the canary that proves the on-disk contract Stage 3 will read against
is committed and stable.

Document the fixture in `tests/fixtures/README.md` so future engineers
understand the redaction was deliberate.

#### Step 6: Lessons capture (Phase 12.f)

After the scan completes (success or hard-stop), capture observations in two
places:

1. **`decisions.md` Phase 12 addendum.** Append a new dated section. Cover:
   - Anything Discord did that surprised you — 429 rates higher than the
     2 req/s bucket assumed, gateway resume behaviour, message ordering
     edge cases, attachment-CDN quirks.
   - Real-world `meta.json` counter values. These become the baseline for
     future runs; if a future scan's counters diverge by > 20%, the
     anomaly is investigable.
   - Anything in the rate-limit / fingerprint model that needs tuning.
     If `http_429_seen` was > 5%, recommend tightening
     `http.per_host_rate_per_sec["discord.com/api"]` from 2.0 to 1.5 in
     Phase 12.5.
2. **`CLAUDE.md` Critical Gotchas.** Add an entry only if something
   genuinely surprising appeared — a non-obvious foot-gun a future Claude
   session would predictably step on. Keep the gotcha one or two lines.
   Do NOT mirror routine observations here; that bloats the always-loaded
   context. The `decisions.md` addendum is for the long version.

Once both updates land and the Stage 3 fixture is committed, Phase 2.12 is
done — tick the boxes in `workplan.md` and `PROJECT_WORKPLAN.md`, and
Stage 2 is shipped.

