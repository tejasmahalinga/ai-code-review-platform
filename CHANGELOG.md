# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Single sign-on (ADM-07):** sign in with any OpenID Connect provider (Okta, Microsoft Entra ID, Keycloak,
  Google) and with GitLab.
  - Uses PKCE, `state`, and `nonce`. ID tokens are verified against the provider's JWKS (asymmetric algorithms
    only).
  - Identities are matched by subject, an invite, or a verified email.
  - Optional group-to-role mapping provisions accounts, syncs roles at each sign-in, and denies people outside
    the allowed groups. It never demotes the last admin.
  - `REVIEWBOT_PASSWORD_LOGIN=admins|none` enforces SSO, keeping an admin break-glass option.
  - New **Account → Single sign-on** card for linking and unlinking. See `docs/sso.md`.

## [0.4.0] — 2026-10-06

### Added
- **GitLab support (INT-03):** reviews merge requests on gitlab.com and self-managed GitLab 15+.
  - Connects with a bot user's access token.
  - Syncs projects, and creates project webhooks automatically when the bot is a Maintainer (otherwise adds them by
    hand with the shown URL and secret).
  - Supports incremental reviews, `/reviewbot` commands for Developer+ members, and `.reviewbot.yml`.
  - Posts inline findings as diff discussions with GitLab suggestions and the summary as a note, and reports a
    `Reviewbot` commit status (only the severity gate fails it).
  - See `docs/gitlab.md`.

### Changed
- The Git provider layer is now provider-neutral: inline comments a provider refuses one by one are moved into the
  summary and marked "in summary".

### Fixed
- GitLab instances on internal host names without a dot (for example `http://gitlab:8080`) can be connected.

### Upgrading
- Migrations run automatically. To review GitLab merge requests, follow `docs/gitlab.md`: connect a bot user's
  access token under **Integrations → GitLab**, then enable projects.
- Existing GitHub setups need no changes.

## [0.3.0] — 2026-10-06

### Added
- **Pricing (KEY-05):** an editable model price table, seeded with list prices for common OpenAI and Anthropic
  models. Every LLM call and review now records its cost. Unpriced calls are flagged, and can be backfilled with
  `POST /api/v1/model-prices/recalculate`.
- **Monthly budgets per LLM key:** an alert at 80% and at 100% (audit event, plus email when SMTP is configured).
  At 100%, automatic reviews with that key are skipped, with a PR notice and a skipped check run. Only admins can
  still run reviews by hand.
- **Usage and cost page (ADM-05):** cost, reviews, tokens, budgets, a daily cost chart, and breakdowns by repository
  and model. `GET /api/v1/usage` gains `cost_usd`, `unpriced`, `reviews`, and `&export=csv`.
- **Notifications (INT-04, INT-05, INT-07):** Slack, email, and signed JSON webhook channels for high-risk pull
  requests (risk or severity thresholds, optional repository filter), failed reviews, budget alerts, and a weekly
  digest. Includes delivery logs, test sends, retries, at-most-once delivery per event, encrypted URLs, and SSRF
  protection. New **Notifications** page.
- **Personal API tokens (ADM-06):** `Authorization: Bearer rbt_…`. A token acts with its owner's role, has an
  optional expiry, is hashed at rest, and cannot mint more tokens.

### Changed
- Release notes are generated from this changelog, and release images carry an SBOM and provenance attestation.
  CI and releases scan the images with Trivy, and CI audits production npm dependencies. Dependabot is enabled,
  and the GitHub Actions now run on Node 24.

### Upgrading
- Migrations run automatically (the Compose `migrate` service, or the Helm migration Job). A migration installs the
  default model prices. Check them under **LLM keys → Model prices**, since your contract prices may differ.
- Reviews made before 0.3.0 have no recorded cost. Usage pages show them as unpriced until you run
  **Recalculate unpriced usage**.

## [0.2.0] — 2026-10-06

### Added
- **Incremental reviews on push (RE-10):** `synchronize` events queue a debounced review of only the changes
  since the last reviewed commit; newer pushes supersede queued ones; force pushes fall back to a full review.
