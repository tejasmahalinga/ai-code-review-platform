# Reviewbot — Feature Plan

> Status: v0.1 planning baseline. Working name "Reviewbot" is a placeholder (see Open Question 9).

## 1. Product overview

**Vision.** A self-hosted service that installs on your Git provider, reviews every pull/merge request with the LLM *you* pay for, and posts actionable, severity-ranked inline findings back on the PR — with a dashboard to control keys, repos, rules, and cost. No code or keys leave your infrastructure except the diff sent to the LLM provider you chose (or none, with a local OpenAI-compatible model such as Ollama/vLLM).

**Target users.**
| Segment | Need | Implication |
|---|---|---|
| Solo devs | A second pair of eyes, near-zero ops | One `docker compose up`, one admin account, one API key |
| Small teams (2–20) | Consistent review, team conventions, cost control | Per-repo rules, roles, per-repo/per-key cost |
| OSS maintainers | Triage drive-by PRs, catch security issues from untrusted contributors | Fork-PR safety, cost caps, local-model support |

**Definition of done for v1 (v1.0 = all P0 + P1).** v0.1 (MVP = P0) is done when a new user can, on a fresh VM in ≤ 30 minutes following the docs: `docker compose up` → create admin → add an OpenAI/Anthropic/OpenAI-compatible key → create & install the GitHub App → enable a repo → open a PR → receive a summary + inline comments within 5 minutes for a ≤ 500-changed-line PR → see the review, its findings, token usage, and re-run it from the dashboard. v1.0 additionally covers GitLab, notifications, check runs, roles, audit, and cost analytics.

## 2–3. Feature list with priorities

Priority legend: **P0** = MVP, cannot launch without it · **P1** = needed soon after launch (v1.0) · **P2** = nice to have. ⚠️ = secretly a large undertaking.

### Feature matrix (summary)

| ID | Feature | Area | Pri |
|---|---|---|---|
| RE-01 | Webhook ingestion (GitHub) | Review engine | P0 |
| RE-02 | Diff fetch, parsing & line mapping | Review engine | P0 |
| RE-03 | Chunking for large PRs + size/cost budget ⚠️ | Review engine | P0 |
| RE-04 | Model-agnostic LLM adapter (BYOK) | Review engine | P0 |
| RE-05 | Structured findings: category + severity | Review engine | P0 |
| RE-06 | Inline + summary comments | Review engine | P0 |
| RE-07 | File ignore patterns | Review engine | P0 |
| RE-08 | Custom repo instructions (free-text prompt) | Review engine | P0 |
| RE-09 | Finding de-duplication across runs | Review engine | P0 |
| RE-10 | Incremental review on new pushes | Review engine | P1 |
| RE-11 | Review profiles (strict / balanced / lenient / security) | Review engine | P1 |
| RE-12 | Risk score per PR | Review engine | P1 |
| RE-13 | Test suggestions | Review engine | P1 |
| RE-14 | Configurable review rules (structured) | Review engine | P1 |
| RE-15 | Repo config file (`.reviewbot.yml`) | Review engine | P1 |
| RE-16 | Feedback: thumbs up/down + accept/dismiss in dashboard | Review engine | P1 |
| RE-17 | PR comment commands (`/reviewbot review`) | Review engine | P1 |
| RE-18 | Feedback via Git-provider reactions/replies | Review engine | P2 |
| RE-19 | Repository context beyond the diff (RAG) ⚠️ | Review engine | P2 |
| KEY-01 | Add / revoke LLM keys, encrypted at rest | API keys | P0 |
| KEY-02 | Key validation on save | API keys | P0 |
| KEY-03 | Per-call token usage recording | API keys | P0 |
| KEY-04 | Key rotation (zero-downtime swap) | API keys | P1 |
| KEY-05 | Per-key cost tracking & monthly budget cap | API keys | P1 |
| REPO-01 | Repo sync from installation; enable/disable | Repos | P0 |
| REPO-02 | Per-repo settings (auto-review, key/model, ignores, instructions) | Repos | P0 |
| REPO-03 | Per-repo branch filters & draft handling | Repos | P1 |
| REPO-04 | Language allow-list | Repos | P2 |
| PR-01 | Unified PR list with filters | PRs | P0 |
| PR-02 | Review detail view | PRs | P0 |
| PR-03 | Manual re-run | PRs | P0 |
| PR-04 | Review run history per PR | PRs | P1 |
| PR-05 | Severity filter + finding search | PRs | P1 |
| INT-01 | GitHub App (create via manifest, install) | Integrations | P0 |
| INT-02 | GitHub check runs | Integrations | P1 |
| INT-03 | GitLab (MRs, notes, commit status) ⚠️ | Integrations | P1 |
| INT-04 | Slack notifications | Integrations | P1 |
| INT-05 | Email notifications (SMTP) | Integrations | P1 |
| INT-06 | Discord notifications | Integrations | P2 |
| INT-07 | Outbound webhooks (signed) | Integrations | P2 |
| INT-08 | Bitbucket Cloud ⚠️ | Integrations | P2 |
| INT-09 | GitLab CI / Bitbucket Pipelines status | Integrations | P2 |
| ADM-01 | Dashboard auth (local accounts, first-run bootstrap) | Admin | P0 |
| ADM-02 | Roles: admin / reviewer / viewer + invites | Admin | P1 |
| ADM-03 | Login with GitHub/GitLab OAuth | Admin | P1 |
| ADM-04 | Audit log | Admin | P1 |
| ADM-05 | Usage & cost analytics (per repo, per key) | Admin | P1 |
| ADM-06 | Personal API tokens for the REST API | Admin | P2 |
| ADM-07 | SSO (OIDC) | Admin | P2 |

