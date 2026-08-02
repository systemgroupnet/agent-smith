"""
LangChain Configuration Module — one OpenAI-compatible client for every endpoint

Every chat model is a ``ChatOpenAI`` pointed at ``OPENROUTER_BASE_URL``. That one
client covers OpenRouter, OpenRouter behind an internal reverse proxy, and a
vendor's own OpenAI-compatible API (e.g. an internal GLM/Zhipu ``/v4`` endpoint),
because they all speak ``POST {base}/chat/completions``.

This replaced ``langchain-openrouter``'s ``ChatOpenRouter``, which could only talk
to OpenRouter: it validates *responses* against OpenRouter's schema, so a GLM
endpoint — which omits ``system_fingerprint`` — failed every single call with
``ResponseValidationError``, on requests that were perfectly valid. The two
OpenRouter-specific request fields it existed to provide, ``reasoning`` and
``provider`` routing, are plain JSON and ride along in ``extra_body``; a vendor
endpoint that doesn't know them ignores them (verified against GLM 5.2).

A side benefit: ``ChatOpenAI`` implements ``_astream``, which ``ChatOpenRouter``
did not. LangChain's ``astream`` — what the agent loop calls — no longer degrades
to a single blocking ``ainvoke``, so a long completion arrives incrementally
instead of having to land inside one ``OPENROUTER_TIMEOUT_MS`` read window. That
window expiring is what produced the `httpx.ReadTimeout` failures in CI.
"""

import json
import os
from typing import Any, Optional
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI


def int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to ``default`` on a missing/malformed value.

    A garbled numeric env var (e.g. OPENROUTER_TIMEOUT_MS="5min") must not crash
    the process — especially for the resilience timeouts, where the crash would
    defeat the very feature the timeout provides.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print(f"  ⚠️  [config] Ignoring invalid {name}={raw!r}; using default {default}")
        return default


def get_default_model() -> str:
    """
    Return the default LLM model id from the environment (OpenRouter format).

    Read at call time so changes to DEFAULT_LLM_MODEL after import are respected.
    """
    return os.getenv("DEFAULT_LLM_MODEL", "deepseek/deepseek-v4-pro")


# ── Fallback provider ────────────────────────────────────────────────────────
# A second endpoint the whole run switches to when the primary keeps answering
# with a rate/concurrency limit (see core/utils/llm_retry.py). The switch is a
# process-wide, sticky latch rather than a per-call choice: several file reviews
# are in flight at once and they hit the limit together, so once one of them has
# established that the primary is throttling, the others should not each spend a
# full retry budget rediscovering it. It is never switched back — a provider that
# just shed load under our concurrency will still be shedding it this run.
_fallback_active = False


def get_fallback_model() -> str:
    """Fallback model id, or "" when no fallback is configured."""
    return os.getenv("FALLBACK_LLM_MODEL", "").strip()


