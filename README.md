<div align="center">

# CodeReviewAgent

**An AI reviewer for GitLab merge requests that reads your repository, not just the diff.**

One specialist agent per changed file, a real checkout of the branch under review,
and a comment pipeline built to post fewer, better findings.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.13-blue.svg)
![GitLab](https://img.shields.io/badge/GitLab-merge%20requests-orange.svg)

[Quickstart](#quickstart) · [How it works](#how-it-works) · [Configuration](#configuration) · [Analytics](#analytics-and-feedback) · [Contributing](CONTRIBUTING.md)

</div>

---

## Why another AI reviewer?

Most AI reviewers see a diff and guess. Their most common false positives are exactly
the ones a diff produces: "this symbol is undefined" (it is defined in a file the diff
does not show), "this method is never implemented", "this variable is unused". Nobody
reads the tenth wrong comment.

CodeReviewAgent is built around the opposite assumptions:

- **It reads the code.** Every reviewer works over a checkout of the merge request's
  source branch, pinned to the exact commit being reviewed, with `read_file`, `grep`
  and `glob`. "Is this defined anywhere?" becomes a question it can answer.
- **Silence is a valid answer.** A reviewer that finds nothing says so. Findings are
  gated, ranked, de-duplicated and capped before anything reaches the merge request.
- **Your conventions, not ours.** Which files are reviewed, by whom, and under which
  house rules is a YAML profile, plus optional instruction files that live in your repository.

## What it posts

- **Inline findings** on the exact diff line, headed with a severity badge
  (🔴 high · 🟠 medium), with an **Apply suggestion** button when the fix is concrete code.
- **A review summary**, once per merge request. It orients the human reviewer (what changed,
  where to look) and does not repeat the findings.
- **Labels**: `ai-reviewed`, plus a `review effort::N/5` estimate of how hard the MR is for a human to review.

Re-running on a new push reviews **only the commits since the last review**, and never
re-posts a finding that is already on the merge request.

## How it works

```
 merge request ──► fetch diff (GitLab API) ──► shallow checkout of the source branch
                                                          │
                     review profile (YAML) ──► classify each changed file
                                                          │
                          ┌───────────────────────────────┼───────────────────────────────┐
                    service reviewer                sql reviewer                   test reviewer   …
                   (read · grep · glob)          (read · grep · glob)          (read · grep · glob)
                          └───────────────────────────────┼───────────────────────────────┘
                                                          ▼
      in-diff gate ─► rank & purge false positives ─► restore severities ─► de-duplicate ─► cap
                                                          │
                                         inline comments · summary · labels
```

1. **Classify.** Each changed file is matched against the profile's `file_types` rules
   (a deterministic match on file names, no LLM). A file matching no rule is skipped.
   Go files that embed SQL can additionally be routed to the SQL reviewer.
2. **Review.** Every `(file, reviewer)` pair becomes an independent agent with its own
   context, pulled from a work queue by a pool of workers. Reviewers are read-only: they
   can read and search the checkout, but writes are refused and there is no shell.
3. **Filter.** Findings that don't land on a line of the diff are dropped. A ranking agent
   purges compile-time claims (the code already builds), style nits and unsupported
   opinions, then selects findings up to a budget. Duplicates of anything already on the
   MR are removed.
4. **Post**, with retries, a fallback to a general note when a line can't be anchored, and a
   refusal to post any comment with no text in it.

Nine reviewers ship built in: `service_reviewer` (technical quality), `service_business_reviewer`
(does the code do what the ticket asked), `sql_reviewer`, `test_reviewer`, `typescript_reviewer`,
`html_reviewer`, `scss_reviewer`, `dataview_reviewer` and `pbook_reviewer` (message catalogues).

## Quickstart

You need a GitLab personal access token with `api` scope, and a key for any
OpenAI-compatible LLM endpoint ([OpenRouter](https://openrouter.ai) works out of the box).

```bash
git clone https://github.com/systemgroup/codereviewagent.git
cd codereviewagent
cp .env.example .env        # set OPENROUTER_API_KEY, DEFAULT_LLM_MODEL, GITLAB_URL, GITLAB_TOKEN

docker build -t codereviewagent reviewer
docker run --rm --env-file .env codereviewagent \
  python main.py --gitlab-mr "https://gitlab.example.com/group/project/-/merge_requests/42"
```

### Run it on every merge request (GitLab CI)

Add a job to the project you want reviewed:

```yaml
ai-review:
  stage: test
  image: ghcr.io/systemgroup/codereviewagent:latest   # or your own build
  variables:
    GIT_STRATEGY: none          # the reviewer makes its own checkout
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - cd /app && python main.py --gitlab-mr "$CI_MERGE_REQUEST_PROJECT_URL/-/merge_requests/$CI_MERGE_REQUEST_IID"
```

Put `OPENROUTER_API_KEY`, `DEFAULT_LLM_MODEL`, `GITLAB_URL` and `GITLAB_TOKEN` in
**Settings → CI/CD → Variables** (masked). The token needs `api` scope, which also covers
the read-only clone.

### Run the whole stack locally

```bash
docker compose up -d      # reviewer web UI on :5000, analytics collector on :8000, PostgreSQL
```

## Configuration

### Review profile: what gets reviewed, and how

The profile is a YAML file selected with `REVIEW_PROFILE` (a bundled name, or a path).
Copy [`reviewer/profiles/default.yml`](reviewer/profiles/default.yml) and adapt it:

```yaml
version: 1
exclude: ["vendor/*", "*.pb.go"]

file_types:                      # first match wins
  - name: sql
    match: ["*.sql", "migrations/*"]
    reviewers: [sql_reviewer]
  - name: test
    match: ["*_test.go"]
    reviewers: [test_reviewer]
  - name: service
    match: ["*.go"]
    reviewers: [service_reviewer, service_business_reviewer]

sql_routing:
  scan_file_types: [service]     # Go files with embedded SQL also get sql_reviewer

instructions:                    # your house rules, added to the reviewer prompts
  all: |
    This is a payments service. Money is always int64 cents, never float.
  sql_reviewer: |
    We do not use foreign keys. Primary keys are BIGINT identity columns.
```

The profile is validated at startup: an unknown reviewer or a malformed rule stops the
run with a clear error, rather than letting it silently skip files.

### Per-repository instructions (`.aid/`)

Teams can also keep review guidance next to their code. The reviewer reads, from the
branch under review:

- `.aid/<type>.md` at the repository root (for example `.aid/sql.md`), and
- `<module>/.aid/<type>.md`, for guidance specific to one module (it takes priority).

### How much it says: verbosity labels

Add a label to the merge request and re-run the job:

| Label | Posts | Budget |
|---|---|---|
| *(none)* | high severity, ranked | 10 |
| `review::-v` | high severity, everything that qualifies | 30 |
| `review::-vv` | high and medium, ranked | 30 |
| `review::-vvv` | high and medium, everything that qualifies | 30 |

Verbosity changes the budget, not the bar: false positives, style nits and duplicates
are filtered at every level.

### Environment

[`.env.example`](.env.example) documents every setting with its default. The ones you'll
most likely touch:

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY`, `DEFAULT_LLM_MODEL` | LLM credentials and model id |
| `OPENROUTER_BASE_URL` | Any OpenAI-compatible endpoint (default: OpenRouter) |
| `FALLBACK_LLM_MODEL` | Model to fail over to when the primary is rate-limited |
| `GITLAB_URL`, `GITLAB_TOKEN` | Your GitLab and an `api`-scoped token |
| `REVIEW_PROFILE` | Which review profile to use |
| `LLM_REASONING_EFFORT` | `none` … `max`, default `xhigh` |
| `FILE_BATCH_SIZE` | Reviews in flight at once (default 5) |
| `OVERALL_TIMEOUT_S` | Total time budget per run (default 1500 s) |
| `MCP_ATLASSIAN_ENABLED` | Pull the linked Jira ticket into every reviewer's context |
| `LANGFUSE_BASE_URL` | Trace every prompt and tool call in [Langfuse](https://langfuse.com) |

## Analytics and feedback

The optional **collector** (`collector/`, Flask + PostgreSQL) records one event per review
run, including files, findings, comments posted, errors and model used, and turns human feedback
into data:

- Each posted comment is seeded with `1️⃣`–`5️⃣` reactions. A GitLab webhook records
  every rating and every thread reply.
- Three **Grafana dashboards** (`dashboards/`) chart run health, comment quality by model,
  and team engagement.
- An optional weekly **Mattermost digest** recognises the people who rate and reply.

Enable it with `COLLECTOR_ENABLED=true`. See [`collector/README.md`](collector/README.md)
and [`dashboards/README.md`](dashboards/README.md).

## Development

```bash
python -m venv venv && source venv/bin/activate
pip install -r reviewer/requirements.txt -r tests/requirements.txt
pytest                      # ~300 tests, a few seconds, no network or LLM calls
```

The test suite poisons LLM credentials, so an accidental live call fails loudly.

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.
To report a security problem, follow [SECURITY.md](SECURITY.md) and don't open a public issue.

## License

Copyright 2026 SystemGroup. Licensed under the [Apache License, Version 2.0](LICENSE).