**MVP shape:** GitHub only, one LLM call path, findings with severity/category, inline + summary comments, dashboard to manage keys, repos, and reviews. Everything else waits.

### 2.1 Review engine

**RE-01 Webhook ingestion (GitHub) — P0**
Single endpoint receives GitHub App webhooks, verifies the HMAC signature, stores the delivery, and enqueues work; handlers never do LLM/Git calls inline. Triggers: `pull_request` actions `opened`, `reopened`, `ready_for_review` (and `synchronize` once RE-10 lands); `installation`/`installation_repositories` for repo sync.
*Why:* Webhooks are the only trigger; GitHub retries and redelivers, so ingestion must be fast, authenticated and idempotent.
- Request with missing/invalid `X-Hub-Signature-256` → HTTP 401, nothing stored or enqueued.
- Valid delivery responds 202 in < 500 ms (p95) with the job enqueued; LLM/Git calls happen only in the worker.
- Re-delivering the same `X-GitHub-Delivery` ID creates no second review run (unique constraint on delivery ID + idempotency key `(repo, pr, head_sha, trigger)`).
- Events for disabled repos, draft PRs (default), or unsupported actions are stored with status `ignored` and a reason.

**RE-02 Diff fetch, parsing & line mapping — P0**
Worker fetches the PR file list/patches via the GitHub API, parses unified diffs into per-file hunks with old/new line numbers, and classifies files (added/modified/deleted/renamed/binary). Every finding the LLM returns is validated against the set of commentable `(path, new_line)` pairs.
*Why:* Inline comments on lines not in the diff are rejected by GitHub (422); bad mapping is the #1 source of broken reviews.
- Given fixture diffs (add, modify, delete, rename, binary, no-newline-at-EOF, >300 files paginated), parser output matches golden JSON.
- A finding referencing a line outside the diff is moved to the summary comment ("unanchored findings"), never posted inline, never causing a 422.
- Binary and deleted files are excluded from LLM input.

**RE-03 Chunking for large PRs + size/cost budget — P0 ⚠️**
Files are packed into chunks under a per-model token budget (estimated with a tokenizer, ~4 chars/token fallback); oversized single files are split at hunk boundaries. Each repo has hard caps: max files, max changed lines, max estimated input tokens per review. Above the cap the review is skipped (or limited to the first N files by risk ordering) with an explanatory comment.
*Why:* Prevents context-window errors and surprise bills on a 20k-line PR. ⚠️ Getting *quality* on huge PRs (cross-file reasoning across chunks) is a research problem; v1 only guarantees bounded cost and no failures.
- A 5,000-line fixture PR with a 16k-token budget produces ≥ 2 chunks, each under budget, and every non-ignored hunk appears in exactly one chunk.
- A PR exceeding `max_changed_lines` produces a single summary comment stating the limit and a review run with status `skipped_too_large`; zero LLM calls are made.
- Chunks are reviewed with bounded concurrency (default 3) and the merged result contains findings from all chunks.

**RE-04 Model-agnostic LLM adapter (BYOK) — P0**
A thin provider interface with three implementations: OpenAI, Anthropic, and "OpenAI-compatible" (custom base URL: Ollama, vLLM, LM Studio, OpenRouter, Azure-style gateways). Requests use structured JSON output (JSON schema / tool call) and return normalized `{findings, summary, usage}`.
*Why:* BYOK and model-agnostic are the core value proposition; local models are critical for privacy-sensitive users.
- With HTTP mocked, each adapter produces identical normalized output for the same canned response.
- Invalid JSON from the model triggers one repair retry; a second failure marks the chunk `failed` with the raw response stored (truncated to 64 KB) for debugging.
- 429/5xx are retried with exponential backoff + jitter honoring `Retry-After` (max 5 attempts); 401/403 fail immediately and mark the key `invalid`.
- Changing a repo's model requires no code change — only selecting a different credential/model string.

**RE-05 Structured findings: category + severity — P0**
Each finding has `category ∈ {bug, security, performance, maintainability, style, test}`, `severity ∈ {critical, high, medium, low, info}`, `path`, `line_start/line_end`, `title`, `body`, optional `suggestion` (replacement code), and `confidence (0–1)`. Security/bug/style "detection" in v1 is LLM-based via this schema — not static analysis.
*Why:* Severity and category make findings filterable, sortable and usable for gating.
- Model output not matching the JSON schema is rejected by the validator (unit-tested with invalid payloads).
- Findings below the repo's `min_severity` (default `low`) are stored but not posted.
- Findings with `confidence < 0.5` (configurable) are stored but not posted.

**RE-06 Inline + summary comments — P0**
Posts one GitHub *pull request review* (event `COMMENT`, never `APPROVE`/`REQUEST_CHANGES` in v1) containing inline comments plus a summary body: overall assessment, counts by severity, unanchored findings, files skipped, model used. `suggestion` blocks use GitHub's ```` ```suggestion ```` syntax. Max inline comments per review is capped (default 25, highest severity first).
*Why:* Developers act on feedback where they already work.
- A fixture review with 3 anchored + 1 unanchored finding results in exactly one GitHub API "create review" call with 3 inline comments and the 4th listed in the body.
- When findings exceed the cap, the top 25 by (severity, confidence) are inline and the summary states "N more findings in dashboard" with a link.
- Comment bodies have `@`-mentions neutralized (zero-width joiner) so model output cannot ping users.
- A review with zero findings posts a summary "No issues found" (configurable to post nothing).

