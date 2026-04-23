# discord-scanner — Environment Variables

Read this doc before: adding a new config value, debugging a missing env var, or setting up a new environment (staging, CI, production).

All variables use the `DISCORD_SCANNER_` prefix unless noted otherwise.

Copy `.env.example` to `.env` and fill in all **required** values before running locally.

---

## Quick Reference

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|

| `JWT_SECRET` | ✅ | — | Min 256-bit random key; never commit |
| `ENVIRONMENT` | ✅ | `development` | `development` / `staging` / `production` |

| `LOG_LEVEL` | — | `info` | `debug` / `info` / `warning` / `error` |
| `SENTRY_DSN` | — | — | Omit to disable Sentry |

---



## Auth / JWT

```dotenv
# Signing key — generate with: openssl rand -hex 32
JWT_SECRET=replace-with-32-byte-hex

# Token lifetimes (optional — defaults shown, in seconds)
JWT_ACCESS_TTL=900       # 15 min
JWT_REFRESH_TTL=604800   # 7 days
```

| Variable | Required | Notes |
|----------|----------|-------|
| `JWT_SECRET` | ✅ | Min 256 bits; rotate requires all active sessions to re-login |
| `JWT_ACCESS_TTL` | — | Short enough to limit breach window |
| `JWT_REFRESH_TTL` | — | Long enough for UX; refresh tokens are revocable via JTI |

---

## External APIs

```dotenv
# OpenAI-compatible AI provider
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1   # override for Groq / local Ollama
OPENAI_MODEL=gpt-4o-mini



# Email provider (optional — omit to disable transactional email)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=noreply@example.com
```

| Variable | Required | Notes |
|----------|----------|-------|
| `OPENAI_API_KEY` | Depends | Required if any AI features enabled |
| `OPENAI_BASE_URL` | — | Override to use Groq, Together, or local Ollama |
| `SMTP_*` | — | All optional; omit block to disable email |

---

## Application

```dotenv
ENVIRONMENT=development   # development | staging | production
LOG_LEVEL=info            # debug | info | warning | error


```

| Variable | Required | Notes |
|----------|----------|-------|
| `ENVIRONMENT` | ✅ | Gates feature flags, debug panels, strict CORS |
| `LOG_LEVEL` | — | `debug` adds noisy request tracing; use `info` in production |


---

## Feature Flags

```dotenv
# Set to "true" / "false" (string) — evaluated at startup
FEATURE_AI_ENABLED=true
FEATURE_EMAIL_ENABLED=false
FEATURE_ANALYTICS_ENABLED=true
```

Feature flags live in env rather than a database to avoid a cold-start chicken-and-egg problem (the feature flag system needs the DB to be up).

---

## Observability

```dotenv
# Sentry — omit DSN to disable error tracking entirely
SENTRY_DSN=https://...@sentry.io/...
SENTRY_TRACES_SAMPLE_RATE=0.1    # 10% of transactions; reduce in high-traffic prod
SENTRY_ENVIRONMENT=production    # defaults to ENVIRONMENT if omitted
```

| Variable | Required | Notes |
|----------|----------|-------|
| `SENTRY_DSN` | — | Empty string disables Sentry SDK silently |
| `SENTRY_TRACES_SAMPLE_RATE` | — | Float 0.0–1.0; set lower in high-volume production |

---

## Adding a New Variable

1. Add it to `.env.example` with a placeholder value and an inline comment explaining what it's for.
2. Add it to the relevant section in this file (required/optional, type, notes).
3. Load it in `src/core/config.py` (or equivalent settings module) — never read `os.environ` directly in application code.
4. If it's required, add a startup check that raises `ValueError` with a clear message when it's missing.
