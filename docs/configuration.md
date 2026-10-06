# Configuration reference

All configuration is read from environment variables (`deploy/.env` with Docker Compose).

## Required

| Variable | Description |
|---|---|
| `DJANGO_SECRET_KEY` | Signs sessions and CSRF tokens. Long and random. |
| `REVIEWBOT_ENCRYPTION_KEYS` | Comma-separated Fernet keys. The first key encrypts and all keys decrypt. Back it up. |
| `POSTGRES_PASSWORD` | Password for the bundled Postgres (Compose only). |
| `REVIEWBOT_PUBLIC_URL` | Public base URL, e.g. `https://reviewbot.example.com`. Used for webhook URLs, OAuth redirects, CSRF, and links in PR comments. |

## Core

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | set by Compose | PostgreSQL URL. |
| `REDIS_URL` | set by Compose | Redis URL (cache, rate limiting, job queue). |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,api` | Extra accepted hostnames. The `REVIEWBOT_PUBLIC_URL` host is always allowed. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `REVIEWBOT_PUBLIC_URL` | Comma-separated origins allowed to submit forms. |
| `REVIEWBOT_SECURE_COOKIES` | `true` when the public URL is HTTPS | Marks cookies `Secure`. |
| `REVIEWBOT_BEHIND_PROXY` | `false` | Trust `X-Forwarded-Proto` from a TLS-terminating proxy. |
| `REVIEWBOT_SESSION_AGE_SECONDS` | `1209600` (14 days) | Dashboard session lifetime. |
| `REVIEWBOT_LOGIN_RATE` | `5/min` | Failed logins allowed per IP and email. |
| `DJANGO_DEBUG` | `false` | Never enable in production. |

## Git providers

| Variable | Default | Description |
|---|---|---|
| `GITHUB_URL` | `https://github.com` | GitHub web URL (change for GitHub Enterprise Server). |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API URL (`https://<host>/api/v3` for GHES). |
| `REVIEWBOT_GIT_TIMEOUT_SECONDS` | `30` | HTTP timeout for Git provider calls. |
| `REVIEWBOT_WEBHOOK_RETENTION_DAYS` | `30` | Webhook delivery log retention. |

## LLM

| Variable | Default | Description |
|---|---|---|
| `REVIEWBOT_LLM_TIMEOUT_SECONDS` | `180` | Per-request timeout. |
| `REVIEWBOT_LLM_MAX_RETRIES` | `4` | Retries for 429/5xx/connection errors. Exponential backoff that honors `Retry-After`. |
| `REVIEWBOT_ENABLE_FAKE_PROVIDER` | `false` | Adds an offline "Demo" provider that returns canned findings, for trying Reviewbot without a key. |

## Workers

| Variable | Default | Description |
|---|---|---|
| `CELERY_CONCURRENCY` | `4` | Concurrent tasks per worker container. |
| `CELERY_QUEUES` | `default,reviews` | Queues a worker consumes. Split them to dedicate workers to reviews. |
| `REVIEWBOT_REVIEW_TIME_LIMIT_SECONDS` | `900` | Hard time limit for one review run. |
| `REVIEWBOT_STUCK_RUN_MINUTES` | `30` | Running reviews older than this are marked failed by the reaper. |
| `REVIEWBOT_PUSH_DEBOUNCE_SECONDS` | `60` | Delay before reviewing a push; newer pushes within the window replace it. |
| `GUNICORN_WORKERS` | `3` | API processes. |

## Observability

| Variable | Default | Description |
|---|---|---|
| `LOG_FORMAT` | `json` | `json` or `console`. |
| `LOG_LEVEL` | `INFO` | Root log level. |
| `REVIEWBOT_METRICS_TOKEN` | empty | When set, `/metrics` requires `Authorization: Bearer <token>`. |

## Per-repository settings (dashboard)

| Setting | Default | Effect |
|---|---|---|
| Auto-review | on | Review when a PR is opened, reopened, or marked ready for review. |
| Review new commits | on | Review pushes to open PRs incrementally (only changes since the last reviewed commit). |
| Review profile | balanced | `strict`, `balanced`, `lenient` (no style), or `security` (security findings only). Sets the prompt focus and pre-fills the thresholds. |
| Check run | on | Report a "Reviewbot" check per reviewed commit (needs the Checks permission). |
| Fail the check at | never | Severity at or above which the check concludes `failure`. |
| Review drafts | off | Also review draft PRs. |
| LLM key / model override | first valid key / key default | Which credential and model to use. |
| Minimum severity | `low` | Findings below it are stored but not posted. |
| Minimum confidence | `0.5` | Findings below it are stored but not posted. |
| Max inline comments | `25` | The highest-severity findings are posted inline; the rest are counted in the summary. |
| Custom instructions | empty | Up to 4,000 characters appended to the system prompt. |
| Ignore patterns | built-in defaults | Gitignore syntax, `!pattern` re-includes. Optionally replaces the defaults. |
| Max changed lines / files / input tokens | 2,000 / 100 / 150,000 | Above these limits a PR is skipped with a comment and no LLM call (cost guard). |
| Tokens per LLM request | 12,000 | Chunk size for large diffs. |
| Post summary when nothing is found | on | Posts "No issues found." |