**RE-07 File ignore patterns — P0**
Gitignore-syntax patterns (via `pathspec`) applied before any LLM call. Built-in defaults: lockfiles, `vendor/`, `node_modules/`, `dist/`, `*.min.js`, generated code (`*.pb.go`, `*_pb2.py`), images, `*.snap`. Per-repo patterns add to (or, with an explicit toggle, replace) defaults.
*Why:* Saves money and avoids noise; also the privacy control for files that must never be sent to an LLM.
- A PR touching only `package-lock.json` makes zero LLM calls and run status is `skipped_no_reviewable_files`.
- Pattern `!src/generated/keep.py` re-includes that file (negation test).
- Review detail lists every ignored file and the pattern that matched it.

**RE-08 Custom repo instructions — P0**
Free-text (≤ 4,000 chars) appended to the system prompt per repo, e.g. "We use Django; flag raw SQL. Ignore missing docstrings."
*Why:* Cheapest possible customization that captures most team conventions.
- Saved instructions appear verbatim in the prompt sent to the provider (asserted on the mocked request body).
- Instructions > 4,000 chars rejected with 400.
- Instructions are placed in a delimited section and the system prompt states PR content is untrusted data (prompt-injection hardening, see NFR).

**RE-09 Finding de-duplication across runs — P0**
Each finding gets a fingerprint `hash(path, normalized code context ±3 lines, category, normalized title)`. Before posting, findings whose fingerprint already has a posted comment on this PR are skipped.
*Why:* Without it, every re-run spams duplicate comments — the fastest way to get the bot uninstalled.
- Running the same review twice on the same head SHA posts inline comments only once; second run shows them as `duplicate`.
- A finding whose line shifted (code above inserted) but whose context is identical is still recognized as duplicate.

**RE-10 Incremental review on new pushes — P1**
On `synchronize`, review only `compare(last_reviewed_sha, new_head_sha)` restricted to files in the PR, with debounce (wait 60 s for further pushes; superseded runs are cancelled).
*Why:* Re-reviewing the full PR on every push multiplies cost and noise.
- Two pushes 10 s apart produce exactly one review run (for the later SHA).
- A push changing only `README.md` sends only that file to the LLM.
- Force-push (last reviewed SHA not an ancestor) falls back to full review.

**RE-11 Review profiles — P1**
Built-in presets: `strict` (min severity low, style on, cap 50), `balanced` (default), `lenient` (min severity high, no style), `security` (security category only, OWASP-oriented prompt, min severity medium). Profile = prompt template + thresholds; repo picks one.
*Why:* One-click tuning of noise vs. thoroughness.
- Switching a repo from `balanced` to `security` changes the system prompt template ID and drops non-security findings from posting (fixture test).
- Each profile's thresholds are visible read-only in the repo settings UI.

**RE-12 Risk score — P1**
PR-level score 0–100 computed deterministically from findings (severity weights) plus heuristics (lines changed, sensitive paths such as `auth/`, `migrations/`, `.github/workflows/`, `Dockerfile`, dependency manifests). Shown in summary and dashboard. Not LLM-generated, so it's reproducible.
- Same findings + diff always yield the same score (pure function, unit-tested).
- A PR touching `.github/workflows/*.yml` gets ≥ +15 from the sensitive-path heuristic.
- Score buckets (low < 30, medium < 60, high ≥ 60) filterable in PR list.

**RE-13 Test suggestions — P1**
When a PR changes non-test source files without touching tests, the model is asked for `category=test` findings naming the untested behavior and a sketched test case.
- Fixture PR modifying `src/foo.py` without `tests/` changes yields the test-suggestion prompt section; one touching both does not.
- Test suggestions can be disabled per repo and are never higher than `medium` severity.

**RE-14 Configurable review rules — P1**
Structured rules: `{id, description, paths (glob), severity, enabled}`, e.g. "No `print()` in `src/**`". Rules are rendered into the prompt and findings may cite `rule_id`.
- A rule scoped to `src/**` is included in prompts for chunks containing `src/` files only.
- Findings citing a rule display the rule ID in comment and dashboard.

**RE-15 Repo config file `.reviewbot.yml` — P1**
Optional file in the repo default branch overriding dashboard settings (profile, ignores, instructions, rules). Read from the **base** branch, never the PR head, so a PR cannot weaken its own review.
- Config from the PR head is ignored (test with a PR that modifies `.reviewbot.yml`).
- Invalid YAML → review proceeds with dashboard settings and summary notes the config error.

**RE-16 Feedback loop (dashboard) — P1**
Per finding: 👍/👎 and status `open → accepted | dismissed (reason: false positive / won't fix / duplicate)`. Aggregated acceptance rate per repo, category, profile, and model. v1 *collects* signal; it does not auto-tune prompts (non-goal).
- Dismissing a finding as false positive records user, time, reason; it is excluded from "open" counts.
- Dismissed fingerprints are not re-posted on later runs of the same PR.
- Analytics show acceptance rate per category for a repo, matching seeded fixture data.

**RE-17 PR comment commands — P1**
`/reviewbot review` (full re-run), `/reviewbot ignore` (skip PR) in PR comments by users with write access.
- Command from a user without write access on the repo is ignored and logged.
- Command triggers a run with `trigger=comment_command` within the same idempotency rules.

