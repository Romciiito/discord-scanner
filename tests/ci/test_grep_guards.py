"""CI grep merge-blockers.

Traces to: security-model.md §6 (SEC-P0-04, SEC-P0-29), claude-rules.md MUST-NOT list.

These tests fail the build if any forbidden pattern appears under `src/`.
Running locally: `pytest tests/ci/test_grep_guards.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
TESTS_FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _iter_py(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize(
    "forbidden",
    [
        "import requests",
        "import aiohttp",
        "import discord",
        "import anthropic",
        "from selenium",
        "from playwright",
        "verify=False",
        "import fcntl",
        "from fcntl",
    ],
)
def test_no_forbidden_imports_in_src(forbidden: str) -> None:
    """SEC-P0-29: no forbidden libs or TLS-disable in src/."""
    if not SRC_ROOT.exists():
        pytest.skip("src/ not yet scaffolded")
    hits: list[str] = []
    for path in _iter_py(SRC_ROOT):
        text = path.read_text(encoding="utf-8")
        if forbidden in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert not hits, f"forbidden pattern {forbidden!r} found in: {hits}"


def test_no_time_sleep_in_async_def() -> None:
    """SEC-P0-29: `time.sleep` inside `async def` blocks the event loop."""
    if not SRC_ROOT.exists():
        pytest.skip("src/ not yet scaffolded")
    offenders: list[str] = []
    for path in _iter_py(SRC_ROOT):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"async def [^\n]+:", text):
            block_start = match.end()
            indent_m = re.search(r"\n( +)\S", text[block_start:])
            if not indent_m:
                continue
            indent = indent_m.group(1)
            lines = text[block_start:].splitlines()
            body: list[str] = []
            for line in lines[1:]:
                if line.strip() == "":
                    body.append(line)
                    continue
                if not line.startswith(indent):
                    break
                body.append(line)
            body_text = "\n".join(body)
            if re.search(r"\btime\.sleep\(", body_text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
                break
    assert not offenders, f"time.sleep inside async def in: {offenders}"


def test_no_raw_discord_invite_url_in_log_call() -> None:
    """SEC-P0-29: no raw `discord.gg/` / `discord.com/invite/` literal in any log call."""
    if not SRC_ROOT.exists():
        pytest.skip("src/ not yet scaffolded")
    log_call_re = re.compile(
        r"(?:logger|log|structlog|structlog\.get_logger\(\))\s*\."
        r"(?:debug|info|warning|error|critical|exception)\s*\("
        r"[^\)]*(?:discord\.gg/|discord\.com/invite/)",
        re.IGNORECASE | re.DOTALL,
    )
    offenders: list[str] = []
    for path in _iter_py(SRC_ROOT):
        text = path.read_text(encoding="utf-8")
        if log_call_re.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"raw invite URL in log call in: {offenders}"


DISCORD_TOKEN_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}\b"
)


def test_no_raw_discord_token_pattern() -> None:
    """SEC-P0-04: no raw Discord token pattern in src/ or tests/fixtures/."""
    roots = [r for r in (SRC_ROOT, TESTS_FIXTURES) if r.exists()]
    if not roots:
        pytest.skip("no src/ or tests/fixtures/ yet")
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in {".pyc", ".sqlite", ".zst"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue
            if DISCORD_TOKEN_PATTERN.search(text):
                # allow the pattern itself inside this test file + logging_conf
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel in {
                    "tests/ci/test_grep_guards.py",
                    "src/discord_scanner/logging_conf.py",
                }:
                    continue
                offenders.append(rel)
    assert not offenders, f"raw Discord token pattern in: {offenders}"


def test_gitignore_covers_sensitive_roots() -> None:
    """SEC-P0-28, SEC-P0-32: .gitignore covers .env, output/, state/, attachments/."""
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for required in (".env", "output/", "state/", "attachments/", "*.sqlite"):
        assert required in gi, f".gitignore missing: {required}"
