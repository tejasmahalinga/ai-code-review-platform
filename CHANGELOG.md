# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased] — v0.1.0 (MVP)

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
