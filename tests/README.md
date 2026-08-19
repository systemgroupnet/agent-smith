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
| `test_severity.py` | one shared severity rubric in all nine reviewer prompts (no per-prompt drift), and the demotion channel's three asymmetries |

## What is not covered

**No test here makes a real LLM call**, by design — `conftest.py` poisons the
provider credentials so an accidental one fails loudly instead of passing quietly.
That means prompt *wording* is asserted but model *behaviour* is not: a rubric can
be present and still not calibrate. Those questions need a live run.

Nothing here talks to GitLab either. The end-to-end harness that does
(clone → review → gate → dedup → would-post, with every write intercepted) needs a
token and a real merge request, so it is not part of this suite.