**RE-18 Feedback via provider reactions/replies — P2.** Sync 👍/👎 reactions on bot comments into RE-16. AC: a 👎 reaction on a bot comment results in feedback recorded within one sync cycle (polling; GitHub has no reaction webhooks).

**RE-19 Repository context (RAG) — P2 ⚠️.** Index the repo to give the LLM definitions of referenced symbols. Large: indexing, embeddings storage, per-language parsing, incremental updates. AC deferred until design spike.

### 2.2 Dashboard — API key management

**KEY-01 Add / revoke LLM keys, encrypted at rest — P0**
Admins add credentials `{name, provider, api_key, base_url?, default_model}`. Secret is encrypted with an application master key (Fernet/AES-128-CBC+HMAC via `cryptography.MultiFernet`, key from env `REVIEWBOT_ENCRYPTION_KEYS`) before insert; API returns only `last4`. Revoke = soft delete; repos using it are flagged.
*Why:* Users are handing over billable secrets; leaking one is a critical incident.
- The `encrypted_secret` column in Postgres never contains the plaintext (test inspects raw row).
- No API response, log line, or error message contains the plaintext key (test scans captured logs/responses for it).
- Revoked credential cannot be selected for a repo; reviews for repos still pointing at it fail fast with status `failed_no_credential` and a dashboard banner.
- Starting the app without `REVIEWBOT_ENCRYPTION_KEYS` fails with a clear error.

**KEY-02 Key validation on save — P0**
On create, perform a minimal call (list models, or 1-token completion) to verify the key and model. Store `last_validated_at` and status.
- Invalid key → 400 with provider error message (sanitized); credential not saved.
- "Validate" button re-checks an existing key and updates status.

**KEY-03 Per-call token usage recording — P0**
Every LLM call writes an `LLMUsage` row: credential, repo, review run, model, input/output tokens, latency, status.
*Why:* Cheap to do now, impossible to backfill; foundation for KEY-05/ADM-05.
- After a fixture review with 3 chunks, exactly 3 usage rows exist with token counts equal to mocked provider `usage`.
- Failed calls are recorded with `status=error` and zero/partial tokens.

**KEY-04 Key rotation — P1**
"Rotate" replaces the secret of an existing credential in place (new value validated first); in-flight jobs finish with the old value, new jobs use the new one. Master-key rotation supported via MultiFernet + `manage.py rotate_encryption_key`.
- After rotation, the credential ID is unchanged and repos referencing it need no update.
- `rotate_encryption_key` re-encrypts all rows; app works with only the new master key afterwards.

