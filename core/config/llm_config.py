"""
LLM Configuration Module for OpenRouter Integration

This module provides configuration for using Pydantic AI with OpenRouter as the LLM provider.
OpenRouter allows access to multiple LLM providers through a single API endpoint.
"""

import os
from typing import Optional
from pydantic_ai.models.openai import OpenAIModel


def get_llm_model(model_name: str = "gpt-4o-mini") -> OpenAIModel:
    """
    Get a configured LLM model using OpenRouter.
    
    Args:
        model_name: The model to use (default: gpt-4o-mini)
                   Examples: 
                   - "openai/gpt-4o-mini" (OpenRouter format)
                   - "gpt-4o-mini" (will be converted to OpenRouter format)
                   - "anthropic/claude-3-5-sonnet-20241022"
                   - "google/gemini-pro"
    
    Returns:
        Configured OpenAIModel instance for Pydantic AI
    
    Environment Variables:
        - OPENROUTER_API_KEY: OpenRouter API key (required)
        - OPENROUTER_BASE_URL: OpenRouter base URL (default: https://openrouter.ai/api/v1)
        - DEFAULT_LLM_MODEL: Default model to use if not specified
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
    
    # Get model name from environment or use provided default
    model = os.getenv("DEFAULT_LLM_MODEL", model_name)
    
    # Convert model name to OpenRouter format if needed
    # OpenRouter uses format: "provider/model-name"
    if "/" not in model:
        # If no provider specified, assume OpenAI
        model = f"openai/{model}"
    
    # Create and return OpenAIModel configured for OpenRouter
    return OpenAIModel(
        model_name=model,
        api_key=api_key,
        base_url=base_url
    )


def get_reasoning_model(effort: str = "medium") -> OpenAIModel:
    """
    Get a reasoning model (o1/o3 series) configured for OpenRouter.
    
    Args:
        effort: Reasoning effort level - "low", "medium", or "high"
    
    Returns:
        Configured OpenAIModel for reasoning tasks
    """
    # Map effort to appropriate model
    effort_to_model = {
        "low": "openai/o1-mini",
        "medium": "openai/o1-mini", 
        "high": "openai/o1-preview"
    }
    
    model = effort_to_model.get(effort, "openai/o1-mini")
    return get_llm_model(model)


def get_fast_model() -> OpenAIModel:
    """
    Get a fast, cost-effective model for simple tasks.
    
    Returns:
        Configured OpenAIModel for fast inference
    """
    return get_llm_model(os.getenv("FAST_LLM_MODEL", "openai/gpt-4o-mini"))


def get_smart_model() -> OpenAIModel:
    """
    Get a powerful model for complex reasoning tasks.
    
    Returns:
        Configured OpenAIModel for complex tasks
    """
    return get_llm_model(os.getenv("SMART_LLM_MODEL", "openai/gpt-4o"))


# Legacy model name mapping for backward compatibility with old agent code
MODEL_NAME_MAPPING = {
    "gpt-5-nano": "openai/gpt-4o-mini",  # gpt-5-nano doesn't exist, map to gpt-4o-mini
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "o1-mini": "openai/o1-mini",
    "o1-preview": "openai/o1-preview",
}


def get_model_for_agent(agent_model_name: str) -> OpenAIModel:
    """
    Get a model configured for a specific agent, handling legacy model names.
    
    Args:
        agent_model_name: The model name specified in the agent (e.g., "gpt-5-nano")
    
    Returns:
        Configured OpenAIModel instance
    """
    # Map legacy names to actual model names
    actual_model = MODEL_NAME_MAPPING.get(agent_model_name, agent_model_name)
    return get_llm_model(actual_model)

