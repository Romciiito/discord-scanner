# Acceptance criteria → tests mapping

The 15 acceptance criteria from [spec.md](../../spec.md) §11 (verbatim from
seed-spec §10) are the merge-bar for ship. This file maps each criterion to
the test(s) that verify it.

Last verified: 2026-04-25 (Phase 11).

| # | Criterion | Verifying tests | Status |
|---|---|---|---|
| 1 | `pip install -e ".[dev]"` succeeds on Python 3.12 (Windows + Linux CI) | `.github/workflows/ci.yml` matrix — install step | green |
| 2 | `ruff check` / `ruff format --check` / `mypy --strict src` / `pytest --cov` all green | `.github/workflows/ci.yml` job steps | green |
| 3 | `discord-scanner --help` shows all 8 commands + global flags | `tests/test_cli.py::test_help_lists_all_commands` (and adjacents) | green |
| 4 | `store-token` prompts via getpass + keyring round-trip + `list-guilds` reads it | `tests/test_cli.py::test_store_token_mocked_keyring` + `tests/test_cli.py::test_logs_never_contain_raw_token` + `tests/test_auth.py::test_load_token_keyring_first` | green |
| 5 | `resolve --invite aaaabbbb` returns guild from a mocked invite endpoint | `tests/test_discovery.py::test_resolve_invite_happy_path` (now also asserts cache hit issues no second call) | green |
| 6 | `scan --dry-run` prints planned actions, makes zero HTTP calls | `tests/test_cli.py` dry-run path | green |
| 7 | Mocked live scan: 1 guild × 2 channels × 30 messages → writes JSONL.zst + auxiliaries; line count matches | `tests/e2e/test_smoke.py::test_full_pipeline_offline` (uses respx + 50 messages — exceeds threshold) | green |
| 8 | Gateway WebSocket mock test: IDENTIFY → HELLO → HEARTBEAT schedule verified | `tests/test_gateway.py::test_handshake_emits_identify_then_presence` + `tests/test_gateway.py::test_heartbeat_loop_sends_on_interval` + `tests/test_gateway.py::test_identify_properties_equal_rest_xsp_dict` | green |
| 9 | Idempotence: two consecutive scans → byte-identical decompressed JSONL | `tests/test_dump.py::test_two_runs_produce_byte_identical_jsonl` + `tests/e2e/test_smoke.py` post-scan invariant block (lines 288–296) | green |
| 10 | `scan --offline` inspects cursor without HTTP/WS | `tests/test_cli.py::test_status_offline_no_network` (and adjacents) | green |
| 11 | Cursor sqlite contains correct per-channel `last_message_id` after scan | `tests/test_cursor.py::test_cursor_advance_then_get` + `tests/e2e/test_smoke.py` cursor-advance assertion | green |
| 12 | Full Discord header set verified on every mocked REST request | `tests/test_rest_headers.py::test_full_header_set_on_every_request` | green |
| 13 | Grep guards: no forbidden imports / raw token / `verify=False` / raw invite in logs / sync `time.sleep` in async | `tests/ci/test_grep_guards.py` (10+ guard tests) | green |
| 14 | Schema-drift tolerance: extra field on a message → pydantic accepts (`extra='allow'`) | `tests/test_fetch.py` + `tests/test_dump.py` (model uses `ConfigDict(extra="allow")`) | green |
| 15 | CDN image download writes file with correct path/filename; non-image attachments record URL only | `tests/test_attachments.py::test_happy_path_writes_image` + `tests/test_attachments.py::test_mime_mismatch_returns_url_only` + E2E smoke attachment block | green |

## Phase 11 follow-ups (deferred — not merge-blockers)

- **TODO-P1-01** (SEC-P1-01): monthly Chrome-UA probe job. The compiled-in
  `_MIN_PLAUSIBLE_CHROME_MAJOR` floor is enforced today; the dynamic probe
  (`https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Windows`)
  is a Phase 12 hardening item.
- **SBOM on release tag**: `cyclonedx-py` step is sketched in
  `.github/workflows/ci.yml` but only runs on tag. First release will exercise it.
