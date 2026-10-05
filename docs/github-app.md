# Connecting GitHub

Reviewbot authenticates to GitHub as a **GitHub App** that you own. Each instance has its own App, so no
third party ever holds a token for your code.

## Permissions

| Permission | Access | Why |
|---|---|---|
| Pull requests | Read & write | Read PR metadata and changed files, post review comments |
| Contents | Read | Required by GitHub to read file diffs of private repositories |
| Metadata | Read | Mandatory for all Apps |

Events: **Pull request**. Installation events are always delivered to Apps.
The App never requests administration, workflow, or organization permissions, and it never approves,
requests changes, or merges. It only posts reviews of type `COMMENT`.

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

New pushes to an open PR (`synchronize`) update the PR in the dashboard but are not reviewed automatically
in v0.1. Use **Re-run review** in the dashboard. Incremental push reviews are planned (RE-10).