**KEY-05 Cost tracking & budget cap — P1**
Cost = tokens × price from a versioned price table (`pricing.yaml`, user-overridable; custom-base-URL models default to $0). Optional monthly budget per credential; when exceeded, new reviews are skipped with a comment.
- Cost of a usage row equals tokens × price effective at call time (price changes don't rewrite history).
- With budget $1.00 and $1.01 spent this month, next review is `skipped_budget_exceeded` with zero LLM calls.
- Dashboard shows month-to-date spend per key matching sum of usage rows.

### 2.3 Dashboard — repository management

**REPO-01 Repo sync; enable/disable — P0**
Repos come from GitHub App installations (sync on `installation*` webhooks + manual "Sync" button). Each repo is disabled by default; admin enables it. Disconnect = uninstall on GitHub (we mark `removed`, keep history).
- Adding a repo to the installation on GitHub makes it appear in the dashboard within 1 minute (webhook) or after "Sync".
- Removing it on GitHub marks it `removed`; its history stays viewable; no new reviews run.
- A disabled repo's PR webhooks produce `ignored` deliveries and zero jobs.

**REPO-02 Per-repo settings — P0**
Fields: `auto_review (bool)`, `credential + model`, `ignore_patterns`, `custom_instructions`, `min_severity`, `max_inline_comments`, `max_changed_lines`, `review_drafts (bool)`. Validated server-side.
- Each field round-trips via `PATCH /repositories/{id}/settings` and is used by the next review (asserted in mocked prompt/limits).
- Enabling a repo without a selected valid credential is rejected with 400.
- `auto_review=false` → PR events ignored but manual re-run works.

**REPO-03 Branch filters & drafts — P1.** Review only PRs targeting branches matching patterns (default: all). AC: a PR into `release/*` with filter `main` is ignored with reason `base_branch_filtered`; toggling `review_drafts` reviews draft PRs.

**REPO-04 Language allow-list — P2.** Restrict review to file extensions/languages. Mostly redundant with ignore patterns; kept P2. AC: allow-list `[python]` sends only `*.py`/`*.pyi` files to the LLM.

### 2.4 Dashboard — PR/MR management

**PR-01 Unified PR list — P0**
Paginated table across repos: repo, PR #/title/author, state (open/closed/merged), latest review status (`queued/running/completed/failed/skipped`), finding counts by severity, updated at. Filters: repo, PR state, review status. Server-side pagination (cursor, 50/page).
- With 120 seeded PRs, page 1 returns 50, cursor fetches the rest, no duplicates.
- Filter `repo=X&review_status=failed` returns exactly the matching seeded PRs.
- Row links to GitHub PR and to review detail.

**PR-02 Review detail view — P0**
For a review run: status timeline, trigger, head SHA, model, tokens, duration, files reviewed/ignored (with matched pattern), summary, findings grouped by file with severity/category, posted vs. suppressed (and why: below threshold, duplicate, cap, unanchored), and error message on failure.
- A failed run shows the sanitized error and the failing stage (`fetch_diff`, `llm`, `post_comments`).
- Every finding shows its suppression reason or a link to the posted GitHub comment.

**PR-03 Manual re-run — P0**
"Re-run review" button (and API) enqueues a new run on the current head SHA with `trigger=manual`, bypassing idempotency but still deduping comments (RE-09).
- Clicking re-run creates a new run in `queued` within 1 s; UI polls until terminal state.
- Re-run while a run for the same PR is `running` returns 409.

**PR-04 Run history — P1.** All runs per PR in reverse chronological order with diffable finding sets. AC: a PR with 3 runs lists 3 entries; selecting two shows findings added/resolved between them (by fingerprint).

**PR-05 Severity filter + search — P1.** Filter PR list by "has ≥ N findings of severity ≥ X" and risk bucket; full-text search on finding titles. AC: `min_severity=high` returns only PRs whose latest run has ≥ 1 high/critical finding.

### 2.5 Integrations

**INT-01 GitHub App — P0**
Setup wizard uses GitHub's **App Manifest flow** to create the App with exactly the needed permissions and webhook URL, then stores App ID, private key (encrypted), webhook secret. Installation tokens are minted per job (JWT → installation token, cached ≤ 50 min). Works with github.com and GitHub Enterprise Server (configurable API base URL).
- Completing the manifest flow stores credentials and shows "Install App" link; no manual copy-paste of keys needed.
- Requested permissions are exactly: `pull_requests: write`, `contents: read`, `metadata: read` (+ `checks: write` once INT-02 ships). Asserted against the generated manifest JSON.
- Installation tokens are never persisted in DB or logs.

**INT-02 GitHub check runs — P1.** Create a check run `Reviewbot` (in_progress → completed). Conclusion `neutral` by default; optional `failure` when any finding ≥ configured gating severity. AC: a run with a `critical` finding and gating=`high` sets conclusion `failure`; LLM error → `neutral` with "review failed" title (never blocks merges due to our outage).

**INT-03 GitLab (gitlab.com + self-managed) — P1 ⚠️**
MR webhooks (token-verified), diff via MR changes/versions API, inline discussions using `position` (base/start/head SHA), summary note, commit status. Auth: project/group access token or OAuth app. ⚠️ ~2–3 weeks: different diff-position model, no "App" concept, per-project webhook registration. **The provider abstraction (`GitProvider` interface) is designed in P0** so this is additive.
- Same fixture review produces equivalent comments on GitLab (mocked API) with valid `position` objects.
- Webhook with wrong `X-Gitlab-Token` → 401.
- Commit status set to `running` then `success`/`failed`.

**INT-04 Slack — P1.** Incoming-webhook URL (encrypted) per channel; events: review completed with ≥ severity X, review failed, budget exceeded. AC: a completed review with a high finding posts one message containing repo, PR link, counts by severity; Slack 5xx retried 3× then logged.

**INT-05 Email (SMTP) — P1.** SMTP settings in env; per-user opt-in for the same events + invites/password reset. AC: with Mailpit in compose, review-failed event delivers an email to subscribed users only.

**INT-06 Discord — P2.** Webhook URL; same events as Slack. AC: message posted with embed fields for counts.

**INT-07 Outbound webhooks — P2.** User-configured URL + secret; events `review.completed`, `review.failed`, `finding.dismissed`; HMAC-SHA256 signature header; retries with backoff; delivery log. AC: receiver can verify the signature using documented algorithm; failed deliveries retried 5× and shown in log.

**INT-08 Bitbucket Cloud — P2 ⚠️.** Same scope as GitLab via the `GitProvider` interface; another 2–3 weeks.

**INT-09 GitLab CI / Bitbucket Pipelines status — P2.** Mostly covered by commit statuses in INT-03; a CI-job mode (`reviewbot-cli review` inside a pipeline) is P2.

### 2.6 Admin & team

**ADM-01 Dashboard auth — P0**
Local accounts (email + Argon2 password hashing), session cookie (HttpOnly, Secure, SameSite=Lax) + CSRF, login rate-limiting (5/min/IP+email). First-run: if no users exist, `/setup` creates the first admin (or `manage.py createadmin` for headless). In P0 all users are admins.
- `/setup` is only reachable when zero users exist; afterwards 404.
- All `/api/v1/*` except auth/setup/webhooks/health return 401 without a session.
- 6th failed login in a minute returns 429.

**ADM-02 Roles + invites — P1.** `admin` (everything), `reviewer` (view all, re-run, feedback, edit repo instructions/rules), `viewer` (read-only; never sees key metadata beyond name/provider). Invite by email link (expires 72 h). AC: permission matrix enforced by API tests for every endpoint × role; viewer `POST /reviews/{id}/rerun` → 403.

**ADM-03 OAuth login (GitHub/GitLab) — P1.** Link identity to existing account by verified email or invite. AC: an unknown GitHub user without invite cannot create an account (unless `ALLOW_SIGNUP=true`).

**ADM-04 Audit log — P1.** Append-only records for: login/logout/failed login, credential create/rotate/revoke, repo enable/disable/settings change (with JSON diff, secrets redacted), user/role changes, integration changes. Filterable; retention configurable (default 365 days). AC: rotating a key creates exactly one `credential.rotated` event with actor, IP, target; no event payload contains secrets; API exposes no update/delete.

**ADM-05 Usage & cost analytics — P1.** Charts and CSV export: tokens and cost by day × repo × credential × model; reviews per day; acceptance rate. AC: totals equal the sum of `LLMUsage` rows for the selected range (seeded test); CSV export matches table.

**ADM-06 Personal API tokens — P2.** AC: token shown once, hashed at rest, scoped to user's role, revocable.

**ADM-07 OIDC SSO — P2.** AC: login via a test Keycloak realm in CI maps group claim to role.

## 4. Data model sketch

Single-tenant per deployment (one "workspace" per instance — see open questions).

```
User (id, email UNIQUE, password_hash, role[admin|reviewer|viewer], is_active, last_login, created_at)
 └─< AuditEvent (id, actor_id→User NULL, action, target_type, target_id, ip, metadata JSONB, created_at)  [append-only]

GitProviderConnection (id, provider[github|gitlab|bitbucket], base_url, app_id, client_id,
                       encrypted_private_key, encrypted_webhook_secret, encrypted_client_secret, created_at)
 └─< Installation (id, connection_id, external_id, account_login, account_type, suspended_at)
      └─< Repository (id, installation_id, external_id, full_name, default_branch, private, status[active|removed],
                      enabled bool)  UNIQUE(installation_id, external_id)
           ├── RepositorySettings (1:1; auto_review, credential_id→LLMCredential, model, profile, ignore_patterns[],
           │                       replace_default_ignores, custom_instructions, rules JSONB, min_severity,
           │                       max_inline_comments, max_changed_lines, max_input_tokens, review_drafts,
           │                       base_branch_patterns[], updated_by, updated_at)
           └─< PullRequest (id, repository_id, number, title, author_login, state, is_draft, base_ref, head_ref,
                            head_sha, html_url, last_reviewed_sha, updated_at)  UNIQUE(repository_id, number)
                └─< ReviewRun (id, pull_request_id, trigger[webhook|manual|comment|push], head_sha, base_sha,
                               status[queued|running|completed|failed|skipped|cancelled], status_reason, stage,
                               error, idempotency_key UNIQUE, credential_id, model, profile, settings_snapshot JSONB,
                               files_reviewed JSONB, files_ignored JSONB, summary, risk_score,
                               provider_review_id, check_run_id, started_at, finished_at, created_by→User NULL)
                     ├─< Finding (id, review_run_id, fingerprint, path, line_start, line_end, side, category,
                     │            severity, confidence, title, body, suggestion, rule_id,
                     │            post_status[posted|below_threshold|low_confidence|duplicate|cap_exceeded|unanchored|dismissed_previously],
                     │            provider_comment_id, provider_comment_url, state[open|accepted|dismissed], dismiss_reason)
                     │     └─< FindingFeedback (id, finding_id, user_id, vote[up|down], comment, created_at) UNIQUE(finding,user)
                     └─< LLMUsage (id, review_run_id NULL, credential_id, repository_id, model, input_tokens,
                                   output_tokens, cost_usd NUMERIC(12,6) NULL, latency_ms, status, created_at)
                                   INDEX(credential_id, created_at), INDEX(repository_id, created_at)

LLMCredential (id, name, provider[openai|anthropic|openai_compatible], base_url, encrypted_secret, last4,
               default_model, status[valid|invalid|revoked], last_validated_at, monthly_budget_usd NULL,
               created_by, created_at, revoked_at)

WebhookDelivery (id, provider, delivery_id UNIQUE(provider, delivery_id), event, action, repository_id NULL,
                 payload JSONB, status[received|enqueued|ignored|failed], reason, received_at)   [pruned after 30 days]

NotificationChannel (id, kind[slack|discord|email|webhook], name, encrypted_target, secret_encrypted,
                     events[], min_severity, repository_ids[] NULL=all, enabled)
 └─< NotificationDelivery (id, channel_id, event, status, attempts, last_error, created_at)
```

Key relationships: Installation 1→N Repository; Repository 1→1 Settings, 1→N PullRequest; PullRequest 1→N ReviewRun; ReviewRun 1→N Finding, 1→N LLMUsage; LLMCredential 1→N Settings / LLMUsage. Findings are immutable per run; dedup and "previously dismissed" are resolved by `fingerprint` across runs of the same PR. `settings_snapshot` freezes the config each run used (reproducibility/debugging).

## 5. API surface sketch

Base `/api/v1`, JSON, session-cookie auth + CSRF (P2: bearer tokens). OpenAPI 3 schema published at `/api/v1/schema/` (drf-spectacular); the frontend's TS types are generated from it. Errors: `{"error": {"code", "message", "details"}}`. Cursor pagination.

| Method & path | Purpose | Pri | Min role |
|---|---|---|---|
| `GET /setup/status`, `POST /setup` | First-run admin creation | P0 | none (only when 0 users) |
| `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Session auth | P0 | — |
| `POST /auth/password-reset`, `/auth/password-reset/confirm` | Reset | P1 | — |
| `GET/POST /users`, `PATCH/DELETE /users/{id}`, `POST /invites`, `POST /invites/{token}/accept` | Team | P1 | admin |
| `GET/POST /llm-credentials`, `GET/PATCH/DELETE /llm-credentials/{id}` | Keys (DELETE = revoke) | P0 | admin |
| `POST /llm-credentials/{id}/validate` | Re-validate | P0 | admin |
| `POST /llm-credentials/{id}/rotate` | Rotate secret | P1 | admin |
| `GET /llm-providers` | Supported providers + known models | P0 | viewer |
| `GET /integrations/github`, `POST /integrations/github/manifest`, `GET /integrations/github/callback` | GitHub App manifest setup | P0 | admin |
| `POST /integrations/github/sync` | Re-sync installations/repos | P0 | admin |
| `GET/POST/PATCH/DELETE /integrations/gitlab` | GitLab connection | P1 | admin |
| `GET /repositories?enabled=&q=` , `GET /repositories/{id}` | Repo list/detail | P0 | viewer |
| `PATCH /repositories/{id}` (`enabled`) | Enable/disable | P0 | admin |
| `GET/PATCH /repositories/{id}/settings` | Per-repo settings | P0 | admin (P1: reviewer for instructions/rules) |
| `GET /pull-requests?repository=&state=&review_status=&min_severity=&risk=` | Unified list | P0 (severity/risk P1) | viewer |
| `GET /pull-requests/{id}`, `GET /pull-requests/{id}/reviews` | PR + run history | P0 / P1 | viewer |
| `POST /pull-requests/{id}/reviews` | Manual re-run (409 if running) | P0 | admin (P1: reviewer) |
| `GET /reviews/{id}`, `GET /reviews/{id}/findings` | Review detail | P0 | viewer |
| `POST /reviews/{id}/cancel` | Cancel queued/running | P1 | reviewer |
| `PATCH /findings/{id}` (`state`, `dismiss_reason`), `PUT /findings/{id}/feedback` | Feedback | P1 | reviewer |
| `GET /usage?group_by=day,repository,credential,model&from=&to=`, `GET /usage/export.csv` | Analytics | P0 raw / P1 UI | admin |
| `GET/POST/PATCH/DELETE /notification-channels`, `POST /notification-channels/{id}/test` | Notifications | P1 | admin |
| `GET/POST/PATCH/DELETE /outbound-webhooks`, `GET /outbound-webhooks/{id}/deliveries` | Outbound hooks | P2 | admin |
| `GET /audit-events?actor=&action=&from=&to=` | Audit | P1 | admin |
| `GET /webhook-deliveries` | Inbound delivery debug log | P0 | admin |
| `POST /webhooks/github` | GitHub webhook receiver (HMAC) | P0 | signature |
| `POST /webhooks/gitlab`, `POST /webhooks/bitbucket` | Receivers | P1 / P2 | token / signature |
| `GET /healthz` (liveness), `GET /readyz` (DB+Redis), `GET /metrics` (Prometheus, optional token) | Ops | P0 | none / token |

(Webhook and health endpoints are outside `/api/v1`, at root.)

## 6. Non-functional requirements

**Secrets.** All third-party secrets (LLM keys, GitHub private key/webhook secret, Slack URLs, SMTP password) encrypted with MultiFernet; master keys only from env/Docker secrets, never in DB. Plaintext exists only in worker memory for the call. Structured-log processor redacts known secret patterns (`sk-…`, `sk-ant-…`, `ghs_…`, `-----BEGIN … KEY-----`) and any field named `*key*|*secret*|*token*|*password*`. Django `SECRET_KEY`, DB passwords via env. Docs include a key-backup warning (lose the master key = lose stored credentials).

**Least-privilege Git scopes.** GitHub App: `pull_requests:write` (reviews include summary body so `issues:write` is not needed), `contents:read`, `metadata:read`, `checks:write` (P1 only). Events: `pull_request`, `installation`, `installation_repositories`, `issue_comment` (P1 commands). GitLab: `api` scope is unavoidable for discussions — document using a dedicated bot user with Developer role per project. Never request `administration`, `workflows`, or org-level permissions.

**Untrusted input / prompt injection.** PR diffs, titles, and descriptions are untrusted (fork PRs from strangers). The LLM has no tools and no write access; its output is schema-validated, mentions neutralized, links rendered as text, length-capped. System prompt delimits user content. Fork PRs reviewed with the same repo key — so cost caps (RE-03, KEY-05) are the DoS defense; optional "don't auto-review PRs from first-time contributors" (P1).

**Background jobs.** Celery + Redis. Queues: `webhooks` (fast), `reviews` (slow, concurrency-limited), `notifications`, `default`; Celery beat for periodic tasks (pruning, stuck-run reaper, GitHub reaction sync P2). `acks_late=True`, `task_reject_on_worker_lost=True`, hard time limit 15 min per review. Per-PR Redis lock so only one run per PR is `running`. Stuck-run reaper marks runs `failed(stage=timeout)` after 30 min. Review pipeline is staged (`fetch_diff → filter → chunk → llm → aggregate → post`) with each stage persisting state so retries resume, not restart. Postgres is source of truth; Redis loss only loses in-flight queue (reaper re-queues `queued` runs older than 10 min).

**Rate limits.** LLM: per-credential Redis token bucket (configurable RPM/TPM), retry 429/5xx with exponential backoff + full jitter honoring `Retry-After`, max 5 attempts. GitHub: read `X-RateLimit-Remaining/Reset`, sleep-until-reset when < 50; handle secondary limits (403 + `Retry-After`) with backoff; post one review per run (single API call) rather than N comments. Outbound HTTP via `httpx` with explicit timeouts (connect 5 s, read 120 s for LLM, 30 s for Git).

**Observability.** JSON logs (structlog) with `request_id`, `delivery_id`, `review_run_id`, `repo` on every line. Prometheus metrics: `reviews_total{status}`, `review_duration_seconds`, `llm_requests_total{provider,model,status}`, `llm_tokens_total{direction}`, `llm_latency_seconds`, `git_api_requests_total{status}`, `queue_depth{queue}`, `webhook_deliveries_total{event,status}`. `/healthz`, `/readyz`. Optional Sentry via `SENTRY_DSN`. Review detail page doubles as the per-run trace.

**Self-hosting.** Docker images (`reviewbot-api` serves Django via gunicorn and also runs worker/beat with a different command; `reviewbot-web` Next.js standalone) published multi-arch (amd64/arm64) to GHCR on tag. `docker-compose.yml`: `web`, `api`, `worker`, `beat`, `postgres:16`, `redis:7`, optional `caddy` (auto-TLS) and `mailpit` (dev). Migrations run in a one-shot `migrate` service. All config via env with `.env.example`; 12-factor. Requirements: 2 vCPU / 2 GB RAM for small teams. Postgres backups documented (`pg_dump` cron). Upgrade path: semver, migrations forward-only, CHANGELOG. Kubernetes Helm chart is P2.

**Quality bar.** Backend: pytest ≥ 85% coverage on `review_engine` and `providers`, ruff, mypy (strict on core modules). Frontend: TypeScript strict, ESLint, Vitest for components, Playwright E2E for the MVP happy path against a fake GitHub + fake LLM server. CI on every PR; `pip-audit` / `npm audit`; CodeQL. Supported browsers: last 2 versions of evergreen browsers.

**Performance targets.** Webhook ack p95 < 500 ms; dashboard API p95 < 300 ms for lists with 10k PRs; end-to-end review for ≤ 500 changed lines < 5 min excluding provider latency outliers.

## 7. Phasing (P0 milestones, dependency order)

| Milestone | Scope (feature IDs) | Exit criteria | Est. (1–2 devs) |
|---|---|---|---|
| **M1 Foundation** | Monorepo, Docker Compose, Django + DRF + Postgres + Celery/Redis, Next.js shell, CI, ADM-01, KEY-01, KEY-02, encryption utils, logging/metrics/health | `docker compose up` → `/setup` → login → add & validate an OpenAI key; CI green | 2–3 wks |
| **M2 GitHub connection** | INT-01 (manifest + install), RE-01 (webhook ingestion), REPO-01, REPO-02, `GitProvider` interface, webhook delivery log | Install App on a test repo; repos appear; enable one; opening a PR creates a `queued` ReviewRun row | 2 wks |
| **M3 Review engine** | RE-02, RE-03, RE-04, RE-05, RE-06, RE-07, RE-08, RE-09, KEY-03 | Opening a PR on the test repo yields a GitHub review with summary + inline comments; usage rows recorded; golden-fixture tests pass | 3–4 wks |
| **M4 PR dashboard + release** | PR-01, PR-02, PR-03, failure surfacing, docs (install, GitHub setup, config reference, security), E2E test, release pipeline → **v0.1.0** | A new user completes the §1 "done" flow from docs on a fresh VM | 2 wks |

Total MVP ≈ 9–11 weeks for 1–2 developers. v1.0 (P1) ≈ another 8–10 weeks, with GitLab (INT-03) the largest single item.

## 8. Non-goals (v1)

- Auto-approving, requesting changes, or **merging** PRs/MRs; the bot only comments (and in P1 sets a check status).
- Hosted SaaS / multi-tenant (multiple isolated orgs per instance), billing, or reselling LLM usage.
- Static analysis engines (Semgrep/CodeQL-equivalent), SAST certification, secret scanning — detection is LLM-based.
- Whole-repo semantic indexing / RAG (P2 spike only), cross-PR reasoning.
- Auto-fix commits or opening fix PRs.
- Automatic prompt tuning/fine-tuning from feedback (feedback is collected only).
- Azure DevOps, Gitea/Forgejo, Gerrit (accept community adapters later).
- Bundling or hosting an LLM; Kubernetes operator; HA/multi-region guidance.
- IDE plugins, pre-commit hooks, chat-with-PR.

## 9. Open questions (decisions for the maintainer)

1. **License: Apache-2.0 vs MIT.** Recommendation: **Apache-2.0** (explicit patent grant, contributor-friendly, compatible with commercial self-hosting). AGPL would block hosted forks but deter corporate adoption.
2. **Default LLM / model.** No default key ships. Which model is pre-selected in the UI and used for prompt tuning/evals (affects JSON-mode reliability)? Recommendation: tune prompts on one OpenAI + one Anthropic model, and test against one local model (e.g. Qwen-coder via Ollama) for the OpenAI-compatible path.
3. **Use a provider library (LiteLLM) or in-house adapters?** Recommendation: in-house thin adapters for 3 providers (small, auditable, exact usage accounting); revisit if users demand 10+ providers.
4. **Single-tenant forever, or a `Workspace` table now?** Adding it later touches every query. Recommendation: single-tenant v1, documented.
5. **Hosting docs scope:** Docker Compose only, or also Helm, Railway/Fly, and a one-click DigitalOcean image? Recommendation: Compose + Caddy for v0.1; Helm community-maintained.
6. **Plugin architecture for custom reviewers:** in-process Python entry points (fast, but code runs with full secrets access) vs. out-of-process HTTP "reviewer" contract (`POST diff-chunk → findings[]`, language-agnostic, sandboxable). Recommendation: HTTP contract in P2; keep `Reviewer` interface internal until then.
7. **Fork PRs from first-time contributors**: review automatically (helps maintainers, costs money) or require a maintainer `/reviewbot review`? Default?
8. **Telemetry:** none (recommended), or opt-in anonymous usage pings?
9. **Name & trademark** ("Reviewbot" is a placeholder and likely taken).
10. **Price table maintenance:** ship a community-updated `pricing.yaml` per release, or require users to enter prices?
