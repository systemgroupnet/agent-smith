"""
LangChain Configuration Module for OpenRouter Integration

This module provides configuration for using LangChain with OpenRouter as the LLM provider.
OpenRouter allows access to multiple LLM providers through a single API endpoint.

Based on LangChain docs: https://docs.langchain.com/oss/python/langchain/models
Uses init_chat_model for provider-agnostic model initialization.
"""

import os
from typing import Optional
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel


def get_default_model() -> str:
    """
    Return the default LLM model name from the environment.

    Reads DEFAULT_LLM_MODEL at call time so that changes to the environment
    variable after import are respected.

    Environment Variables:
        - DEFAULT_LLM_MODEL: Model identifier in OpenRouter format
                             (default: deepseek/deepseek-v4-pro)
    """
    return os.getenv("DEFAULT_LLM_MODEL", "deepseek/deepseek-v4-pro")


def get_llm_model(
    model_name: Optional[str] = None,
    temperature: float = 0.0,
    streaming: bool = False,
    max_tokens: Optional[int] = None,
) -> BaseChatModel:
    """
    Get a configured LLM model using OpenRouter.
    
    Uses init_chat_model for provider-agnostic initialization, allowing
    seamless switching between different LLM providers via OpenRouter.
    
    Args:
        model_name: The model to use. When None or omitted, falls back to
                   DEFAULT_LLM_MODEL env var (default: deepseek/deepseek-v4-pro).
                   Examples:
                   - "openai/gpt-4o-mini" (OpenRouter format)
                   - "deepseek/deepseek-v4-pro"
                   - "google/gemini-pro"
        temperature: Model temperature (0.0 = deterministic, 1.0 = creative)
        streaming: Enable streaming responses
        max_tokens: Maximum tokens for response (optional)
    
    Returns:
        Configured BaseChatModel instance for LangChain
    
    Environment Variables:
        - OPENROUTER_API_KEY: OpenRouter API key (required)
        - OPENROUTER_BASE_URL: OpenRouter base URL (default: https://openrouter.ai/api/v1)
        - DEFAULT_LLM_MODEL: Fallback model when model_name is not provided
    """
    # Get OpenRouter API key
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY not found in environment variables. "
            "Please set OPENROUTER_API_KEY in your .env file."
        )
    
    # Get OpenRouter base URL (default to standard OpenRouter endpoint)
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    
    # Resolve model: explicit argument takes priority, then env var
    model = model_name or get_default_model()
    
    # Convert model name to OpenRouter format if needed
    # OpenRouter uses format: "provider/model-name"
    if "/" not in model:
        # If no provider specified, assume OpenAI
        model = f"openai/{model}"
    
    # Build configuration kwargs
    config_kwargs = {
        "temperature": temperature,
        "streaming": streaming,
        "api_key": api_key,
        "base_url": base_url,
    }
    
    if max_tokens:
        config_kwargs["max_tokens"] = max_tokens
    
    # Use init_chat_model for provider-agnostic initialization
    # OpenRouter uses OpenAI-compatible API, so we use "openai" as the provider
    return init_chat_model(
        model=model,
        model_provider="openai",  # OpenRouter uses OpenAI-compatible API
        **config_kwargs
    )


def get_reasoning_model(effort: str = "medium", temperature: float = 0.0) -> BaseChatModel:
    """
    Get a reasoning model (o1/o3 series) configured for OpenRouter.
    
    Args:
        effort: Reasoning effort level - "low", "medium", or "high"
        temperature: Model temperature
    
    Returns:
        Configured BaseChatModel for reasoning tasks
    """
    # Map effort to appropriate model
    effort_to_model = {
        "low": "openai/o1-mini",
        "medium": "openai/o1-mini", 
        "high": "openai/o1-preview"
    }
    
    model = effort_to_model.get(effort, "openai/o1-mini")
    return get_llm_model(model, temperature=temperature)


def get_fast_model(temperature: float = 0.0) -> BaseChatModel:
    """
    Get a fast, cost-effective model for simple tasks.
    
    Args:
        temperature: Model temperature
        
    Returns:
        Configured BaseChatModel for fast inference
    """
    return get_llm_model(os.getenv("FAST_LLM_MODEL", "openai/gpt-4o-mini"), temperature=temperature)


def get_smart_model(temperature: float = 0.0) -> BaseChatModel:
    """
    Get a powerful model for complex reasoning tasks.
    
    Args:
        temperature: Model temperature
        
    Returns:
        Configured BaseChatModel for complex tasks
    """
    return get_llm_model(os.getenv("SMART_LLM_MODEL", "openai/gpt-4o"), temperature=temperature)


# Legacy model name mapping for backward compatibility with old agent code
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
) -> BaseChatModel:
    """
    Get a model configured for a specific agent, handling legacy model names.
    
    Args:
        agent_model_name: The model name specified in the agent (e.g., "gpt-5-nano")
        temperature: Model temperature
        streaming: Enable streaming
        max_tokens: Maximum tokens for response (optional)
    
    Returns:
        Configured BaseChatModel instance
    """
    # Map legacy names to actual model names
    actual_model = MODEL_NAME_MAPPING.get(agent_model_name, agent_model_name)
    return get_llm_model(actual_model, temperature=temperature, streaming=streaming, max_tokens=max_tokens)
