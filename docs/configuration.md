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
| `REVIEWBOT_INVITE_TTL_HOURS` | `72` | How long an invite link stays valid. |
| `REVIEWBOT_ALLOW_PRIVATE_WEBHOOKS` | `false` | Allow notification webhooks to private or internal addresses, and plain `http://`. |
| `REVIEWBOT_DIGEST_DAY` / `_HOUR` / `_MINUTE` | `mon` / `8` / `52` | When the weekly digest is sent (UTC, cron fields). |
| `REVIEWBOT_AUDIT_RETENTION_DAYS` | `365` | Audit events older than this are deleted daily. |
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
| Ask for tests | on | When source files change without test changes, the model is asked for a `test` finding (capped at medium). |
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

## Risk score

Every completed review gets a deterministic risk score from 0 to 100, shown in the PR summary, the check run, and
the dashboard. Buckets are low (< 30), medium (30-59), and high (60+). The score adds up:

- **Findings:** reported findings, at critical 30, high 15, medium 6, low 2, info 0, capped at 60.
- **Size:** changed lines, at 50+ → 3, 200+ → 6, 500+ → 10, 1000+ → 15.
- **Sensitive paths**, each category once: CI workflows +15, auth/security code +15, database migrations +10,
  container/deployment files +10, dependency manifests +5, settings/config files +5.

## Feedback

