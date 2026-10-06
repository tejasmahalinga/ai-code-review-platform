# Self-hosting guide

> Running on Kubernetes or a managed cloud? See [Deploying on Kubernetes](kubernetes.md) for the Helm chart.

This guide takes a fresh Linux VM to a working Reviewbot instance in about 30 minutes.

## Requirements

- Docker Engine 24+ with the Compose plugin.
- 2 vCPU and 2 GB RAM for a small team. Review work runs on the worker and mostly waits on the LLM.
- A hostname that GitHub can reach over HTTPS, e.g. `reviewbot.example.com`. For local testing, a tunnel
  such as `cloudflared` or `ngrok` pointing at port 3000 works.
- An API key for OpenAI or Anthropic, or an OpenAI-compatible endpoint such as Ollama or vLLM.

## 1. Configure

```bash
git clone https://github.com/tejasmahalinga/ai-code-review-platform.git
cd ai-code-review-platform/deploy
cp .env.example .env
```

Edit `.env` and set at least:

| Variable | How to generate |
|---|---|
| `DJANGO_SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `REVIEWBOT_ENCRYPTION_KEYS` | `python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `POSTGRES_PASSWORD` | any long random string |
| `REVIEWBOT_PUBLIC_URL` | `https://reviewbot.example.com` (no trailing slash) |

> **Back up `REVIEWBOT_ENCRYPTION_KEYS`.** Every stored LLM key and the GitHub App private key are
> encrypted with it. If you lose it, you have to re-enter those secrets.

## 2. Start

### Option A: built-in HTTPS with Caddy (recommended on a VM)

Point a DNS record at the VM, then:

```bash
echo "REVIEWBOT_DOMAIN=reviewbot.example.com" >> .env
docker compose --profile caddy up -d --build
```

Caddy obtains a Let's Encrypt certificate automatically. Ports 80 and 443 must be open.

### Option B: behind your own reverse proxy

```bash
docker compose up -d --build
```

The dashboard listens on port 3000 (`REVIEWBOT_HTTP_PORT`). Proxy your HTTPS hostname to it. The dashboard
forwards `/api/*` and `/webhooks/*` to the API container. If your proxy terminates TLS, also set
`REVIEWBOT_BEHIND_PROXY=true`.

### Using pre-built images

Release images are published to GHCR. To use them instead of building locally:

```bash
echo "REVIEWBOT_API_IMAGE=ghcr.io/tejasmahalinga/reviewbot-api:0.1" >> .env
echo "REVIEWBOT_WEB_IMAGE=ghcr.io/tejasmahalinga/reviewbot-web:0.1" >> .env
docker compose pull && docker compose up -d --no-build
```

The pre-built web image proxies to `http://api:8000`, the Compose service name. That is the right target
for the bundled Compose file.

## 3. First run

1. Open `REVIEWBOT_PUBLIC_URL` and create the admin account. You can also do this headless:
   `docker compose run --rm api manage createadmin --email you@example.com`.
2. **LLM keys**: add a key. It is validated against the provider before it is saved.
3. **Integrations**: create the GitHub App. See [github-app.md](github-app.md).
4. **Repositories**: install the App on some repositories, then enable reviews per repository and review
   its settings.
5. Open a pull request. A review appears within a minute or two, depending on the model.

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f api worker` (JSON lines; set `LOG_FORMAT=console` for human-readable output) |
| Health | `GET /healthz` (liveness), `GET /readyz` (database + Redis) |
| Metrics | `GET /metrics` on the API container (Prometheus format; protect it with `REVIEWBOT_METRICS_TOKEN`) |
| Scale workers | `docker compose up -d --scale worker=3` or raise `CELERY_CONCURRENCY` |
| Upgrade | `git pull && docker compose up -d --build`. Migrations run automatically in the `migrate` service. |
| Backup | `docker compose exec postgres pg_dump -U reviewbot reviewbot > backup.sql`, plus a copy of `.env` |
| Restore | `docker compose exec -T postgres psql -U reviewbot reviewbot < backup.sql` |
| Rotate the master key | Prepend a new key to `REVIEWBOT_ENCRYPTION_KEYS`, restart, run `docker compose run --rm api manage rotate_encryption_key`, then remove the old key and restart again |

Redis holds only the job queue. If it is lost, a periodic reaper re-queues reviews that are still queued after
10 minutes and fails runs that have been running for more than 30 minutes.

## Troubleshooting

- **No review appears.** Open *Integrations → Recent webhook deliveries*. If nothing arrived, GitHub cannot reach
  `REVIEWBOT_PUBLIC_URL/webhooks/github`; check the App's "Advanced" tab on GitHub for delivery errors. If a
  delivery was `ignored`, the reason column says why: repository disabled, draft, and so on.
- **Review failed.** The review page shows the failing stage and error. `llm_auth_failed` means the key was
  rejected and has been marked invalid. `git_access_denied` usually means the App was uninstalled from the
  repository.
- **"CSRF verification failed".** `REVIEWBOT_PUBLIC_URL` must match the URL in your browser exactly, including
  scheme and port.
