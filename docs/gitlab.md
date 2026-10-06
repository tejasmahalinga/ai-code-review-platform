# Connecting GitLab

Reviewbot reviews GitLab merge requests on gitlab.com and on self-managed GitLab (15.0 or later). It does
everything it does on GitHub:

- inline comments with one-click suggestions;
- a summary comment and a risk score;
- incremental reviews of new pushes;
- `.reviewbot.yml`, rules and profiles;
- `/reviewbot` comment commands;
- a status on the merge request.

GitHub and GitLab can be connected at the same time.

## How Reviewbot authenticates

GitLab has no equivalent of GitHub Apps, so Reviewbot acts as a GitLab user through an access token. Use a
dedicated bot account so its comments are clearly Reviewbot's:

| Option | Where | Notes |
|---|---|---|
| Bot user + personal access token | Any tier | Create a user such as `reviewbot` and add it to the groups or projects to review. |
| Group access token | Premium/Ultimate, or self-managed | Creates a bot user scoped to one group. |
| Project access token | Per project | Fine for trying Reviewbot on a single project. |

The token needs the **`api`** scope. Reviewbot checks the scope on connect where GitLab exposes it (15.5+). The
token is encrypted at rest and never returned by the API.

| Role on a project | Lets Reviewbot |
|---|---|
| Developer | Read merge requests and diffs, comment, and set commit statuses. |
| Maintainer | Also add the project webhook automatically when you enable the project. |

## Setup

1. In Reviewbot, open **Integrations → GitLab**. Enter the GitLab URL (`https://gitlab.com` or your instance) and
   the token, then click **Connect GitLab**. Reviewbot lists every project where the bot has Developer access or
   higher.
2. On **Repositories**, choose an LLM key for the project and enable it.
   - If the bot is a **Maintainer**, Reviewbot creates the project webhook (merge request and comment events) with
     a secret token.
   - Otherwise the repository shows **webhook not managed**. Add the webhook by hand under the project's
     **Settings → Webhooks**:
     - URL: `https://<your-host>/webhooks/gitlab`, also shown on the Integrations page.
     - Secret token: the one shown on the Integrations page.
     - Triggers: **Merge request events** and **Comments**. Keep SSL verification on.
3. Open a merge request. Reviewbot reviews it within a minute.

Click **Sync projects** after adding the bot to new projects.

## What happens on a merge request

| GitLab event | Reviewbot |
|---|---|
| Opened or reopened | Full review. |
| New commits pushed (`update` with `oldrev`) | Incremental review of the changes since the last reviewed commit, debounced like on GitHub. |
| Marked ready (draft → ready) | Full review. Drafts are skipped unless enabled in the repository settings. |
| Comment `/reviewbot review`, `ignore`, or `resume` | Same as on GitHub. Only members with Developer access or higher can use them; others are ignored. |
| Title edits, merge, close | The dashboard is updated; no review. |

The review appears on the merge request as:

- **Inline findings**: one diff discussion per finding, anchored to the reviewed commit's diff version.
  Suggestions use GitLab's `suggestion:-N+0` syntax, so they can be applied with one click. If GitLab refuses a
  position (for example when the diff changed meanwhile), that finding moves into the summary note instead.
- **Summary**: one MR note with the counts, the risk score, and anything not posted inline.
- **Status**: an external commit status named `Reviewbot`, "running" while reviewing.

| Review outcome | Status | Description |
|---|---|---|
| No findings | `success` | No issues found |
| Findings below the gate | `success` | N finding(s) |
| Finding at or above the gate severity | `failed` | N finding(s) at or above X |
| Skipped (too large, budget) or failed review | `success` | Review skipped / failed |
| Superseded by a newer push | `canceled` | Superseded |

Only the gate produces `failed`, so a Reviewbot outage never blocks merges when **Pipelines must succeed** is on.

## Webhook security

GitLab sends the secret token in the `X-Gitlab-Token` header, and Reviewbot compares it in constant time. Each
delivery is recorded once: by `Idempotency-Key`, `X-Gitlab-Event-UUID`, or, on older GitLab, a hash of the body. A
redelivered event never queues a second review.

## Self-managed GitLab

- Reviewbot must reach the GitLab API, and GitLab must reach Reviewbot's `/webhooks/gitlab`.
- GitLab blocks webhooks to private networks by default. If Reviewbot runs on an internal address, allow it under
  **Admin → Settings → Network → Outbound requests**.
- A GitLab instance with a self-signed certificate must be trusted by the API and worker containers (add the CA to
  the image or mount it).

## Limitations

- Sign-in with GitLab is not available yet; people sign in with a password, an invite, or GitHub.
- GitLab has no atomic multi-comment review. If the GitLab API fails part-way through posting, a retried review can
  repeat the inline comments posted before the failure. The summary note is posted last.
