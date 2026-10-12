# House rules: service code (reviewer/ and collector/)

Intended here — do not report these as defects:

- Comments are long on purpose: they record the production incident or
  measurement behind a decision. Never ask to shorten or remove one.
- Steps around posting are non-fatal by design. A failure in analytics, labels,
  reactions, the review summary or dedup is recorded (`record_warning`, or
  `record_error` on the review path) and the run continues. Catching a broad
  exception there is intended; catching one *without recording it* is the defect.
- Each stage's fail-open or fail-closed choice is deliberate and stated in its
  docstring. Report code that contradicts its own docstring, not the choice.

Defects worth a comment:

- An import between `reviewer/` and `collector/`. They are separate services with
  separate Docker build contexts; the contract between them is the JSON analytics
  event and nothing else.
- A chat model built, or an LLM request retried, anywhere except
  `reviewer/core/llm/gateway.py` (`LLMGateway`). A second way to reach a model
  bypasses retry, failover and provider policy.
- Attribute access on a review issue in `reviewer/main.py` (`issue.severity`).
  Issues there are dicts (`model_dump`); read them with the dict-or-object helpers
  (`get_severity`, `_issue_field`). This has crashed production twice.
- A new env flag documented as default-on that reads an empty value as off:
  `.env.example` ships every setting empty.
- A change to a prompt in `reviewer/core/prompts/` with no matching change in
  `tests/`: prompt wording is product behaviour and is asserted.
- An internal hostname, project name or credential in a file that is not listed in
  `.oss/exclude` — those files are published.