On a review page, each finding can be **accepted**, **dismissed** (false positive, won't fix, duplicate, other), or
voted 👍/👎. A dismissed finding is not posted again on later reviews of the same pull request; it shows as
"Dismissed on an earlier run". The repository settings page shows acceptance and false-positive rates per category
(also at `GET /api/v1/feedback-stats?repository=<id>`). Feedback is collected only; it does not tune prompts
automatically.

## Team and sign-in

| Variable | Default | Description |
|---|---|---|
| `REVIEWBOT_GITHUB_LOGIN` | `true` | Show "Sign in with GitHub" when an OAuth client is available. |
| `REVIEWBOT_GITHUB_OAUTH_CLIENT_ID` / `_SECRET` | empty | Use a separate GitHub OAuth App for sign-in. By default the GitHub App's own client ID and secret are used. |
| `REVIEWBOT_ALLOW_SIGNUP` | `false` | Let GitHub users without an invite create an account. |
| `REVIEWBOT_SIGNUP_EMAIL_DOMAINS` | empty | With signup on, only allow these verified email domains (comma-separated). Empty means any domain. |
| `REVIEWBOT_SIGNUP_ROLE` | `viewer` | Role for self-signup accounts: `viewer` or `reviewer`. It is never `admin`. |
| `REVIEWBOT_EMAIL_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_USE_TLS`, `_FROM` | empty / `587` / `true` | SMTP for invite emails. Without it, the dashboard shows the invite link for you to share. |

### Roles

| | Admin | Reviewer | Viewer |
|---|---|---|---|
| See pull requests, reviews, findings, repository settings | ✓ | ✓ | ✓ |
| Re-run reviews; accept, dismiss and vote on findings | ✓ | ✓ | |
| Edit review rules: profile, thresholds, rules, instructions, ignored files, test suggestions | ✓ | ✓ | |
| Enable repositories; edit keys, triggers, checks, branch filters, cost limits | ✓ | | |
| LLM keys, budgets, model prices, integrations, team, audit log, usage | ✓ | | |

The API enforces every rule; the dashboard only hides controls you cannot use. At least one active admin always
remains. A deactivated user's sessions stop working right away. Developers who only open pull requests need no
account: reviews, checks and `/reviewbot` commands all happen on GitHub.

Single sign-on (Okta, Entra ID, Keycloak, Google, GitLab) and enforcing it with `REVIEWBOT_PASSWORD_LOGIN` are
covered in [Single sign-on](sso.md).

### How people sign in

1. **Invite** (recommended): an admin enters an email and a role under **Team**. The person opens the single-use
   link and either sets a password or clicks **Continue with GitHub**.
2. **GitHub**: an existing account is matched by GitHub user ID, or the first time by a *verified* email
   address on the GitHub account. Accounts already linked to a different GitHub user are never re-linked.
3. **Password**: email and password. Failed attempts are rate limited.

### Audit log

Admins see an append-only log under **Audit log** (also `GET /api/v1/audit-events`). It records:

- sign-ins (successful and failed) and sign-outs;
- password changes and linking or unlinking a GitHub account;
- invites, role changes, and deactivating or reactivating users;
- LLM key creation, edits, rotation and revocation;
- repositories being enabled or disabled, and settings changes, with a before/after diff;
- GitHub connection changes;
- manual review requests and finding triage.

Event metadata goes through the same secret redaction as the logs. The API has no endpoint to edit or delete
events, and a database trigger rejects updates.

## Cost and budgets

### Prices

Every LLM call is priced from the **model price table** (**LLM keys → Model prices**, or `/api/v1/model-prices`).

- Each row is a provider (or any provider) plus a model-name prefix, with USD prices per million input and output
  tokens. The most specific row wins: a provider-specific row beats a generic one, then the longest prefix wins.
- Reviewbot ships list prices for common OpenAI and Anthropic models (see `PRICES_AS_OF` in
  `backend/apps/credentials/pricing.py`). Providers change prices and contracts differ, so check them. Edited rows
  are never overwritten, and **Restore defaults** only adds rows that are missing.
- Calls to a model without a price get an unknown cost. The usage page reports them as "unpriced", and they don't
  count toward budgets. Price self-hosted models (Ollama, vLLM) at 0. After adding prices, **Recalculate unpriced
  usage** fills in past calls.

### Monthly budgets

Set a budget per LLM key on the LLM keys page. Spending is counted per UTC calendar month.

| Spend | What happens |
|---|---|
| 80% | One alert per month: an audit event `budget.threshold_reached`, and an email to admins if SMTP is configured. |
| 100% | Alert as above. Automatic reviews (new PRs, pushes, `/reviewbot review`) using this key are **skipped**, with a short PR comment (no comment for pushes) and a skipped check run. Reviewers cannot start manual reviews; admins still can. |

A single review is already capped by the repository's cost limits, so it can overshoot the budget by at most one
review.

### Usage and export

**Usage** (admins) shows cost, reviews, tokens and requests for the last 7, 30 or 90 days, budget bars, a daily cost
chart, and breakdowns by repository and model. **Download CSV** exports day × repository × key × model rows. The
same data is at `GET /api/v1/usage?group_by=day,repository,credential,model&from=…&to=…`, with `&export=csv` for
CSV.

## API tokens

Every user can create personal API tokens under **Account → API tokens**. Send them as
`Authorization: Bearer rbt_…`.

- A token acts with its owner's role, and stops working when the owner is deactivated or the token is revoked or
  expires (7, 30, 90 or 365 days, or never).
- Only a SHA-256 hash is stored, the token is shown once, and log output redacts `rbt_…` values.
- Tokens cannot create tokens or change the password; those need a dashboard session. Token requests need no
  CSRF header.

```bash
curl -H "Authorization: Bearer $REVIEWBOT_TOKEN" \
  "https://reviewbot.example.com/api/v1/usage?group_by=day,repository&export=csv" -o usage.csv
```

## Notifications

Admins add channels under **Notifications** (`/api/v1/notification-channels`). Each channel picks its events:

| Event | When |
|---|---|
| High-risk pull requests | A review finishes with a risk score of at least the channel's threshold (default 60), or with a reported finding at or above its severity (default critical). Can be limited to some repositories. |
| Failed reviews | A review fails: LLM errors, an invalid key, GitHub errors, or retries exhausted. |
| LLM budget alerts | A key reaches 80% or 100% of its monthly budget. |
| Weekly digest | Mondays (configurable). Reviews, findings by severity, triage, LLM cost, and the five riskiest PRs. |

Channel types:

- **Slack**: an [incoming webhook](https://api.slack.com/messaging/webhooks) URL (`https://hooks.slack.com/...`). PR titles
  and other user text are escaped, so they cannot trigger `@channel` mentions or inject links.
- **Email**: a list of recipients; needs SMTP (`REVIEWBOT_EMAIL_*`).
- **Webhook**: a JSON `POST` to any HTTPS URL (for example a Microsoft Teams workflow or your own service):

  ```json
  {"event": "review.high_risk", "title": "...", "text": "...", "url": "https://reviewbot.example.com/reviews/42",
   "level": "danger", "data": {"repository": "acme/app", "number": 7, "risk_score": 72, "findings": {"critical": 1}},
   "sent_at": "2026-10-06T11:28:51+00:00"}
  ```

  Headers: `X-Reviewbot-Event`, and with a signing secret, `X-Reviewbot-Signature-256: sha256=<hex HMAC-SHA256 of the
  raw body>`. Verify it with a constant-time comparison, as for GitHub webhooks.

Delivery details:

- Messages are sent by the worker. Network errors, HTTP 429 and 5xx are retried with backoff, up to 4 attempts.
  Other errors fail immediately.
- Each channel shows its last 50 deliveries, and **Send test** sends a sample message right away.
- Every event is sent at most once per channel: once per review run, budget threshold and month, or digest week.
- URLs and secrets are encrypted at rest and never returned by the API.
- For SSRF protection, URLs must be HTTPS and resolve to public addresses, checked both on save and on every send.
  Redirects are not followed. Set `REVIEWBOT_ALLOW_PRIVATE_WEBHOOKS=true` for on-premise targets.
- Without any channel, admins still get budget alerts by email when SMTP is configured.
