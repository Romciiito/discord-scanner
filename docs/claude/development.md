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

