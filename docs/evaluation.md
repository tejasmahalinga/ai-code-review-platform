# Evaluating review quality

Reviewbot ships an evaluation harness. It runs the real review engine (prompt, chunking, code context, anchoring,
consolidation) over a set of known pull requests and scores the result. Use it to:

- compare models or providers before switching a team to them;
- check that a prompt, profile or setting change helps instead of hurting;
- gate CI on a minimum quality level for your own cases.

It never touches GitHub, GitLab or the database. It only calls the LLM.

## Running it

```bash
cd backend

# A stored key from the dashboard (id from Settings → LLM keys)
python manage.py evaluate_reviews --credential 3 --model gpt-4.1

# Or a provider and a key from the environment
REVIEWBOT_EVAL_API_KEY=sk-... python manage.py evaluate_reviews --provider openai --model gpt-4.1
REVIEWBOT_EVAL_API_KEY=... python manage.py evaluate_reviews --provider openai_compatible \
    --base-url http://localhost:11434/v1 --model qwen2.5-coder:32b
```

With Docker Compose, run it in the API container: `docker compose exec api python manage.py evaluate_reviews ...`.

| Option | Description |
|---|---|
| `--cases DIR` | Directory of case files (default `backend/evals/cases`). |
| `--case NAME` | Run only this case; repeatable. |
| `--credential ID` / `--provider NAME` | Which LLM to use: a stored key, or `openai`, `anthropic`, `openai_compatible`. |
| `--model`, `--base-url`, `--api-key-env` | Model, endpoint, and the environment variable holding the key (default `REVIEWBOT_EVAL_API_KEY`). |
| `--profile` | Review profile (`strict`, `balanced`, `lenient`, `security`). Default `balanced`. |
| `--no-context` | Disable surrounding-code context, to measure what it adds. |
| `--min-confidence` | Confidence threshold, like the repository setting. Default `0.5`. |
| `--out FILE` / `--markdown FILE` | Write the JSON report and a Markdown summary. |
| `--baseline FILE` | An earlier JSON report. The Markdown summary shows the change for each metric. |
| `--min-recall X` / `--max-false-positives N` | Exit with an error when the result is worse. Use these in CI. |

## Metrics

| Metric | Meaning |
|---|---|
| Recall | Expected issues found ÷ expected issues. |
| Precision | Findings that match an expected issue ÷ all reported findings. |
| False positives | Findings inside a `quiet` region, where the case says nothing is wrong. |
| Unexpected | Findings that match neither an expectation nor a quiet region. These are not necessarily wrong; read them. |
| Tokens | Input and output tokens, to compare cost. |

A finding matches an expectation when it is on the same file and within 3 lines of the expected range, and the
category, minimum severity and keywords match when the case sets them. Keywords match when at least one appears in
the title or body (case-insensitive).

LLM output varies between runs. Compare reports run on the same cases, and run important comparisons more than
once.

## Writing cases

A case is a YAML file in the cases directory:

```yaml
description: A refactor dropped an await, so the code reads properties of a Promise.
title: Simplify profile loading          # PR title, as the model sees it
files:
  - path: web/profile.ts
    status: modified                     # added | modified | removed | renamed
    source: |                            # optional: the full new file, used for surrounding-code context
      ...
    patch: |                             # the unified diff, as GitHub returns it
      @@ -1,8 +1,8 @@
      ...
expected:                                # issues the review must find
  - path: web/profile.ts
    lines: [4, 5]                        # new-file line range
    category: bug                        # optional
    min_severity: medium                 # optional
    keywords: [await, promise]           # optional: one must appear in the finding
quiet:                                   # regions where any finding is a false positive
  - path: shop/pricing.py                # whole file
  - path: shop/cart.py
    lines: [10, 30]
```

Good cases come from real reviews:

- a bug that reached production, as the PR that introduced it;
- a finding your team dismissed as a false positive, as a `quiet` case;
- a clean refactor, which should produce no findings at all.

Keep the diffs small and the expectations specific. Seed cases are in `backend/evals/cases`.

## In CI

CI runs the harness with the offline demo provider. This only checks that the cases and the command work; it does
not measure quality. To gate on quality, add a job that runs your cases against a real model with an API key from
your CI secrets:

```bash
python manage.py evaluate_reviews --provider anthropic --model <model> \
    --out eval.json --markdown eval.md --min-recall 0.8 --max-false-positives 0
```
