"""
Base Agent Class for Code Review Agents

This module provides the base class for all reviewer agents with common functionality
including structured output parsing, error handling, and retry logic.
"""

import time
from abc import ABC, abstractmethod
from typing import Optional, Type, TypeVar, Generic
from pydantic import BaseModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig

from core.config.langchain_config import get_default_model
from core.llm.gateway import LLMGateway
from core.utils.llm_retry import describe_error
from dto.general import ReviewResponse, ReviewIssue


T = TypeVar('T', bound=BaseModel)


class BaseReviewAgent(ABC, Generic[T]):
    """
    Base class for all review agents.
    
    Provides common functionality:
    - LLM model configuration
    - Structured output parsing
    - Error handling and retry logic
    - Prompt template management
    """
    
    def __init__(
        self,
        model_name: Optional[str] = None,
        temperature: float = 0.0,
        output_model: Type[T] = ReviewResponse,
    ):
        """
        Initialize base review agent.
        
        Args:
            model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
            temperature: Model temperature (0.0 = deterministic)
            output_model: Pydantic model for structured output
        """
        self.model_name = model_name or get_default_model()
        self.temperature = temperature
        self.output_model = output_model

        # Every call this agent makes goes through the gateway, which owns the
        # provider choice, the client, the retry loop and the failover. All the
        # agent contributes is how to build its chain from a model — the gateway
        # re-invokes _build_chain whenever the provider changes.
        #
        # structured=True because this agent's only LLM use is a
        # with_structured_output(method="function_calling") chain, which sends a
        # forced tool_choice: the gateway then turns reasoning off for models whose
        # thinking mode rejects that, per the model it is actually sending.
        self._gateway = LLMGateway(
            self._build_chain,
            label=f"{self.__class__.__name__} structured call",
            model_name=self.model_name,
            temperature=temperature,
            structured=True,
        )

    @abstractmethod
    def get_system_prompt(self) -> str:
        """
        Get the system prompt for this agent.
        Must be implemented by subclasses.

        Returns:
            System prompt string
        """
        pass

    def _build_chain(self, model):
        """Build the prompt → structured-LLM chain around ``model``.

        Called by the gateway, once per provider. ``function_calling`` is forced so
        the call routes to the same providers as tool calls (avoiding the
        json-schema/strict "no endpoints match your data policy" 404 on OpenRouter).
        """
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.get_system_prompt()),
            ("human", "{input}"),
        ])
        return prompt | model.with_structured_output(self.output_model, method="function_calling")

    async def run(
        self,
        input_text: str,
        context: Optional[dict] = None,
        config: Optional[RunnableConfig] = None,
    ) -> T:
        """
        Run the agent with given input.

        Args:
            input_text: Input text for the agent
            context: Optional context dictionary
            config: Optional tracing run config (carries session metadata)

        Returns:
            Structured output from the agent

        Raises:
            Exception: If agent execution fails after retries
        """
        start_time = time.time()
        agent_name = self.__class__.__name__

        try:
            # Prepare input
            input_data = {"input": input_text}
            if context:
                input_data.update(context)

            result = await self._gateway.ainvoke(input_data, config=config)


            elapsed_time = time.time() - start_time
            print(f"⏱️  {agent_name} completed in {elapsed_time:.2f}s")
            
            return result
            
        except Exception as e:
            elapsed_time = time.time() - start_time
            print(f"❌ Error in {agent_name} after {elapsed_time:.2f}s: {describe_error(e)}")
            raise
    
    def run_sync(self, input_text: str, context: Optional[dict] = None) -> T:
        """
        Run the agent synchronously. One attempt, no retry and no failover — the
        reviewer itself is async end to end and goes through ``run``.

        Args:
            input_text: Input text for the agent
            context: Optional context dictionary

        Returns:
            Structured output from the agent
        """
        try:
            # Prepare input
            input_data = {"input": input_text}
            if context:
                input_data.update(context)

            return self._gateway.invoke(input_data)


        except Exception as e:
            print(f"❌ Error in {self.__class__.__name__}: {describe_error(e)}")
            raise


class StructuredOutputAgent(BaseReviewAgent[T]):
    """
    Agent that uses structured output with custom system prompt.
    
    This class allows creating agents by just providing a system prompt
    without needing to subclass.
    """
    
    def __init__(
        self,
        system_prompt: str,
        model_name: Optional[str] = None,
        temperature: float = 0.0,
        output_model: Type[T] = ReviewResponse,
    ):
        """
        Initialize structured output agent.
        
        Args:
            system_prompt: The system prompt for this agent
            model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
            temperature: Model temperature
            output_model: Pydantic model for structured output
        """
        self._system_prompt = system_prompt
        super().__init__(model_name, temperature, output_model)
    
    def get_system_prompt(self) -> str:
        """Return the provided system prompt."""
        return self._system_prompt


def create_review_agent(
    system_prompt: str,
    model_name: Optional[str] = None,
    temperature: float = 0.0,
) -> StructuredOutputAgent[ReviewResponse]:
    """
    Factory function to create a review agent with standard ReviewResponse output.
    
    Args:
        system_prompt: The system prompt for the agent
        model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
        temperature: Model temperature
    
    Returns:
        Configured review agent
    """
    return StructuredOutputAgent(
        system_prompt=system_prompt,
        model_name=model_name,
        temperature=temperature,
        output_model=ReviewResponse
    )

