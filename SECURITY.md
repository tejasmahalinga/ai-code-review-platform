# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/tejasmahalinga/ai-code-review-platform/security/advisories/new)
for this repository. We aim to acknowledge reports within 3 business days and to ship a fix or mitigation for
confirmed high-severity issues within 30 days.

## Supported versions

Only the latest released minor version receives security fixes during the 0.x series.

## Security model (summary)

- **Secrets at rest**: LLM API keys, the GitHub App private key, and the webhook secret are encrypted with
  `REVIEWBOT_ENCRYPTION_KEYS` (Fernet / MultiFernet). The master key lives only in the environment. **Back it up**:
  losing it makes stored credentials unrecoverable.
- **Secrets in transit/logs**: API responses expose only the last 4 characters of a key. Log output passes through a
  redaction processor.
- **Webhooks** are authenticated with HMAC-SHA256 (`X-Hub-Signature-256`) and de-duplicated by delivery ID.
- **Least privilege**: the GitHub App requests only `pull_requests: write`, `contents: read`, `metadata: read`.
- **Untrusted input**: pull request content, including from forks, is treated as untrusted. The LLM has no tools.
  Its output is schema-validated, `@mentions` are neutralized, and it is only ever posted as a comment.
- **Cost-based abuse**: per-repo size limits cap the tokens any single PR can consume.
- **Dashboard**: session cookies (HttpOnly, SameSite=Lax, Secure in production), CSRF protection, Argon2 password
  hashing, and login rate limiting.
