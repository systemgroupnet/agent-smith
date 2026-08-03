"""
Provider configuration and chat-model construction — one OpenAI-compatible client.

This module is deliberately **stateless**: it reads the environment and builds a
``ChatOpenAI``, and that is all. It answers "what providers are configured, and
how do I build a client for one of them?" It does *not* decide which provider is
in force, retry anything, or fail over — that is
:class:`core.llm.gateway.LLMGateway`, the single object every LLM call goes
through. Keeping the two apart is what lets the gateway own all the mutable
state (one latch, in one place) while this stays a pure builder that cannot
disagree with itself.

Every chat model is a ``ChatOpenAI`` pointed at an OpenAI-compatible base URL.
That one client covers OpenRouter, OpenRouter behind an internal reverse proxy,
and a vendor's own API (e.g. an internal GLM/Zhipu ``/v4`` endpoint), because
they all speak ``POST {base}/chat/completions``.

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


# ── The primary provider ─────────────────────────────────────────────────────
# All read at call time, so a change after import is respected (tests, web mode).


def get_default_model() -> str:
    """The primary model id, as the configured endpoint names it — ``provider/model``
    on OpenRouter, native (``glm-5.2``) on a vendor API."""
    return os.getenv("DEFAULT_LLM_MODEL", "deepseek/deepseek-v4-pro")


def primary_api_key() -> str:
    """Key for the primary endpoint."""
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def primary_base_url() -> str:
    """Primary endpoint, or "" for api.openrouter.ai."""
    return os.getenv("OPENROUTER_BASE_URL", "").strip()


# ── The fallback provider ────────────────────────────────────────────────────
# A second endpoint the run switches to when the primary keeps answering with a
# rate/concurrency limit. Only the *configuration* lives here; whether the switch
# has happened is LLMGateway's business.


def get_fallback_model() -> str:
    """Fallback model id, or "" when no fallback is configured."""
    return os.getenv("FALLBACK_LLM_MODEL", "").strip()


def fallback_api_key() -> str:
    """Fallback key. Defaults to the primary key, so a fallback that is another
    model on the *same* account needs only FALLBACK_LLM_MODEL."""
    return os.getenv("FALLBACK_LLM_API_KEY", "").strip() or primary_api_key()


def fallback_base_url() -> str:
    """Fallback endpoint. Defaults to the primary endpoint, for the same reason."""
    return os.getenv("FALLBACK_LLM_BASE_URL", "").strip() or primary_base_url()


def fallback_configured() -> bool:
    """True when a fallback provider is usable. FALLBACK_LLM_MODEL is the switch —
    the key and base URL each fall back to the primary's when unset, so pointing at
    a different model on the same endpoint takes one variable, and a genuinely
    separate provider takes all three."""
    return bool(get_fallback_model() and fallback_api_key())


# Legacy model name mapping for backward compatibility with old agent code.
MODEL_NAME_MAPPING = {
    "gpt-5-nano": "openai/gpt-4o-mini",  # gpt-5-nano doesn't exist, map to gpt-4o-mini
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "o1-mini": "openai/o1-mini",
    "o1-preview": "openai/o1-preview",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
}


def _openrouter_provider() -> Optional[dict]:
    """
    Optional OpenRouter provider-routing config, read from OPENROUTER_PROVIDER.

    Set OPENROUTER_PROVIDER to a JSON object to pin/sort providers or enforce a
    data policy — e.g. '{"order": ["Anthropic"], "allow_fallbacks": true}' or
    '{"data_collection": "allow"}'. This is the lever for "No endpoints matching
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


def build_chat_model(
    *,
    model: str,
    api_key: str,
    base_url: str = "",
    temperature: float = 0.0,
    streaming: bool = False,
    max_tokens: Optional[int] = None,
    reasoning: Optional[dict] = None,
) -> BaseChatModel:
    """
    Build a ``ChatOpenAI`` for one explicit endpoint.

    Every argument is passed in rather than read from the environment here: the
    caller (LLMGateway) has already decided which provider is in force, and a
    builder that re-derived it could disagree with that decision.

    Args:
        model: Model id as the endpoint names it.
        api_key: Key for that endpoint.
        base_url: Endpoint, or "" for api.openrouter.ai.
        temperature: Sampling temperature (0.0 = deterministic).
        streaming: Enable streaming for ``invoke``. (``astream`` streams anyway.)
        max_tokens: Maximum tokens for the response (optional).
        reasoning: Optional reasoning config, sent as the top-level ``reasoning``
                   request field via ``extra_body`` (e.g. {"effort": "none"} to
                   turn thinking off). Honored by OpenRouter; ignored by
                   endpoints that don't know the field.
    """
    if not api_key:
        raise ValueError(
            "No LLM API key found in environment variables. "
            "Please set OPENROUTER_API_KEY (and FALLBACK_LLM_API_KEY if the "
            "fallback provider uses a different account) in your .env file."
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
    provider = _openrouter_provider()
    if provider:
        extra_body["provider"] = provider
    if reasoning is not None:
        extra_body["reasoning"] = reasoning
    if extra_body:
        kwargs["extra_body"] = extra_body

    return ChatOpenAI(**kwargs)


def structured_reasoning_for(model: str) -> Optional[dict]:
    """
    Reasoning override for structured-output calls, decided per model.

    ``with_structured_output(method="function_calling")`` sends a forced
    ``tool_choice`` (a named function). DeepSeek V4-class models reject that
    while thinking mode is on ("Thinking mode does not support this
    tool_choice" — historically surfaced as "Function call should not be used
    with prefix"), so for those models structured calls must run with
    reasoning disabled. All other models keep their reasoning behavior.

    The argument must be the model *actually being sent*, not the one an agent
    asked for: once the gateway has switched providers those differ, and falling
    back from GLM (fine with a forced tool_choice while thinking) to a DeepSeek V4
    model (which rejects it) has to disable reasoning even though the primary id
    never needed it.

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
