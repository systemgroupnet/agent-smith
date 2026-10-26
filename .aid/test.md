# House rules: tests

- The suite is pytest at the repository root (`tests/`), not godog or Gherkin.
- `tests/conftest.py` poisons the LLM provider credentials and resets
  `LLMGateway` around every test. A test must never make a live LLM or GitLab
  call: models and HTTP are stubbed. A test that needs the network is a defect.
- Prompt *wording* is asserted on purpose ("the rule appears exactly once in all
  11 prompts"); model *behaviour* is not tested. Do not ask for behavioural
  assertions on a prompt.
- Tests that need PostgreSQL are destructive and live in `scripts/verify_*.py`,
  outside CI. Do not ask for them to move into `tests/`.
- `tests/internal/` holds tests that name internal projects; it is never
  published.
