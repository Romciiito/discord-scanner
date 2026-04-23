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

