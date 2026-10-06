# Architecture

```
            GitHub ──webhook──▶ ┌────────────┐
                                │  web       │  Next.js dashboard; proxies /api, /webhooks
 Browser ───────────────────────▶ (port 3000)│
                                └─────┬──────┘
                                      │ HTTP
                                ┌─────▼──────┐      ┌────────────┐
                                │  api       │◀────▶│ PostgreSQL │  source of truth
                                │ Django/DRF │      └────────────┘
                                └─────┬──────┘
                                      │ enqueue           ┌────────┐
                                      └──────────────────▶│ Redis  │  queue, cache, throttles
                                                          └───┬────┘
                                ┌────────────┐                │
            GitHub API ◀────────┤  worker    │◀───────────────┘
            LLM provider ◀──────┤  Celery    │
                                └────────────┘
                                ┌────────────┐
                                │  beat      │  periodic: stuck-run reaper, log pruning
                                └────────────┘
```

## Backend layout (`backend/apps`)

| App | Responsibility |
|---|---|
| `core` | Logging with secret redaction, error envelope, pagination, health and metrics endpoints |
| `accounts` | Users (email login, role column), first-run setup, session auth, login throttling |
| `credentials` | Encrypted LLM credentials, `crypto` (MultiFernet), validation, key rotation command |
| `llm` | `LLMProvider` protocol and adapters: OpenAI, Anthropic, OpenAI-compatible, offline demo |
| `git_providers` | `GitProvider` protocol, GitHub App client (JWT, installation tokens, rate limits), fake provider |
| `repositories` | Provider connection, installations, repositories, per-repo settings, sync |
| `webhooks` | Signed webhook receiver, delivery log, event handlers |
| `reviews` | Pull requests, review runs, findings, token usage, the review pipeline and its Celery tasks |
| `reviews/engine` | Pure functions: diff parsing, ignore rules, chunking, prompts, schema, fingerprints, rendering |

## Review pipeline

`apps/reviews/pipeline.py` runs one `ReviewRun` through these stages. The current stage is persisted, so a failure
names the stage that broke.

1. **fetch_diff**: PR metadata and changed files from the `GitProvider`. A closed PR is skipped; a PR with too
   many files is skipped with a comment.
2. **filter**: drop deleted, binary, and ignored files (gitignore patterns) and record why.
3. **chunk**: render hunks with new-file line numbers and pack them into token-budgeted chunks. Cost caps
   (changed lines, estimated input tokens) skip the review before any LLM call.
4. **llm**: up to 3 chunks run concurrently. Each response must match one JSON schema; malformed output gets
   one repair retry. Every call is recorded in `LLMUsage`. An authentication error marks the credential invalid.
5. **aggregate**: anchor findings to commentable diff lines, compute fingerprints, then apply severity and
   confidence thresholds, de-duplication across runs, and the inline cap.
6. **post_comments**: one GitHub review (`COMMENT`) with inline comments plus a summary. If GitHub rejects an
   inline position, the findings move into the summary and the review is posted again without inline comments.

The Celery task retries on Git rate limits (`RetryLater`). A lease prevents two workers from processing the
same run, and a run that already has a posted review is never posted twice.

## Users and access

- One instance serves one organization: many GitHub installations and repositories, and many users.
- Users have one of three roles (admin, reviewer, viewer), enforced by DRF permission classes in
  `apps/accounts/permissions.py`. Reviewers may change only the review-content repository settings
  (`REVIEWER_SETTINGS_FIELDS`).
- People join through single-use invites (only a SHA-256 of the token is stored) or "Sign in with GitHub",
  which uses the GitHub App's OAuth client. Sessions are Django sessions in PostgreSQL.
- `apps/audit` records security-relevant actions in an append-only table: the model refuses updates and a
  PostgreSQL trigger rejects `UPDATE`. A daily beat task applies retention.

## Security notes

- The LLM has no tools and no network access through us. Its output is schema-validated, `@mentions` are
  neutralized, suggestion fences are escaped, and the output is only ever posted as a PR comment.
- Prompts mark PR content as untrusted and place repository instructions in a delimited block.
- Installation tokens live only in worker memory. Webhook payloads are kept for 30 days for debugging.

## Extending

- **New LLM provider**: implement `complete_json` and `validate` (see `apps/llm/base.py`), then register it in
  `apps/llm/registry.py`.
- **New Git provider** (GitLab, Bitbucket): implement `GitProvider` (`apps/git_providers/base.py`), add a
  webhook receiver, and register a factory in `apps/git_providers/registry.py`.