def _fallback_api_key() -> str:
    """Fallback key. Defaults to the primary key, so a fallback that is another
    model on the *same* account needs only FALLBACK_LLM_MODEL."""
    return os.getenv("FALLBACK_LLM_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip()


def _fallback_base_url() -> str:
    """Fallback endpoint. Defaults to the primary endpoint, for the same reason."""
    return os.getenv("FALLBACK_LLM_BASE_URL", "").strip() or os.getenv("OPENROUTER_BASE_URL", "").strip()


def fallback_configured() -> bool:
    """True when a fallback provider is usable. FALLBACK_LLM_MODEL is the switch —
    the key and base URL each fall back to the primary's when unset, so pointing at
    a different model on the same endpoint takes one variable, and a genuinely
    separate provider takes all three."""
    return bool(get_fallback_model() and _fallback_api_key())


def is_fallback_active() -> bool:
    """True once the run has switched to the fallback provider."""
    return _fallback_active


def activate_fallback_provider() -> bool:
    """Latch the fallback on for the rest of the process.

    Returns True if this call flipped it, False if it was already on — the caller
    uses that only to decide how to phrase its log line, since concurrent agents
    all arrive here after the same throttle.
    """
    global _fallback_active
    if _fallback_active:
        return False
    _fallback_active = True
    return True


def reset_fallback_provider() -> None:
    """Clear the latch. For tests and for web mode, where one process serves many
    reviews and a throttle during one MR should not permanently demote the next."""
    global _fallback_active
    _fallback_active = False


def describe_fallback_provider() -> str:
    """Human-readable fallback target for log lines (never includes the key)."""
    base = _fallback_base_url()
    return f"{get_fallback_model()} @ {base}" if base else f"{get_fallback_model()} @ api.openrouter.ai"


def resolve_effective_model(model_name: Optional[str] = None) -> str:
    """The model id actually sent — the fallback's once the fallback is active.

    Every caller's requested model is overridden, because the latch means "the
    primary endpoint is throttling us", which is true of every model on it.
    """
    if _fallback_active and fallback_configured():
        return get_fallback_model()
    return model_name or get_default_model()


def _resolve_endpoint(model_name: Optional[str]) -> tuple[str, str, str]:
    """``(model, api_key, base_url)`` for the provider currently in force."""
    if _fallback_active and fallback_configured():
        return get_fallback_model(), _fallback_api_key(), _fallback_base_url()
    return (
        model_name or get_default_model(),
        os.getenv("OPENROUTER_API_KEY", "").strip(),
        os.getenv("OPENROUTER_BASE_URL", "").strip(),
    )


def _get_openrouter_provider() -> Optional[dict]:
    """
    Optional OpenRouter provider-routing config, read from OPENROUTER_PROVIDER.

    Set OPENROUTER_PROVIDER to a JSON object to pin/sort providers or enforce a
    data policy — e.g. '{"order": ["Anthropic"], "allow_fallbacks": true}' or
    '{"data_collection": "deny"}'. This is the lever for "No endpoints matching
    your data policy" guardrail errors. Returns None when unset or invalid.
    """
    raw = os.getenv("OPENROUTER_PROVIDER", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        print(f"  ⚠️  [langchain_config] Ignoring invalid OPENROUTER_PROVIDER (not valid JSON): {raw}")
        return None


def get_llm_model(
    model_name: Optional[str] = None,
    temperature: float = 0.0,
    streaming: bool = False,
    max_tokens: Optional[int] = None,
    reasoning: Optional[dict] = None,
) -> BaseChatModel:
    """
    Get a ChatOpenAI model pointed at the configured endpoint.

    Args:
        model_name: Model id as the endpoint names it — "provider/model" on
                    OpenRouter, native (e.g. "glm-5.2") on a vendor API. Falls
                    back to DEFAULT_LLM_MODEL when None.
        temperature: Sampling temperature (0.0 = deterministic).
        streaming: Enable streaming for ``invoke``. (``astream`` streams anyway.)
        max_tokens: Maximum tokens for the response (optional).
        reasoning: Optional reasoning config, sent as the top-level ``reasoning``
                   request field via ``extra_body`` (e.g. {"effort": "none"} to
                   turn thinking off). Honored by OpenRouter; ignored by
                   endpoints that don't know the field.

    Returns:
        Configured ChatOpenAI (a BaseChatModel).

    Environment Variables:
        - OPENROUTER_API_KEY: required (the key for whichever endpoint is set).
        - DEFAULT_LLM_MODEL: fallback model when model_name is not provided.
        - OPENROUTER_BASE_URL: optional custom endpoint (reverse proxy, vendor API).
        - OPENROUTER_PROVIDER: optional JSON provider-routing config (OpenRouter).
        - OPENROUTER_TIMEOUT_MS: per-request timeout in milliseconds.
    """
    # Which provider is in force: the primary, or the fallback once the rate-limit
    # latch has been flipped (see activate_fallback_provider).
    model, api_key, base_url = _resolve_endpoint(model_name)
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY not found in environment variables. "
            "Please set OPENROUTER_API_KEY in your .env file."
        )

    # "provider/model" is *OpenRouter's* id convention, so only assume a missing
    # provider means OpenAI when we are talking to OpenRouter directly. A custom
    # base_url may be a vendor's own API, where ids are native and unprefixed —
    # pointing at an internal GLM endpoint with DEFAULT_LLM_MODEL=glm-5.2 used to
    # send "openai/glm-5.2" and get back "BadRequestResponseError: Unknown Model,
    # please check the model code". Existing OpenRouter-behind-a-proxy setups are
    # unaffected: their ids already carry a provider (deepseek/deepseek-v4-pro).
    if "/" not in model and not base_url:
        model = f"openai/{model}"

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "streaming": streaming,
        # Bound every request so a proxy that swallows a request without ever
        # answering (observed with the internal reverse proxy) surfaces as a
        # timeout + SDK retry instead of hanging the review forever. ChatOpenAI
        # takes seconds, so convert — passing the raw ms would be an ~83-hour
        # timeout, i.e. no bound at all.
        "timeout": int_env("OPENROUTER_TIMEOUT_MS", 300000) / 1000.0,
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    if base_url:
        kwargs["base_url"] = base_url

    # OpenRouter's two non-OpenAI request fields are plain JSON, so they ride in
    # extra_body rather than needing a bespoke client. An endpoint that doesn't
    # know them ignores them (checked against GLM: a request carrying
    # reasoning={"effort":"none"} is answered normally).
    extra_body: dict[str, Any] = {}
    provider = _get_openrouter_provider()
    if provider:
        extra_body["provider"] = provider
    if reasoning is not None:
        extra_body["reasoning"] = reasoning
    if extra_body:
        kwargs["extra_body"] = extra_body

    return ChatOpenAI(**kwargs)


def _structured_reasoning_for(model: str) -> Optional[dict]:
    """
    Reasoning override for structured-output calls, decided per model.

    ``with_structured_output(method="function_calling")`` sends a forced
    ``tool_choice`` (a named function). DeepSeek V4-class models reject that
    while thinking mode is on ("Thinking mode does not support this
    tool_choice" — historically surfaced as "Function call should not be used
    with prefix"), so for those models structured calls must run with
    reasoning disabled. All other models keep their reasoning behavior.

    The model list is comma-separated id prefixes in
    STRUCTURED_NO_REASONING_MODELS (default "deepseek/").

    Uses {"effort": "none"} rather than {"enabled": false}. Both now reach the
    wire intact (the dict goes out verbatim in ``extra_body``), but "effort" is
    the form proven against this endpoint — the older client silently dropped an
    unknown "enabled" key and sent an empty {}, so that variant was never
    actually exercised.
    """
    raw = os.getenv("STRUCTURED_NO_REASONING_MODELS", "deepseek/")
    prefixes = [p.strip() for p in raw.split(",") if p.strip()]
    if any(model.startswith(p) for p in prefixes):
        return {"effort": "none"}
    return None


def get_reasoning_model(effort: str = "medium", temperature: float = 0.0) -> BaseChatModel:
    """Get a reasoning model (o1 series) configured for OpenRouter."""
    effort_to_model = {
        "low": "openai/o1-mini",
        "medium": "openai/o1-mini",
        "high": "openai/o1-preview",
    }
    return get_llm_model(effort_to_model.get(effort, "openai/o1-mini"), temperature=temperature)


def get_fast_model(temperature: float = 0.0) -> BaseChatModel:
    """Get a fast, cost-effective model for simple tasks (FAST_LLM_MODEL)."""
    return get_llm_model(os.getenv("FAST_LLM_MODEL", "openai/gpt-4o-mini"), temperature=temperature)


def get_smart_model(temperature: float = 0.0) -> BaseChatModel:
    """Get a powerful model for complex reasoning tasks (SMART_LLM_MODEL)."""
    return get_llm_model(os.getenv("SMART_LLM_MODEL", "openai/gpt-4o"), temperature=temperature)


# Legacy model name mapping for backward compatibility with old agent code.
MODEL_NAME_MAPPING = {
    "gpt-5-nano": "openai/gpt-4o-mini",  # gpt-5-nano doesn't exist, map to gpt-4o-mini
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "o1-mini": "openai/o1-mini",
    "o1-preview": "openai/o1-preview",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
}


def get_model_for_agent(
    agent_model_name: str,
    temperature: float = 0.0,
    streaming: bool = False,
    max_tokens: Optional[int] = None,
    reasoning: Optional[dict] = None,
) -> BaseChatModel:
    """
    Get a model configured for a specific agent, mapping legacy model names to
    OpenRouter ids.
    """
    actual_model = MODEL_NAME_MAPPING.get(agent_model_name, agent_model_name)
    return get_llm_model(
        actual_model,
        temperature=temperature,
        streaming=streaming,
        max_tokens=max_tokens,
        reasoning=reasoning,
    )


def get_structured_model_for_agent(
    agent_model_name: str,
    temperature: float = 0.0,
) -> BaseChatModel:
    """
    Model instance for ``with_structured_output(method="function_calling")``
    calls. Same as get_model_for_agent, except reasoning is disabled for
    models whose thinking mode rejects a forced tool_choice (see
    _structured_reasoning_for). Agent-loop calls (tool_choice auto) should
    keep using get_model_for_agent so reasoning stays on.
    """
    actual_model = MODEL_NAME_MAPPING.get(agent_model_name, agent_model_name)
    # Decide the reasoning override against the model that will actually be sent.
    # Once the fallback latch is on that is the *fallback* id, and the two can
    # disagree: falling back from GLM (fine with a forced tool_choice while
    # thinking) to a DeepSeek V4 model (which rejects it) must disable reasoning,
    # even though the primary id never needed it.
    return get_llm_model(
        actual_model,
        temperature=temperature,
        streaming=False,
        reasoning=_structured_reasoning_for(resolve_effective_model(actual_model)),
    )
