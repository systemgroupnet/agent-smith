"""How a failed LLM call is classified, and where the run goes next.

This module has been wrong twice in production, both times in the same way: a
failure the retry layer *exists* for was classified as non-retryable, got one
attempt, and killed a file review that would have succeeded on the next try. A
Cloudflare **524** did it (no text any signature matched, and 524 fell outside an
enumerated `(408, 502, 503, 504)`), and then an **HTTP 200 carrying a body that
was not JSON** did it again. Both are cheap to assert and were expensive to find,
so they are asserted here.

The gateway tests use a scripted runnable rather than a provider, because what is
under test is the decision — retry here, switch there, give up — and not any
model's behaviour. They drive it with `asyncio.run` rather than
`@pytest.mark.asyncio`, which is the convention across this suite: pytest-asyncio
is not in `tests/requirements.txt`, so with `--strict-markers` the marker is a
collection *error* in CI while passing locally for anyone whose environment
happens to have the plugin. It did exactly that once.
"""

import asyncio
import json

import pytest

from core.llm.gateway import FALLBACK, PRIMARY, LLMGateway, StructuredOutputMissing
from core.utils.llm_retry import parse_llm_error


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def _with_response(exc, status):
    """Attach a response the way the OpenAI SDK does, so `_status_code` finds it."""
    exc.response = FakeResponse(status)
    return exc


def _json_decode_error():
    """The real thing: what the SDK raises on a 200 whose body is not JSON.

    Reproduced verbatim from a live run against the fallback endpoint —
    `JSONDecodeError: Expecting value: line 5 column 1 (char 22)`.
    """
    try:
        json.loads("\n\n\n\n")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("that should not have parsed")


# --- classification ----------------------------------------------------------


def test_an_unparseable_body_on_a_200_is_retryable():
    """The failure that killed all four files of a production MR.

    A 200 means the request was fine, so nothing about it can be *fixed* by the
    client — the body simply did not arrive intact. That is the definition of a
    transient failure, but `transient` only looked at 5xx / 429 / known text, and
    a `JSONDecodeError` matched none of them: one attempt, then the file died.
    """
    info = parse_llm_error(_with_response(_json_decode_error(), 200))

    assert info.is_retryable, "a 200 with an unreadable body must be retried"
    assert not info.is_hard_limit and not info.is_config_block


def test_an_unparseable_body_with_no_status_at_all_is_retryable():
    """Re-raised without a response object, it has no status to classify on, so the
    type name has to carry it — the same reason `httpx.ReadTimeout("")` does."""
    assert parse_llm_error(_json_decode_error()).is_retryable


def test_a_quota_denial_is_not_retryable_even_when_the_body_is_unreadable():
    """The widening must not reach the two failures that are deterministic."""
    info = parse_llm_error(_with_response(
        ValueError("Expecting value: insufficient_quota, credit balance exhausted"), 200))

    assert info.is_hard_limit
    assert not info.is_retryable, "a quota denial fails identically on every attempt"


def test_a_gateway_timeout_status_keeps_its_tighter_budget():
    """524 regression guard: transient, and timeout-class so it retries only once."""
    from core.utils.llm_retry import retry_attempt_budget

    info = parse_llm_error(_with_response(Exception("Error code: 524"), 524))
    assert info.is_retryable and info.is_timeout
    assert retry_attempt_budget(info) == 2


# --- where the run goes next -------------------------------------------------


def test_a_rejected_reasoning_field_retries_without_it(monkeypatch):
    """`LLM_REASONING_EFFORT` is configuration, and its acceptance could not be
    verified against every endpoint — OpenRouter documents `xhigh`, a vendor API may
    not know it. A 400 is not retryable, so without this one call shape being
    repairable, a config value nobody could test would kill every call in the run."""
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "vendor/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "xhigh")
    monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
    LLMGateway.reset()

    seen = []

    def build(model):
        sent = (getattr(model, "extra_body", None) or {}).get("reasoning")
        seen.append(sent)

        class Runnable:
            async def ainvoke(self, payload, config=None):
                if sent is not None:
                    raise _with_response(
                        Exception("Error code: 400 - unsupported value for "
                                  "'reasoning.effort': 'xhigh'"), 400)
                return "answered"

        return Runnable()

    result = asyncio.run(LLMGateway(build=build, label="review sql_reviewer").ainvoke("go"))

    assert result == "answered"
    assert seen == [{"effort": "xhigh"}, None], seen


