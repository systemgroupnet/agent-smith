"""
Base Agent Class for Code Review Agents

This module provides the base class for all reviewer agents with common functionality
including structured output parsing, error handling, and retry logic.
"""

from abc import ABC, abstractmethod
from typing import Optional, Type, TypeVar, Generic
from pydantic import BaseModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain_openai import ChatOpenAI

from core.config.langchain_config import get_model_for_agent
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
        model_name: str = "gpt-4o",
        temperature: float = 0.0,
        output_model: Type[T] = ReviewResponse,
    ):
        """
        Initialize base review agent.
        
        Args:
            model_name: Name of the LLM model to use
            temperature: Model temperature (0.0 = deterministic)
            output_model: Pydantic model for structured output
        """
        self.model_name = model_name
        self.temperature = temperature
        self.output_model = output_model
        
        # Initialize LLM
        self.llm = get_model_for_agent(
            model_name,
            temperature=temperature,
            streaming=False
        )
        
        # Initialize output parser
        self.parser = PydanticOutputParser(pydantic_object=output_model)
        
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
        
        # Add format instructions to system prompt
        format_instructions = self.parser.get_format_instructions()
        full_system_prompt = f"{system_prompt}\n\n{format_instructions}"
        
        # Create prompt template
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", full_system_prompt),
            ("human", "{input}")
        ])
        
        # Create chain: prompt | llm | parser
        self.chain = self.prompt | self.llm | self.parser
    
    async def run(self, input_text: str, context: Optional[dict] = None) -> T:
        """
        Run the agent with given input.
        
        Args:
            input_text: Input text for the agent
            context: Optional context dictionary
        
        Returns:
            Structured output from the agent
        
        Raises:
            Exception: If agent execution fails after retries
        """
        try:
            # Prepare input
            input_data = {"input": input_text}
            if context:
                input_data.update(context)
            
            # Invoke chain
            result = await self.chain.ainvoke(input_data)
            return result
            
        except Exception as e:
            print(f"❌ Error in {self.__class__.__name__}: {e}")
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
            print(f"❌ Error in {self.__class__.__name__}: {e}")
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
        model_name: str = "gpt-4o",
        temperature: float = 0.0,
        output_model: Type[T] = ReviewResponse,
    ):
        """
        Initialize structured output agent.
        
        Args:
            system_prompt: The system prompt for this agent
            model_name: Name of the LLM model to use
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
    model_name: str = "gpt-4o",
    temperature: float = 0.0,
) -> StructuredOutputAgent[ReviewResponse]:
    """
    Factory function to create a review agent with standard ReviewResponse output.
    
    Args:
        system_prompt: The system prompt for the agent
        model_name: Name of the LLM model to use
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

