# Contributing to CodeReviewAgent

Thanks for helping. This project reviews other people's code, so we hold our own
to the same bar: small, well-explained changes with tests.

## Getting set up

```bash
python -m venv venv && source venv/bin/activate
pip install -r reviewer/requirements.txt -r tests/requirements.txt
pytest
```

Tests need `git` on your `PATH` (the clone tests drive real git over `file://`). They
make no network or LLM calls: provider credentials are poisoned in `tests/conftest.py`,
so an accidental live call fails loudly instead of billing someone.

## Making a change

1. **Open an issue first** for anything larger than a bug fix, so we can agree on the
   approach before you write it.
2. **Keep the change focused.** One concern per pull request.
3. **Add a test.** A bug fix comes with a test that fails without it. Prompt *wording*
   can be asserted; model *behaviour* cannot, so don't write tests that need a live model.
4. **Say why in the commit message**, not just what. We use
   [Conventional Commits](https://www.conventionalcommits.org) (`fix(review): …`, `feat(collector): …`).
5. **Never commit credentials**, internal hostnames or real merge-request content.
   `.env` is ignored. Use `.env.example` for new settings, with the default documented.

## Common contributions

- **Support a new file layout.** Usually no code at all: write a review profile
  (`reviewer/profiles/`) and, if it's generally useful, send it as an example.
- **Add a reviewer.** Add a prompt in `reviewer/core/prompts/` built from the shared
  rules in `common.py`, register a `ReviewerSpec` in `reviewer/core/agents/deep_reviewer.py`,
  and route a file type to it in a profile.
- **A new LLM provider.** Anything OpenAI-compatible already works through
  `DEFAULT_LLM_BASE_URL`. Provider-specific behaviour belongs in `reviewer/core/llm/`.

## Code style

Match the code around you. Comments explain *why* a decision was made, especially one
that came from a real failure. A reader should never have to rediscover it.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the same license as the project.
