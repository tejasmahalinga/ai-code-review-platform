# Contributing

Thanks for helping. This project is small and maintained by a few people, so focused contributions land fastest.

## Before you start

- Check [`docs/FEATURE_PLAN.md`](docs/FEATURE_PLAN.md). Features have IDs (e.g. `RE-10`) and priorities. Open an
  issue referencing the ID before starting anything larger than a bug fix.
- Items listed under **Non-goals** will not be merged in v1.

## Development setup

See the *Development* section of the [README](README.md). You need Python 3.11+, Node 20+, PostgreSQL 14+ and Redis 6+.

## Checks you must run before opening a PR

```bash
# backend/
ruff check . && ruff format --check .
mypy apps config
pytest

# frontend/
npm run lint
npm run typecheck
npm test
npm run build
```

CI runs the same commands.

## Guidelines

- **Tests are required** for behavior changes. Pure review-engine logic (diff parsing, chunking, ignore rules,
  fingerprints, comment rendering) must be covered by unit tests with fixtures in `backend/tests/fixtures/`.
- **No network in tests.** Mock HTTP with `respx`. Use `FakeGitProvider` and the `fake` LLM provider.
- **Secrets**: never log, return, or persist plaintext secrets. Use `apps.credentials.crypto` for anything
  sensitive stored in the database.
- **Migrations** are forward-only; never edit a migration that has been released.
- Keep PRs small and single-purpose. Use [Conventional Commits](https://www.conventionalcommits.org/) for titles
  (`feat:`, `fix:`, `docs:` …).

## License

By contributing you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).

## Releasing (maintainers)

1. Move the `## [Unreleased]` entries in `CHANGELOG.md` under `## [X.Y.Z] — YYYY-MM-DD`, and bump the versions in
   `backend/pyproject.toml`, `backend/config/settings.py` (`SPECTACULAR_SETTINGS["VERSION"]`),
   `frontend/package.json`, and `deploy/helm/reviewbot/Chart.yaml` (`version` and `appVersion`). Merge to `main`
   once CI is green.
2. Start the release: either push a `vX.Y.Z` tag, or open **Actions → Release → Run workflow** on `main` and enter
   `X.Y.Z` (the workflow then creates the tag).
3. The workflow publishes `ghcr.io/<owner>/reviewbot-{api,web}:X.Y.Z` (amd64 and arm64, with an SBOM and
   provenance), pushes the Helm chart to `oci://ghcr.io/<owner>/charts/reviewbot`, scans the images with Trivy
   (results in the run summary), and creates the GitHub release from the CHANGELOG section.
4. First release only: set each new package's visibility to **Public** in the GitHub package settings.
