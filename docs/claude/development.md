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

