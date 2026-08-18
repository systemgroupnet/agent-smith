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
model's behaviour.
"""

import json

import pytest

from core.llm.gateway import FALLBACK, PRIMARY, LLMGateway
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
    """The failure that killed all four files of a production project MR 1180.

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


@pytest.mark.asyncio
async def test_a_broken_fallback_hands_the_run_back_to_the_primary(two_providers):
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

    result = await _gateway(script, calls).ainvoke("go")

    assert result == "reviewed"
    assert FALLBACK in calls, "should have tried the fallback"
    assert calls[-1] == PRIMARY, "should have come back to the primary"
    assert LLMGateway.fallback_ever_used(), "a mixed run must be reported as mixed"
    assert not LLMGateway.fallback_active(), "the fallback should be blocked now"
    kinds = [t.kind for t in LLMGateway.transitions()]
    assert kinds == ["llm_fallback_activated", "llm_fallback_blocked"], kinds


@pytest.mark.asyncio
async def test_a_throttled_fallback_does_not_bounce_back(two_providers):
    """If the fallback is rate limited the primary is too — bouncing is churn.

    So this one is expected to *fail*, and to fail on the fallback rather than
    ping-ponging until the budget is gone.
    """
    calls = []
    throttle = lambda *_: _with_response(  # noqa: E731
        Exception("Error code: 429 - rate limit reached"), 429)

    with pytest.raises(Exception, match="429"):
        await _gateway(throttle, calls).ainvoke("go")

    assert calls[-1] == FALLBACK
    assert LLMGateway.fallback_active(), "a throttle must not block the fallback"
    assert [t.kind for t in LLMGateway.transitions()] == ["llm_fallback_activated"]
