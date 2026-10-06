# Reviewbot

Self-hosted, bring-your-own-key AI code review for GitHub pull requests (GitLab and Bitbucket on the roadmap).

Reviewbot installs as a GitHub App, reviews each pull request with the LLM **you** choose (OpenAI, Anthropic, or any
OpenAI-compatible endpoint such as Ollama or vLLM), and posts severity-ranked inline findings plus a summary on the PR.
A web dashboard manages API keys, repositories, per-repo review settings, and the history of every review.

> **Status:** v0.1 (MVP) feature-complete, pre-release. See [`docs/FEATURE_PLAN.md`](docs/FEATURE_PLAN.md) for
> scope, priorities, and the roadmap. "Reviewbot" is a working name.

## Features

- **GitHub App** created in one click through GitHub's manifest flow, with least-privilege permissions.
- **GitLab** (gitlab.com and self-managed): merge request reviews with diff discussions, suggestions, commit
  statuses, and automatic project webhooks.
- **Bring your own key**: OpenAI, Anthropic, and OpenAI-compatible providers (e.g. Ollama). Keys are encrypted at
  rest, never returned by the API, and can be rotated in place.
- **Structured findings** with category and severity, posted as one GitHub review with inline comments, a summary,
  a deterministic risk score, and an optional check run that can gate merges.
- **Incremental reviews** of new pushes, **review profiles** (strict, balanced, lenient, security), team **rules**,
  a per-repo **`.reviewbot.yml`**, and `/reviewbot` **comment commands**.
- **Large-PR safety**: token-budgeted chunking plus hard size limits, so a 20k-line PR cannot run up a surprise bill.
- **Feedback loop**: accept or dismiss findings and vote on them; dismissed findings are not re-posted.
- **Teams**: admin, reviewer, and viewer roles; invites; sign in with GitHub, GitLab, or any OpenID Connect
  provider (Okta, Entra ID, Keycloak) with group-to-role mapping; an append-only audit log.
  Developers need no account, since everything they see happens on the pull request.
- **Dashboard**: PR list with risk and severity filters, review detail, run comparison, re-runs, and usage.
- **Runs anywhere**: Docker Compose on a VM, or the Helm chart on Kubernetes with managed PostgreSQL and Redis.

## Architecture

| Component | Tech |
|---|---|
| API | Django 5 + Django REST Framework (`backend/`) |
| Worker | Celery + Redis (same image as the API) |
| Database | PostgreSQL 16 |
| Dashboard | Next.js (`frontend/`), proxying `/api` to Django so the browser sees one origin |

See [`docs/architecture.md`](docs/architecture.md) for details.

## Quick start (Docker Compose)

```bash
git clone https://github.com/tejasmahalinga/ai-code-review-platform.git
cd ai-code-review-platform/deploy
cp .env.example .env
# Generate secrets and paste them into .env:
python3 -c "import secrets; print(secrets.token_urlsafe(50))"                                  # DJANGO_SECRET_KEY
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"       # REVIEWBOT_ENCRYPTION_KEYS
# ...and set POSTGRES_PASSWORD to any long random string.
docker compose up -d --build
```

Open `http://localhost:3000`, create the first admin account, add an LLM key (or set
`REVIEWBOT_ENABLE_FAKE_PROVIDER=true` to try the offline demo provider), then follow
[`docs/github-app.md`](docs/github-app.md) to connect GitHub. GitHub must be able to reach your instance's
`/webhooks/github` URL (public hostname, or a tunnel for local testing).

Full guide: [`docs/self-hosting.md`](docs/self-hosting.md). Configuration reference: [`docs/configuration.md`](docs/configuration.md).

## Development

```bash
# Backend (needs PostgreSQL and Redis running locally)
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export DJANGO_DEBUG=1 REVIEWBOT_ENABLE_FAKE_PROVIDER=1 \
  DATABASE_URL=postgres://reviewbot:reviewbot@localhost:5432/reviewbot REDIS_URL=redis://localhost:6379/0 \
  REVIEWBOT_ENCRYPTION_KEYS=$(python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
python manage.py migrate
python manage.py runserver
celery -A config worker -l info   # in another shell
pytest

# Frontend
cd frontend
npm install
npm run dev     # http://localhost:3000, proxies /api to http://localhost:8000
```

End-to-end smoke test (whole stack in Docker against a stub GitHub API and the offline demo LLM):

```bash
cd deploy
cp e2e.env .env
docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --build --wait
BASE_URL=http://localhost:3000 FAKE_GITHUB_URL=http://localhost:9000 python3 ../scripts/e2e/smoke_test.py
docker compose -f docker-compose.yml -f docker-compose.e2e.yml down -v && rm .env
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Documentation

- [Feature plan & roadmap](docs/FEATURE_PLAN.md)
- [Self-hosting guide](docs/self-hosting.md) (Docker Compose)
- [Deploying on Kubernetes](docs/kubernetes.md) (Helm)
- [Connecting GitHub](docs/github-app.md)
- [Connecting GitLab](docs/gitlab.md)
- [Single sign-on](docs/sso.md)
- [Configuration reference](docs/configuration.md)
- [Architecture](docs/architecture.md)

## Security

Please report vulnerabilities privately. See [`SECURITY.md`](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
