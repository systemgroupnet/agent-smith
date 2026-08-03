"""The reviewer's LLM layer.

One object stands between every agent and every provider:
:class:`core.llm.gateway.LLMGateway`.
"""

from core.llm.gateway import LLMGateway, Provider

__all__ = ["LLMGateway", "Provider"]
