# tests

The repo's test suite. Runs in CI **before** either image is built, so a broken
reviewer can't be packaged.

```bash
pip install -r reviewer/requirements.txt -r tests/requirements.txt
pytest tests/
```

## Why these tests exist at the root

Each component's Docker build context is its own folder, so `reviewer/tests/`
would ship pytest and fixtures into the production image. A root-level suite also
covers both components without either importing the other. `conftest.py` puts
`reviewer/` on `sys.path`, because the reviewer's modules import each other by
top-level name (`from core.utils…`) — that is how `main.py` runs.

## What is covered

| file | what it protects |
|---|---|
| `test_repo_clone.py` | the three-rung shallow-clone ladder, against a **real** local git repo: `--shallow-exclude` → `--depth=1` → the MR head ref; pinning to the reviewed SHA when the branch tip has moved; credentials never reaching a log; no leaked temp directories |
| `test_findings_sink.py` | the only channel a finding travels on — validation, lenient coercion of what models actually emit (`line: "n/a"`, `severity: "CRITICAL"`), per-file caps, and the coverage ledger that distinguishes "reviewed and clean" from "never ran" |
| `test_review_pass.py` | the review loop: reviewers are deep agents with the harness, writes are **refused at the backend** and `execute` is unavailable, a reviewer that answers in prose without recording is a failed file, an expired budget truncates without blaming a file, a tool that *raises* becomes an error observation instead of ending the review (and `record_findings` failing says so, rather than letting the model believe it reported), and the per-tool-call trace is labelled per reviewer-and-file and can be switched off |
| `test_llm_config.py` | what we ask the model to *do*: the whole seven-rung effort ladder is accepted (an earlier cut collapsed the top two onto `high`), only *spellings* are aliased, an empty value means the `xhigh` default rather than off, and the DeepSeek forced-`tool_choice` override outranks any configured level — the one ordering here whose failure mode is a 400 |
| `test_llm_errors.py` | the classifications this module has been wrong about in production — a gateway 524 and a **200 carrying an unreadable body** are both transient, a quota denial never is — plus where the run goes next: a broken fallback hands back to the primary and a *throttled* one does not, a rejected `reasoning` field retries without it while an ordinary content 400 still fails immediately, and a structured call that returns **None** (the model skipping the schema tool) is retried rather than handed to a caller that cannot recognise it |
| `test_sql_routing.py` | the one routing judgement — deterministic SQL evidence gathering, hallucinated paths discarded, fails closed, and additions that never displace the static assignment |
| `test_comment_dedup.py` | duplicate suppression: normalisation across posted-vs-candidate markup, the exact-match pre-pass, cycle resolution that can never empty a group, and fail-open on every malformed response |
| `test_comment_body.py` | that a posted comment always says something — the production symptom was a body consisting of nothing but `**🔴 High severity issue**`, because `ReviewIssue.problem` defaults to `""` and the ranking stage rewrites the text. Pins the recovery (positional match back to the reviewers' finding, most-severe text on a merged position) and its one asymmetry against the severity restore: an unmatchable severity is guessed because a badge has to be printed, an unmatchable description is dropped because inventing one would be a fabrication. Then the two outer layers: the rule is present exactly once in all nine reviewer prompts **with its escape hatch** (an edit that keeps "never record an empty finding" and drops "an empty list is a correct answer" turns the reviewers into inventors of findings), and the posting loop refuses a rendered body that carries nothing but the badge while still allowing one that carries only a fix. Also holds the two boundaries that were already guarded, since the recovery has nothing to recover from if the sink stops rejecting a blank finding |
| `test_severity.py` | one shared severity rubric in all eleven reviewer prompts (no per-prompt drift), and the demotion channel's three asymmetries |
| `test_engagement.py` | the collector's engagement layer — the badge ladder's thresholds and no-double-award rule, level boundaries, ISO-week arithmetic (including that 1 Jan 2027 is `2026-W53`, which is why the bucket key carries the ISO year), the digest's rendering, and that its error paths never print the Mattermost token. Also the in-service scheduler's due-week decision, whose nastiest case is a week boundary read in the wrong clock — 03:00 Monday in Tehran is still Sunday in UTC, so a UTC-derived boundary reports the wrong week once a week and looks like a fluke. Above all the design rule the feature rests on: **no metric and no points weight may read the score a rater gave** — a future "gave ten 5-stars" badge fails the suite, which is the point |

`test_engagement.py` needs `collector/` on `sys.path`, which `conftest.py` adds
alongside `reviewer/` (the collector's modules also import each other by top-level
name, because gunicorn runs with `collector/` as its working directory). Only
modules that touch no database at import time are testable that way — `engagement`
and `digest` are pure; `main` runs Alembic on import and is deliberately never
imported here.

## What is not covered

**No test here makes a real LLM call**, by design — `conftest.py` poisons the
provider credentials so an accidental one fails loudly instead of passing quietly.
That means prompt *wording* is asserted but model *behaviour* is not: a rubric can
be present and still not calibrate. Those questions need a live run.

Nothing here talks to GitLab either. The end-to-end harness that does
(clone → review → gate → dedup → would-post, with every write intercepted) needs a
token and a real merge request, so it is not part of this suite.

**Nothing here talks to PostgreSQL**, which is the notable gap in
`test_engagement.py`: it pins down the logic *around* the SQL and the design rules
the SQL must obey, but not whether a query returns the right rows. Two scripts at
the repo root cover that against a throwaway database, and they are how the
engagement SQL was actually verified — run them by hand when changing it, the same
way the dashboards' panel queries are checked (`dashboards/README.md` →
*Validating changes*):

```bash
docker run -d --name crg-verify-pg -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=collector -p 55433:5432 postgres:18

# drives the real webhook ingest paths: canonical-score dedupe, points,
# revoke semantics, badge awards, ISO-week awards, idempotency
python scripts/verify_engagement.py

# migrates 0010 → ten weeks of history → head, and checks the backfill dates
# each badge to the event that earned it rather than to today
python scripts/verify_engagement_backfill.py

# the in-service digest scheduler: the claim serialises under 8 racing threads,
# a restart does not re-announce, a failed week keeps its claim. Posts real
# messages when given credentials — use a scratch channel.
MATTERMOST_URL=… MATTERMOST_TOKEN=… MATTERMOST_CHANNEL_ID=… \
  python scripts/verify_digest_scheduler.py

docker rm -f crg-verify-pg
```

All three are **destructive** — they write into (and the backfill one drops the
schema of) whatever `COLLECTOR_DATABASE_URL` points at, defaulting to that
throwaway container, and the scheduler one clears `digest_runs` and sends real
Mattermost messages. Never aim them at the real collector or the team's channel.
