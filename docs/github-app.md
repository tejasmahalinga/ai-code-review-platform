# Connecting GitHub

Reviewbot authenticates to GitHub as a **GitHub App** that you own. Each instance has its own App, so no
third party ever holds a token for your code.

## Permissions

| Permission | Access | Why |
|---|---|---|
| Pull requests | Read & write | Read PR metadata and changed files, post review comments |
| Contents | Read | Required by GitHub to read file diffs of private repositories |
| Metadata | Read | Mandatory for all Apps |
| Checks | Read & write | Report a "Reviewbot" check run per reviewed commit (optional) |
| Email addresses (account permission) | Read | "Sign in with GitHub": read the signed-in user's verified emails |

Events: **Pull request** and **Issue comment** (for `/reviewbot` commands). Installation events are always
delivered to Apps. Apps created before v0.2 should enable the *Issue comment* event in the App settings.
The App never requests administration, workflow, or organization permissions, and it never approves,
requests changes, or merges. It only posts reviews of type `COMMENT` and, optionally, check runs.

> **Apps created before v0.2** lack the Checks permission. Add *Checks: Read & write* in the App's settings
> on GitHub, then accept the updated permissions on each installation. Until then reviews work normally
> and check runs are skipped (the worker logs `github.checks_permission_missing`).

## Option 1: one-click setup (manifest flow)

1. Make sure `REVIEWBOT_PUBLIC_URL` is set and reachable from GitHub.
2. In the dashboard, go to **Integrations → Create GitHub App**. Choose a name (it must be unique on GitHub)
   and, optionally, an organization to own the App.
3. GitHub shows the App it is about to create. Confirm it.
4. You are redirected back to Reviewbot. The App ID, private key, and webhook secret are stored encrypted.
5. Click **Install on repositories**, pick the repositories, and confirm. You land on the Repositories page.
6. Enable reviews for the repositories you want and check their settings.

## Option 2: existing App (manual)

Use this for GitHub Enterprise versions without manifest support, or when the App must be created by
someone else.

1. On GitHub, create an App (*Settings → Developer settings → GitHub Apps → New*):
   - Webhook URL: `https://<your-host>/webhooks/github`
   - Webhook secret: a long random string
   - Permissions and events as listed above
2. Generate a private key and download the `.pem` file.
3. In Reviewbot, go to **Integrations → I already have a GitHub App** and enter the App ID, the slug (from
   `github.com/apps/<slug>`), the webhook secret, and the private key contents.

## GitHub Enterprise Server

Set both variables in `.env` before connecting:

```
GITHUB_URL=https://github.example.com
GITHUB_API_URL=https://github.example.com/api/v3
```

## What happens on a pull request

1. GitHub sends `pull_request` (`opened`, `reopened`, or `ready_for_review`) to `/webhooks/github`.
2. Reviewbot verifies the HMAC signature, records the delivery (de-duplicated by delivery ID), and queues a
   review if the repository is enabled, auto-review is on, and the PR is not a draft (unless drafts are
   enabled).
3. A worker fetches the changed files, drops ignored, deleted, and binary files, splits the diff into chunks,
   and asks the LLM for structured findings.
4. Reviewbot posts **one** review: inline comments on changed lines (highest severity first, capped per
   repository) plus a summary. Findings it cannot attach to a changed line are listed in the summary.
5. Re-running a review never re-posts a comment that was already posted on the PR.

## New commits (incremental reviews)

When new commits are pushed to an open PR (`synchronize`), Reviewbot waits `REVIEWBOT_PUSH_DEBOUNCE_SECONDS`
(default 60). A newer push in that window replaces the queued review. Reviewbot then compares the last reviewed
commit with the new head and sends **only the files changed since then** to the LLM. Inline comments are still
anchored to the pull request's diff. After a force push, when the last reviewed commit is no longer an ancestor,
it falls back to a full review. You can turn this off per repository with *Review new commits pushed to open
pull requests*.

## Comment commands

Maintainers can comment `/reviewbot review`, `/reviewbot ignore`, or `/reviewbot resume` on a pull request.
See [configuration.md](configuration.md#pr-comment-commands).

## Check runs

With *Report a "Reviewbot" check run* enabled (the default), every reviewed commit gets a check:

| Outcome | Conclusion |
|---|---|
| No findings | `success` |
| Findings, none at or above the gate severity | `neutral` |
| A finding at or above the gate severity | `failure` (use with branch protection to block merging) |
| Review skipped, or superseded by a newer push | `skipped` |
| Review failed (LLM or GitHub outage) | `neutral`: an outage never blocks merges |

The gate severity is off ("never fail") by default.

## Sign in with GitHub

The GitHub App doubles as the dashboard's login provider. Apps created through the manifest flow in v0.2 or
later are configured automatically. Older Apps need two changes in the App's settings on GitHub:

1. Under **Identifying and authorizing users**, add the callback URL `https://<your-host>/api/v1/auth/github/callback`.
2. Under **Permissions → Account permissions**, set **Email addresses** to *Read-only*, and accept the change on
   each installation.

With a manually configured App, also enter its **Client ID** and **Client secret** in the manual setup form (or set
`REVIEWBOT_GITHUB_OAUTH_CLIENT_ID` / `_SECRET`). The user's GitHub token is used once to read their profile and
verified emails, then discarded; it is never stored.

GitHub only lets users of the owning account authorize a **private** App. If people outside that organization
need to sign in, either make the App public (it still can't be installed without your approval) or use a separate
OAuth App through `REVIEWBOT_GITHUB_OAUTH_CLIENT_ID` / `_SECRET`.
