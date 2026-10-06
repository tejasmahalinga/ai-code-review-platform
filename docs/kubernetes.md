# Deploying on Kubernetes and in the cloud

The Helm chart in [`deploy/helm/reviewbot`](../deploy/helm/reviewbot) runs Reviewbot on any Kubernetes 1.25+
cluster (EKS, GKE, AKS, k3s and others). PostgreSQL and Redis are external, so use your cloud's managed services.
For a single VM, the [Docker Compose guide](self-hosting.md) is simpler.

## What gets deployed

```
                       Ingress (TLS)
             ┌─────────────┴──────────────────────────┐
  /api, /webhooks, /healthz, /readyz                  /  (everything else)
             │                                        │
     Service <release>-api                    Service <release>-web
             │                                        │
  Deployment api (gunicorn, N replicas)       Deployment web (Next.js, N replicas)
             │ enqueue reviews
             ▼
  Managed Redis ◄──── Deployment worker (Celery, N replicas): fetch diff → LLM → post review
             ▲        Deployment beat (Celery beat, exactly 1): cleanup, stuck-run recovery
             │
  Managed PostgreSQL ◄── Job migrate-<revision> (runs once per release; other pods wait for it)
```

| Component | Scales | Notes |
|---|---|---|
| `api` | Horizontally (HPA on CPU) | Stateless. Sessions live in PostgreSQL. |
| `worker` | Horizontally (HPA on CPU or replicas) | Does the actual reviews. Throughput ≈ replicas × `worker.concurrency`. |
| `beat` | Always 1 | Uses the `Recreate` strategy so two schedulers never overlap. |
| `web` | Horizontally | Stateless dashboard. |
| `migrate` | One Job per `helm upgrade` | A plain Job, not a Helm hook, so Argo CD and Flux work too. API, worker and beat pods wait in an init container until `manage.py migrate --check` passes. |

All pods run as non-root (UID 10001) with a read-only root filesystem, no Linux capabilities, and no
Kubernetes API token. They only need outbound HTTPS to GitHub and to your LLM provider.

## Prerequisites

- PostgreSQL 14 or later, for example RDS, Cloud SQL or Azure Database for PostgreSQL. Use `?sslmode=require`
  in the URL when the database requires TLS.
- Redis 6 or later, for example ElastiCache, Memorystore or Azure Cache. Use a `rediss://` URL for TLS. Redis is
  only a queue and cache, so it needs no persistence.
- An ingress controller and a certificate, for example ingress-nginx with cert-manager, or your cloud's load
  balancer controller.
- A public hostname that GitHub can reach, for webhooks.

## Install

1. Create the secrets. The recommended way is an existing Secret, managed by External Secrets, Sealed Secrets or
   your cloud's secret manager:

   ```bash
   kubectl create namespace reviewbot
   kubectl -n reviewbot create secret generic reviewbot-secrets \
     --from-literal=DJANGO_SECRET_KEY="$(openssl rand -base64 48)" \
     --from-literal=REVIEWBOT_ENCRYPTION_KEYS="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" \
     --from-literal=DATABASE_URL='postgres://reviewbot:...@db.internal:5432/reviewbot?sslmode=require' \
     --from-literal=REDIS_URL='rediss://:...@redis.internal:6379/0'
   ```

   **Back up `REVIEWBOT_ENCRYPTION_KEYS`.** Without it, the stored LLM keys and GitHub App credentials cannot be
   decrypted.

2. Install the chart:

   ```bash
   helm install reviewbot ./deploy/helm/reviewbot -n reviewbot \
     --set publicUrl=https://reviewbot.example.com \
     --set ingress.host=reviewbot.example.com \
     --set ingress.className=nginx \
     --set 'ingress.annotations.cert-manager\.io/cluster-issuer=letsencrypt' \
     --set 'ingress.tls[0].secretName=reviewbot-tls' \
     --set 'ingress.tls[0].hosts[0]=reviewbot.example.com' \
     --set secrets.existingSecret=reviewbot-secrets
   ```

   Images default to `ghcr.io/tejasmahalinga/reviewbot-{api,web}:<appVersion>`, which the release workflow
   publishes for amd64 and arm64. Override `image.*` to use your own registry.