- **GitHub check runs (INT-02):** a "Reviewbot" check per reviewed commit with an optional severity gate.
  Failed reviews conclude `neutral`, so an outage never blocks merges. The GitHub App manifest now requests
  `checks: write`.
- **Review profiles (RE-11):** strict, balanced, lenient, and security-focused. Each profile has its own prompt
  focus, category filter, and threshold presets. Available at `GET /api/v1/review-profiles`.

- **Review rules (RE-14):** structured team rules with path globs and severities. Findings that violate a rule are
  tagged and raised to the rule's severity.
- **`.reviewbot.yml` (RE-15):** repository-level settings (profile, thresholds, ignore patterns, instructions,
  rules), read from the PR's base commit. Invalid files never block a review.
- **PR comment commands (RE-17):** `/reviewbot review`, `/reviewbot ignore`, and `/reviewbot resume` for repository
  members and collaborators. The App manifest now subscribes to `issue_comment`.
- **Base-branch filters (REPO-03):** limit auto-reviews to PRs into matching branches.

- **Feedback (RE-16):** accept or dismiss findings (with a reason) and vote 👍/👎. Dismissed findings are not
  re-posted on the same PR. Per-category acceptance stats are at `GET /api/v1/feedback-stats`.
- **Risk score (RE-12):** a deterministic 0-100 score from findings, change size, and sensitive paths, shown in the
  summary, the check run, and the dashboard.
- **Test suggestions (RE-13):** when source changes come without tests, the model is asked for a test finding
  (capped at medium). Can be turned off per repository.
- **Run comparison (PR-04):** `GET /api/v1/reviews/{id}/compare?with={id}` lists new and resolved findings.
- **Roles and invites (ADM-02):** admin, reviewer, and viewer roles enforced on every endpoint. Admins invite people
  by single-use link (emailed when SMTP is configured), change roles, and deactivate users. The last admin cannot
  be removed. Reviewers can re-run reviews, triage findings, and edit review rules. New **Team** and **Account**
  pages.
- **Sign in with GitHub (ADM-03):** uses the GitHub App's OAuth client. Accounts are matched by GitHub ID or a
  verified email, or created from an invite. Optional self-signup can be limited to email domains. The App
  manifest now requests the `emails: read` account permission and an OAuth callback URL.
- **Audit log (ADM-04):** an append-only record of sign-ins, key changes (including the new
  `POST /llm-credentials/{id}/rotate`), settings diffs, user and integration changes, review requests, and
  finding triage. Secrets are redacted, a database trigger blocks updates, and retention defaults to 365 days.
  New **Audit log** page.
- **Kubernetes:** Helm chart (`deploy/helm/reviewbot`) with a migration Job, HPAs, PDBs, non-root read-only pods,
  and an ingress. See `docs/kubernetes.md`.
- **PR filters (PR-05):** filter by minimum severity and risk bucket; search titles, authors, and finding titles.

### Fixed
- Docker Compose: worker and beat now have healthchecks, so `docker compose up --wait` succeeds; gunicorn's
  control socket is disabled to avoid a permission error under the non-root user.

## v0.1.0 (MVP)

### Added
- GitHub App integration via the manifest flow (or manual App configuration), installation and repository
  sync, signed and idempotent webhook ingestion with a delivery log.
- Review engine: diff parsing with line mapping, gitignore-style ignore rules with defaults, token-budgeted
  chunking, cost guards, structured findings (category, severity, confidence, suggestion) validated against
  a JSON schema, inline and summary comments in one GitHub review, de-duplication across runs.
- LLM providers: OpenAI, Anthropic, any OpenAI-compatible endpoint (Ollama, vLLM, ...), and an offline demo
  provider. Keys are encrypted at rest, validated on save, and revocable. Per-call token usage is recorded.
- Dashboard: first-run setup, login, LLM keys, integrations, repositories with per-repo settings, unified
  pull request list with filters, review detail with posted/suppressed findings, manual re-run.
- Operations: Docker images, Docker Compose stack with optional Caddy HTTPS, JSON logs with secret
  redaction, health/readiness/Prometheus endpoints, stuck-run reaper, master-key rotation command.
- CI (lint, types, tests, build, Compose end-to-end smoke test) and a release workflow publishing multi-arch
  images to GHCR.