def test_a_400_about_the_prompt_is_not_blamed_on_reasoning(monkeypatch):
    """The sniff must not turn an ordinary bad request into a silent retry."""
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "vendor/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
    LLMGateway.reset()
    calls = []

    def build(model):
        class Runnable:
            async def ainvoke(self, payload, config=None):
                calls.append(1)
                raise _with_response(Exception("Error code: 400 - messages: too long"), 400)
        return Runnable()

    with pytest.raises(Exception, match="too long"):
        asyncio.run(LLMGateway(build=build, label="review sql_reviewer").ainvoke("go"))
    assert len(calls) == 1, "a content 400 must not be retried at all"


def test_a_structured_call_with_no_tool_call_is_retried(monkeypatch):
    """`with_structured_output` returns **None** when the model answers without
    calling the schema tool — a failed call arriving as a successful invocation,
    which is how the dedup stage crashed in production. Measured as intermittent
    (1 in 3 on deepseek-v4-flash at effort=xhigh), so a retry is the right answer."""
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "vendor/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    monkeypatch.setenv("LLM_RETRY_WAIT_MIN_S", "0")
    monkeypatch.setenv("LLM_RETRY_WAIT_MAX_S", "0")
    monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
    LLMGateway.reset()
    attempts = []

    def build(model):
        class Runnable:
            async def ainvoke(self, payload, config=None):
                attempts.append(1)
                return None if len(attempts) == 1 else "structured!"
        return Runnable()

    gw = LLMGateway(build=build, label="comment reviewer", structured=True)
    assert asyncio.run(gw.ainvoke("go")) == "structured!"
    assert len(attempts) == 2, "the None should have been retried, not returned"


def test_a_none_from_a_plain_call_is_left_alone(monkeypatch):
    """Only *structured* calls promise an object; a loop call may legitimately
    return something falsy, and inventing a failure there would be worse."""
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "vendor/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
    LLMGateway.reset()

    def build(model):
        class Runnable:
            async def ainvoke(self, payload, config=None):
                return None
        return Runnable()

    assert asyncio.run(LLMGateway(build=build, label="loop").ainvoke("go")) is None


def test_a_persistently_missing_structured_output_reaches_the_caller(monkeypatch):
    """After the budget, it must arrive as an exception the caller can handle —
    every structured agent already degrades on one, and none recognises a None."""
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "vendor/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    monkeypatch.setenv("LLM_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("LLM_RETRY_WAIT_MIN_S", "0")
    monkeypatch.setenv("LLM_RETRY_WAIT_MAX_S", "0")
    monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
    LLMGateway.reset()

    def build(model):
        class Runnable:
            async def ainvoke(self, payload, config=None):
                return None
        return Runnable()

    gw = LLMGateway(build=build, label="comment reviewer", structured=True)
    with pytest.raises(StructuredOutputMissing):
        asyncio.run(gw.ainvoke("go"))


