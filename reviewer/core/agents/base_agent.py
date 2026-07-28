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

from core.config.langchain_config import get_structured_model_for_agent, get_default_model
from core.utils.llm_retry import with_llm_retry, describe_error
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
        resolved_model = model_name or get_default_model()
        self.model_name = resolved_model
        self.temperature = temperature
        self.output_model = output_model
        
        # Initialize LLM. This agent's only LLM use is the structured-output
        # chain (forced tool_choice), so use the structured variant — it turns
        # reasoning off for models whose thinking mode rejects forced tool_choice.
        self.llm = get_structured_model_for_agent(
            resolved_model,
            temperature=temperature,
        )
        
        # Build chain
        self._build_chain()
    
    @abstractmethod
    def get_system_prompt(self) -> str:
        """
        Get the system prompt for this agent.
        Must be implemented by subclasses.
        
        Returns:
            System prompt string
        """
        pass
    
    def _build_chain(self):
        """Build the LangChain chain with prompt + LLM + parser."""
        # Get system prompt from subclass
        system_prompt = self.get_system_prompt()
        
        # Use structured output with the LLM directly. Force function_calling so it
        # routes to the same providers as tool calls (avoids the json-schema/strict
        # "no endpoints match your data policy" 404 on OpenRouter).
        llm_with_structure = self.llm.with_structured_output(self.output_model, method="function_calling")
        
        # Create prompt template (no format instructions needed with structured output)
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", "{input}")
        ])
        
        # Create chain: prompt | llm_with_structure
        self.chain = self.prompt | llm_with_structure
    
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

            # Invoke chain, retrying transient gateway failures (e.g. the egress
            # security proxy's burst 403) but not hard quota/limit denials.
            result = await with_llm_retry(
                lambda: self.chain.ainvoke(input_data, config=config),
                label=f"{agent_name} structured call",
            )
            
            elapsed_time = time.time() - start_time
            print(f"⏱️  {agent_name} completed in {elapsed_time:.2f}s")
            
            return result
            
        except Exception as e:
            elapsed_time = time.time() - start_time
            print(f"❌ Error in {agent_name} after {elapsed_time:.2f}s: {describe_error(e)}")
            raise
    
    def run_sync(self, input_text: str, context: Optional[dict] = None) -> T:
        """
        Run the agent synchronously.
        
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
            
            # Invoke chain
            result = self.chain.invoke(input_data)
            return result
            
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

