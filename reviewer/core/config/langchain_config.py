"""
LangChain Configuration Module for OpenRouter Integration

Builds chat models via the official ``langchain-openrouter`` package
(``ChatOpenRouter``), which is purpose-built for OpenRouter's schema / reasoning /
provider-routing contract — unlike ``ChatOpenAI`` pointed at OpenRouter's base_url,
which mishandles reasoning models and structured output through OpenRouter.
"""

import json
import os
from typing import Any, Optional
from langchain_core.language_models import BaseChatModel
from langchain_openrouter import ChatOpenRouter


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
    Get a ChatOpenRouter model configured for OpenRouter.

    Args:
        model_name: OpenRouter model id ("provider/model"). Falls back to
                    DEFAULT_LLM_MODEL when None.
        temperature: Sampling temperature (0.0 = deterministic).
        streaming: Enable streaming responses.
        max_tokens: Maximum tokens for the response (optional).
        reasoning: Optional OpenRouter reasoning config passed through to
                   ChatOpenRouter (e.g. {"enabled": False} to turn thinking off).

    Returns:
        Configured ChatOpenRouter (a BaseChatModel).

    Environment Variables:
        - OPENROUTER_API_KEY: required.
        - DEFAULT_LLM_MODEL: fallback model when model_name is not provided.
        - OPENROUTER_BASE_URL: optional custom endpoint (e.g. internal reverse proxy).
        - OPENROUTER_PROVIDER: optional JSON provider-routing config.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY not found in environment variables. "
            "Please set OPENROUTER_API_KEY in your .env file."
        )

    model = model_name or get_default_model()
    # OpenRouter ids are "provider/model"; assume OpenAI if no provider is given.
    if "/" not in model:
        model = f"openai/{model}"

    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "temperature": temperature,
        "streaming": streaming,
        # Bound every request so a proxy that swallows a request without ever
        # answering (observed with the internal reverse proxy) surfaces as a
        # timeout + SDK retry instead of hanging the review forever.
        "timeout": int_env("OPENROUTER_TIMEOUT_MS", 300000),
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    # Honor a custom OpenRouter endpoint (e.g. an internal reverse proxy) when set;
    # if OPENROUTER_BASE_URL is unset, ChatOpenRouter targets OpenRouter directly.
    base_url = os.getenv("OPENROUTER_BASE_URL", "").strip()
    if base_url:
        kwargs["base_url"] = base_url

    provider = _get_openrouter_provider()
    if provider:
        kwargs["openrouter_provider"] = provider

    if reasoning is not None:
        kwargs["reasoning"] = reasoning

    return ChatOpenRouter(**kwargs)


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

    Uses {"effort": "none"} rather than {"enabled": false}: the openrouter
    SDK's ChatRequestReasoning model only knows effort/summary, so an
    "enabled" key is silently dropped and an empty {} reaches the wire.
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
    return get_llm_model(
        actual_model,
        temperature=temperature,
        streaming=False,
        reasoning=_structured_reasoning_for(actual_model),
    )