3. Open `https://reviewbot.example.com/setup` and create the first admin. To do it without the browser:

   ```bash
   kubectl -n reviewbot exec -it deploy/reviewbot-api -- python manage.py createadmin --email you@example.com
   ```

4. Connect GitHub under **Integrations** (see [Connecting GitHub](github-app.md)), add an LLM key, then invite your
   team under **Team**.

## Configuration

Put non-secret settings under `config`, which becomes a ConfigMap, and secret ones under `secrets.extra` or your
existing Secret. Every variable is listed in the [configuration reference](configuration.md).

```yaml
config:
  REVIEWBOT_ALLOW_SIGNUP: "true"               # GitHub users with a verified @example.com email may join
  REVIEWBOT_SIGNUP_EMAIL_DOMAINS: "example.com"
  REVIEWBOT_EMAIL_HOST: smtp.example.com       # send invite emails
  REVIEWBOT_EMAIL_FROM: reviewbot@example.com
secrets:
  existingSecret: reviewbot-secrets            # add REVIEWBOT_EMAIL_PASSWORD to it
worker:
  replicas: 4
  concurrency: 8
```

Changing `config` or a chart-managed Secret rolls the pods automatically, because the templates carry checksum
annotations. If you change an existing Secret, restart the pods yourself with
`kubectl rollout restart deploy -l app.kubernetes.io/instance=reviewbot`.

### Routing

The ingress sends `/api`, `/webhooks`, `/healthz` and `/readyz` straight to the API and everything else to the
dashboard. The published web image also proxies `/api` to `http://api:8000`. That value is fixed when the image
is built, so the chart adds a Service named `api` (`service.apiAlias`). Set `service.apiAlias=false` if you install
two releases in one namespace; routing through the ingress still works.

### Health and metrics

- `GET /healthz`: liveness. `GET /readyz`: the database and Redis are reachable.
- `GET /metrics`: Prometheus metrics, protected by a bearer token when `REVIEWBOT_METRICS_TOKEN` is set.
- Django only accepts known host names, and the probes send `Host: localhost`. If you scrape metrics, scrape
  through the `<release>-api` Service rather than pod IPs; that Service name is in `DJANGO_ALLOWED_HOSTS`.

## Upgrades and rollbacks

```bash
helm upgrade reviewbot ./deploy/helm/reviewbot -n reviewbot --reuse-values --set image.api.tag=0.3.0 --set image.web.tag=0.3.0
```

The new revision's migration Job runs first, and the new pods start once it has finished. Old pods keep serving
while that happens, so releases aim to keep migrations compatible with the previous version. A Helm rollback does
not reverse database migrations, so take a database snapshot before upgrading across minor versions.

## Sizing guide

| Team | API | Workers × concurrency | Database | Redis |
|---|---|---|---|---|
| Up to 50 developers | 2 × (0.25 CPU, 512 MiB) | 2 × 4 | 2 vCPU / 4 GiB | smallest tier |
| Up to 500 developers | 3 × (0.5 CPU, 512 MiB) | 4 × 8 | 4 vCPU / 8 GiB | small tier |

Reviews spend most of their time waiting on the LLM, so worker concurrency can be well above the CPU count. Your
LLM provider's rate limits are usually the real ceiling. Reviewbot backs off and retries when it hits a 429.

## Security checklist

- [ ] TLS on the ingress, and `publicUrl` uses `https://` (session and CSRF cookies are then `Secure`).
- [ ] Secrets come from `secrets.existingSecret`, not from chart values in Git.
- [ ] `REVIEWBOT_ENCRYPTION_KEYS` is backed up somewhere other than the cluster.
- [ ] The database is reachable only from the cluster, and backups are enabled.
- [ ] Optional: a NetworkPolicy that allows ingress → api/web, pods → PostgreSQL/Redis, and egress to GitHub and
      your LLM endpoint only.
- [ ] People join by invite. Self-signup stays off, or is limited to your email domain.
- [ ] Check the audit log (**Audit log** in the dashboard) after setup.
