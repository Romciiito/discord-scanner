# Workflow: Bug Fix

A WAT-framework workflow for diagnosing and fixing a reported bug.

---

## Trigger

User reports: a crash, an unexpected behavior, a failing test, a 5xx error, a data integrity issue, or similar.

---

## Steps

### 1. Reproduce the bug

Before touching any code:

1. Read the bug report carefully. Extract:
   - What the user did (steps to reproduce)
   - What they expected
   - What actually happened (error message, stack trace, incorrect output)
2. Reproduce locally. Do not proceed to fix until you can trigger the bug yourself.
3. If the bug is intermittent or environment-specific, document the reproduction conditions.

**If you cannot reproduce:** Ask the user for more context (logs, env, steps) rather than guessing at a fix.

---

### 2. Root cause analysis

Work from the symptom inward:

1. **Read the error.** Full stack trace > last line. Find the first frame in project code (skip library internals).
2. **Trace the data flow.** Follow the request/event from entry point (route / event handler / CLI command) to the failing line.
3. **Check recent changes.** Run `git log --oneline -20` and `git diff <suspect-commit>` — most bugs live close to recent edits.
4. **Check the logs.** Structured logs with correlation IDs often show what happened before the crash.
5. **Isolate assumptions.** State the assumption you think is wrong, then verify it with a targeted read of the relevant code.

Do not write a fix until you can state the root cause in one sentence.

---

### 3. Write a regression test first

Before fixing, write a test that:
- Reproduces the bug (it must fail on the current code)
- Will pass once the fix is in place
- Is as narrow as possible (unit test preferred; integration test if the bug requires I/O)

```python
# Example shape
def test_<thing>_does_not_<bad_behavior>_when_<condition>():
    # Arrange
    ...
    # Act
    result = <call that triggered the bug>
    # Assert
    assert result == <expected>, "was broken by <root cause>"
```

Commit the failing test on a separate commit (or at minimum confirm it fails) before writing the fix. This proves the test actually catches the bug.

---

### 4. Fix

1. Create a branch: `git checkout -b fix/<short-slug>`.
2. Make the minimal change that fixes the root cause. Resist the urge to refactor unrelated code in the same commit — it obscures the diff during review.
3. Confirm the regression test now passes.
4. Confirm no previously passing tests now fail: run the full test suite.
5. If the bug was a security issue (auth bypass, injection, data leak), immediately check for the same pattern in related code — bugs of a type often appear in clusters.

---

### 5. Manual verification

Run the full local stack and verify:
- [ ] The reported bug no longer occurs
- [ ] The fix does not break adjacent functionality
- [ ] Edge cases around the fix behave correctly (empty input, boundary values, concurrent requests)
- [ ] No new console errors or warnings introduced

---

### 6. Update workplan

If this bug was tracked in `workplan.md`:
- Check off the corresponding task.
- If the fix completes a phase, mark it ✅ and update the summary table.

If the bug was not in the workplan but reveals a gap (missing validation, missing test coverage), add a task to the relevant phase now.

---

### 7. Open PR

```
gh pr create \
  --title "fix: <one-line description of what was wrong>" \
  --body "$(cat <<'EOF'
## Root cause
<One sentence: what was wrong and why>

## Fix
<One sentence: what the change does>

## Regression test
`tests/path/to/test_<thing>.py::test_<specific_test>`

## Test plan
- [ ] Regression test passes: `pytest tests/path/to/test_<thing>.py -v`
- [ ] Full test suite passes
- [ ] Manual verification completed (see Step 5 above)
- [ ] workplan.md updated if applicable

## Risk
<low / medium / high> — <reason>
EOF
)"
```

---

## Abort conditions

- Stop and ask the user if: the root cause requires a breaking schema change, the fix would require disabling a security control, or the bug is in a third-party library with no safe workaround.
- Do not ship a fix that trades one bug for a security regression.
