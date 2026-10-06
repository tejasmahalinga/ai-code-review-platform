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
| Base branches | all | Globs such as `main` or `release/*`. Auto-reviews only run for PRs into matching branches; comment commands always work. |
| Rules | none | Team rules: id, description, severity, and optional path globs (see below). |
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

## Review rules

Rules are short, enforceable team conventions. A rule is added to the prompt only for chunks that contain files
matching its `paths` (gitignore-style globs; empty means every file). When the model reports a finding that
violates a rule, the finding is tagged with the rule id and raised to at least the rule's severity. Rule ids the
model invents are dropped.

```yaml
- id: no-print            # letters, digits, - _ . (max 64)
  description: Use the logging module instead of print() in library code
  severity: medium        # critical | high | medium | low | info
  paths: ["src/**"]
  enabled: true
```

## `.reviewbot.yml`

A repository can keep review settings in `.reviewbot.yml` at its root. Reviewbot reads the file from the pull
request's **base commit**, never from the PR head, so a pull request cannot weaken its own review.

```yaml
profile: security             # strict | balanced | lenient | security
min_severity: medium
min_confidence: 0.6
max_inline_comments: 15
ignore_patterns:              # added to the dashboard's patterns
  - docs/
instructions: |               # appended to the dashboard's instructions
  We use Django; flag raw SQL.
rules:                        # merged with dashboard rules; the file wins on the same id
  - id: no-eval
    description: Never call eval() on user input
    severity: critical
```

Cost guards (size and token limits), the LLM key, triggers, branch filters, and the check-run gate can only be
changed in the dashboard. Unknown keys are ignored with a warning. An invalid file never blocks a review: the
dashboard settings are used, and the problem is shown in the review summary and on the review page.

## PR comment commands

Comment on a pull request with one of these on the first line:

| Command | Effect |
|---|---|
| `/reviewbot review` | Queue a full review of the current head (works even when auto-review is off). |
| `/reviewbot ignore` | Stop automatic reviews (open, reopen, push) for this pull request. |
| `/reviewbot resume` | Resume automatic reviews for this pull request. |

Only commenters GitHub reports as the repository owner, an organization member, or a collaborator can run
commands. Other comments, and comments from bots, are ignored. The repository must be enabled in Reviewbot.