@pytest.fixture
def two_providers(monkeypatch):
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "primary/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    monkeypatch.setenv("FALLBACK_LLM_MODEL", "fallback/model")
    monkeypatch.setenv("LLM_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("LLM_RETRY_WAIT_MIN_S", "0")
    monkeypatch.setenv("LLM_RETRY_WAIT_MAX_S", "0")
    LLMGateway.reset()
    yield
    LLMGateway.reset()


class ScriptedRunnable:
    """Raises whatever the script says for the provider it was built against."""

    def __init__(self, provider_name, script, calls):
        self.provider_name = provider_name
        self.script = script
        self.calls = calls

    async def ainvoke(self, payload, config=None):
        self.calls.append(self.provider_name)
        outcome = self.script(self.provider_name, self.calls)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _gateway(script, calls):
    def build(model):
        # The model carries the provider's id, which is how the scripted runnable
        # knows which endpoint it is standing in for.
        name = FALLBACK if "fallback" in str(getattr(model, "model_name", "")) else PRIMARY
        return ScriptedRunnable(name, script, calls)

    return LLMGateway(build=build, label="review typescript_reviewer")


def test_a_broken_fallback_hands_the_run_back_to_the_primary(two_providers):
    """The whole point of the fix: a lost file becomes a reviewed file.

    Primary throttles → switch → the fallback answers 200-with-garbage for its
    whole budget → the run returns to the primary, which by then has a slot free.
    Before this, every file that reached the fallback in that state was lost, and
    the run posted nothing at all.
    """
    calls = []

    def script(provider, seen):
        if provider == PRIMARY:
            if len([c for c in seen if c == PRIMARY]) <= 2:
                return _with_response(Exception("Error code: 429 - rate limit reached"), 429)
            return "reviewed"
        return _with_response(_json_decode_error(), 200)

    result = asyncio.run(_gateway(script, calls).ainvoke("go"))

    assert result == "reviewed"
    assert FALLBACK in calls, "should have tried the fallback"
    assert calls[-1] == PRIMARY, "should have come back to the primary"
    assert LLMGateway.fallback_ever_used(), "a mixed run must be reported as mixed"
    assert not LLMGateway.fallback_active(), "the fallback should be blocked now"
    kinds = [t.kind for t in LLMGateway.transitions()]
    assert kinds == ["llm_fallback_activated", "llm_fallback_blocked"], kinds


def test_a_throttled_fallback_does_not_bounce_back(two_providers):
    """If the fallback is rate limited the primary is too — bouncing is churn.

    So this one is expected to *fail*, and to fail on the fallback rather than
    ping-ponging until the budget is gone.
    """
    calls = []
    throttle = lambda *_: _with_response(  # noqa: E731
        Exception("Error code: 429 - rate limit reached"), 429)

    with pytest.raises(Exception, match="429"):
        asyncio.run(_gateway(throttle, calls).ainvoke("go"))

    assert calls[-1] == FALLBACK
    assert LLMGateway.fallback_active(), "a throttle must not block the fallback"
    assert [t.kind for t in LLMGateway.transitions()] == ["llm_fallback_activated"]


# --- an empty completion is not an answer -------------------------------------


class _StubRequest:
    """Enough of a ModelRequest for the middleware: it only calls `override`."""

    def override(self, **_kwargs):
        return self


def test_an_empty_model_response_is_retried_not_returned(monkeypatch):
    """The failure behind "3 file(s) failed to review" with nothing in the log.

    A completion with no content and no tool calls is indistinguishable from a
    finished answer to the agent graph: it ends, having read and recorded nothing.
    Measured intermittent on deepseek-v4-flash at every effort level, so the right
    response is the attempt budget, not a shrug.
    """
    from langchain_core.messages import AIMessage

    from core.llm.gateway_middleware import ProviderPolicyMiddleware

    monkeypatch.setenv("DEFAULT_LLM_MODEL", "vendor/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    monkeypatch.setenv("LLM_RETRY_WAIT_MIN_S", "0")
    monkeypatch.setenv("LLM_RETRY_WAIT_MAX_S", "0")
    monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
    LLMGateway.reset()

    calls = []

    async def handler(_request):
        calls.append(1)
        if len(calls) == 1:
            return AIMessage(content="")          # what the relay actually sent
        return AIMessage(content="", tool_calls=[
            {"name": "read_file", "id": "1", "args": {"file_path": "svc/pay.go"}}])

    mw = ProviderPolicyMiddleware(label="review service_reviewer")
    result = asyncio.run(mw.awrap_model_call(_StubRequest(), handler))

    assert len(calls) == 2, "the empty completion should have been retried"
    assert result.tool_calls, "the retry's tool call must survive"


def test_a_message_with_content_or_a_tool_call_is_left_alone():
    """Never invent a failure: content alone, or a tool call alone, is a real answer."""
    from langchain_core.messages import AIMessage

    from core.llm.gateway_middleware import _carries_nothing

    assert _carries_nothing(AIMessage(content="")) is True
    assert _carries_nothing(AIMessage(content="I found a problem")) is False
    assert _carries_nothing(AIMessage(content="", tool_calls=[
        {"name": "grep", "id": "1", "args": {"pattern": "x"}}])) is False


def test_the_attempt_budget_follows_the_cost_of_an_attempt():
    """Two caps in opposite directions, both set by what one attempt costs.

    A timeout burns a full request window, so three of them would starve the run. An
    empty completion burns no output tokens and returns in seconds, and giving up on
    it loses the file — luna produced three in a row on one reviewer and that file
    failed with the default budget of 3.
    """
    from core.llm.gateway import EmptyModelResponse
    from core.utils.llm_retry import retry_attempt_budget

    timeout = parse_llm_error(_with_response(Exception("Error code: 524"), 524))
    empty = parse_llm_error(EmptyModelResponse("no content and no tool calls"))
    plain = parse_llm_error(_with_response(Exception("Error code: 503"), 503))

    assert retry_attempt_budget(timeout) == 2
    assert retry_attempt_budget(plain) == 3
    assert retry_attempt_budget(empty) == 6, "the cheapest failure deserves the most tries"
