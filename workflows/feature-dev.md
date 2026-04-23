# Workflow: Feature Development

A WAT-framework workflow for building a new feature end-to-end.

---

## Trigger

User says: "implement [feature name]", "add [feature]", "build [feature]", or similar.

---

## Steps

### 1. Read the spec

- Open `workplan.md` and find the phase + task(s) for this feature.
- Read `docs/claude/architecture.md` to understand which components are involved.
- Read `docs/claude/development.md` for the relevant "add route / add model / add page" recipe.
- If this feature touches auth or security, re-read the security section of `docs/claude/architecture.md`.

**Check before proceeding:** Is the previous phase fully complete (all ✅)? If not, flag this to the user — do not start new work on top of incomplete foundations.

---

### 2. Check workplan + branch

- Identify the exact workplan task(s) this feature maps to.
- Create a feature branch: `git checkout -b feat/<short-slug>`
- Never commit directly to `main`.

---

### 3. Backend — data layer first

If the feature needs new persistence:

1. Define / update the SQLAlchemy model.
2. Generate migration: `alembic revision --autogenerate -m "add <entity>"`.
3. Review the migration file — check for unintended destructive ops.
4. Apply locally: `alembic upgrade head`.
5. Write / update the repository class.
6. Write unit tests for the model and repository.

---

### 4. Backend — API layer

1. Create or update the route file (`src/api/routes/<resource>.py`).
2. Attach auth dependency (`Depends(require_auth)`) on all protected endpoints.
3. Validate all inputs with Pydantic schema — no raw dict access.
4. Return RFC 7807 errors for 4xx responses.
5. Register the router in `src/api/main.py` if it is new.
6. Write integration tests: happy path + at least one 401/403 case + one 422 validation error.

---

### 5. Frontend — if applicable

1. Add API call to `src/lib/api.ts`.
2. Create TanStack Query hook in `src/hooks/use<Resource>.ts`.
3. Build the page or component. Use existing shadcn/ui primitives first.
4. Wire up optimistic updates if the action is user-initiated (mutation with `onMutate` + `onError` rollback).
5. Sanitize any user-generated content rendered as HTML with DOMPurify.
6. Write a component test covering the loading, success, and error states.

---

### 6. Manual smoke test

Run the full local stack and manually verify:
- [ ] Feature works as expected (happy path)
- [ ] Unauthenticated request is rejected
- [ ] Invalid input returns a clear error (not a 500)
- [ ] No console errors in the browser
- [ ] Relevant workplan tasks are functionally complete

---

### 7. Update workplan

Immediately after the feature passes smoke test:
- Check off every completed `[ ]` item in `workplan.md`.
- If all items in a phase are now done, mark the phase ✅ and update the summary table.
- Do this in the same commit or a follow-up commit before opening the PR.

---

### 8. Open PR

```
gh pr create \
  --title "feat: <short description>" \
  --body "$(cat <<'EOF'
## What
<1-2 sentences on what this adds or changes>

## Why
<link to workplan phase/task or brief rationale>

## Test plan
- [ ] Unit tests pass: `pytest tests/unit -v`
- [ ] Integration tests pass: `pytest tests/integration -v`
- [ ] Frontend tests pass: `npm test`
- [ ] Manual smoke test completed (see Step 6 above)
- [ ] workplan.md updated

## Checklist
- [ ] No secrets committed
- [ ] All new routes have auth + input validation
- [ ] Migrations reviewed for destructive ops
EOF
)"
```

---

## Abort conditions

- Stop and ask the user if: the feature spec is ambiguous, the required data model conflicts with existing schema, or implementing this feature would require skipping a workplan security item.
- Never silently skip security requirements to ship faster.
