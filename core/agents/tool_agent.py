"""
Tool-Enabled Agent - Agent with MCP Tool Integration

This module provides an agent base class that integrates with MCP tools,
allowing the LLM to automatically choose and use external tools (GitLab, Jira, etc.).

Usage:
    from core.agents.tool_agent import create_tool_agent
    
    agent = await create_tool_agent(
        system_prompt="You are a code reviewer...",
        tools=["gitlab", "atlassian"],  # or specific tool list
        output_model=ReviewResponse
    )
    
    result = await agent.run(input_text)
"""

from __future__ import annotations

from typing import Optional, Type, TypeVar, List, Union
from pydantic import BaseModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain_core.tools import BaseTool

from core.config.langchain_config import get_model_for_agent, get_default_model
from core.MCP.toolkit_manager import get_mcp_toolkit_manager
from dto.general import ReviewResponse


T = TypeVar('T', bound=BaseModel)


class ToolEnabledAgent:
    """
    An agent that can use external tools (MCP) to assist with its task.
    
    This agent uses LangChain's AgentExecutor to allow the LLM to:
    - Choose which tools to use based on the task
    - Call tools with appropriate parameters
    - Use tool results to generate better responses
    """
    
    def __init__(
        self,
        system_prompt: str,
        tools: List[BaseTool],
        model_name: Optional[str] = None,
        temperature: float = 0.8,
        output_model: Optional[Type[T]] = None,
        max_iterations: int = 15,
        verbose: bool = True,
    ):
        """
        Initialize tool-enabled agent.
        
        Args:
            system_prompt: The system prompt for the agent
            tools: List of LangChain tools the agent can use
            model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
            temperature: Model temperature (0.0 = deterministic)
            output_model: Optional Pydantic model for structured output
            max_iterations: Maximum tool usage iterations
            verbose: Whether to print agent thoughts and tool calls
        """
        resolved_model = model_name or get_default_model()
        self.system_prompt = system_prompt
        self.tools = tools
        self.model_name = resolved_model
        self.temperature = temperature
        self.output_model = output_model
        self.max_iterations = max_iterations
        self.verbose = verbose
        
        # Initialize LLM
        self.llm = get_model_for_agent(
            resolved_model,
            temperature=temperature,
            streaming=False
        )
        
        # Build agent
        self._build_agent()
    
    def _build_agent(self):
        """Build the LangChain agent with tools."""
        # Create prompt template
        if self.output_model:
            # Add structured output instructions
            # Note: We escape the curly braces in format instructions to avoid
            # ChatPromptTemplate treating them as variables
            parser = PydanticOutputParser(pydantic_object=self.output_model)
            format_instructions = parser.get_format_instructions()
            # Escape curly braces by doubling them for ChatPromptTemplate
            escaped_instructions = format_instructions.replace("{", "{{").replace("}", "}}")
            full_system_prompt = f"{self.system_prompt}\n\n{escaped_instructions}"
        else:
            full_system_prompt = self.system_prompt
        
        # Create agent prompt with tool placeholders
        prompt = ChatPromptTemplate.from_messages([
            ("system", full_system_prompt),
            ("human", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ])
        
        # Create OpenAI tools agent
        agent = create_openai_tools_agent(
            llm=self.llm,
            tools=self.tools,
            prompt=prompt,
        )
        
        # Create agent executor
        self.agent_executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            verbose=self.verbose,
            max_iterations=self.max_iterations,
            handle_parsing_errors=True,
            return_intermediate_steps=False,
        )
    
    async def run(
        self,
        input_text: str,
        context: Optional[dict] = None,
        config: Optional[RunnableConfig] = None,
    ) -> Union[T, dict]:
        """
        Run the agent with given input.

        The agent will automatically choose and use tools as needed to
        complete the task.

        Args:
            input_text: Input text for the agent
            context: Optional context dictionary
            config: Optional LangSmith run config (carries thread metadata so the
                    agent's nested LLM/tool runs stay in the MR thread)

        Returns:
            Structured output (if output_model specified) or dict

        Raises:
            Exception: If agent execution fails
        """
        try:
            # Prepare input
            input_data = {"input": input_text}
            if context:
                input_data.update(context)

            # Invoke agent
            result = await self.agent_executor.ainvoke(input_data, config=config)
            
            # Extract output
            output = result.get("output", "")
            
            # Parse structured output if model specified
            if self.output_model:
                parser = PydanticOutputParser(pydantic_object=self.output_model)
                parsed_output = parser.parse(output)
                return parsed_output
            
            return output
            
        except Exception as e:
            print(f"❌ Error in ToolEnabledAgent: {e}")
            raise
    
    def run_sync(self, input_text: str, context: Optional[dict] = None) -> Union[T, dict]:
        """
        Run the agent synchronously.
        
        Args:
            input_text: Input text for the agent
            context: Optional context dictionary
        
        Returns:
            Structured output (if output_model specified) or dict
        """
        try:
            # Prepare input
            input_data = {"input": input_text}
            if context:
                input_data.update(context)
            
            # Invoke agent
            result = self.agent_executor.invoke(input_data)
            
            # Extract output
            output = result.get("output", "")
            
            # Parse structured output if model specified
            if self.output_model:
                parser = PydanticOutputParser(pydantic_object=self.output_model)
                parsed_output = parser.parse(output)
                return parsed_output
            
            return output
            
        except Exception as e:
            print(f"❌ Error in ToolEnabledAgent: {e}")
            raise


async def create_tool_agent(
    system_prompt: str,
    tools: Optional[Union[List[str], List[BaseTool]]] = None,
    model_name: Optional[str] = None,
    temperature: float = 0.0,
    output_model: Optional[Type[T]] = None,
    max_iterations: int = 15,
    verbose: bool = True,
) -> ToolEnabledAgent:
    """
    Factory function to create a tool-enabled agent.
    
    Args:
        system_prompt: The system prompt for the agent
        tools: List of tool names ("gitlab", "atlassian") or BaseTool objects.
               If None, all available MCP tools are included.
        model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
        temperature: Model temperature
        output_model: Optional Pydantic model for structured output
        max_iterations: Maximum tool usage iterations
        verbose: Whether to print agent thoughts and tool calls
    
    Returns:
        Configured ToolEnabledAgent instance
    
    Example:
        # Create agent with all MCP tools
        agent = await create_tool_agent(
            system_prompt="Review this code...",
            output_model=ReviewResponse
        )
        
        # Create agent with specific tools
        agent = await create_tool_agent(
            system_prompt="Review this code...",
            tools=["gitlab", "atlassian"]
        )
    """
    # Get MCP toolkit manager
    manager = get_mcp_toolkit_manager()
    
    # Initialize if not already done
    if not manager.is_initialized:
        await manager.initialize()
    
    # Resolve tools
    resolved_tools: List[BaseTool] = []
    
    if tools is None:
        # Use all available tools
        resolved_tools = manager.get_all_tools()
    elif isinstance(tools, list) and len(tools) > 0:
        if isinstance(tools[0], str):
            # Tool names provided - resolve to actual tools
            tools_by_category = manager.get_tools_by_category()
            for tool_name in tools:
                if tool_name.lower() == "gitlab":
                    resolved_tools.extend(tools_by_category["gitlab"])
                elif tool_name.lower() == "atlassian":
                    resolved_tools.extend(tools_by_category["atlassian"])
                elif tool_name.lower() == "aiand":
                    resolved_tools.extend(tools_by_category["aiand"])
                else:
                    print(f"  ⚠️  Unknown tool category: {tool_name}")
        else:
            # BaseTool objects provided directly
            resolved_tools = tools
    
    if len(resolved_tools) == 0:
        print("  ⚠️  No tools available for agent")
    
    # Create and return agent
    return ToolEnabledAgent(
        system_prompt=system_prompt,
        tools=resolved_tools,
        model_name=model_name,
        temperature=temperature,
        output_model=output_model,
        max_iterations=max_iterations,
        verbose=verbose,
    )


async def create_review_agent_with_tools(
    system_prompt: str,
    tools: Optional[Union[List[str], List[BaseTool]]] = None,
    model_name: Optional[str] = None,
    temperature: float = 0.3,
    max_iterations: int = 15,
    verbose: bool = False,
) -> ToolEnabledAgent:
    """
    Factory function to create a review agent with tools and ReviewResponse output.
    
    This is a convenience function that automatically uses ReviewResponse as
    the output model, suitable for code review agents.
    
    Args:
        system_prompt: The system prompt for the agent
        tools: List of tool names or BaseTool objects
        model_name: Name of the LLM model to use. Defaults to DEFAULT_LLM_MODEL env var.
        temperature: Model temperature
        max_iterations: Maximum tool usage iterations
        verbose: Whether to print agent thoughts and tool calls
    
    Returns:
        Configured ToolEnabledAgent with ReviewResponse output
    """
    return await create_tool_agent(
        system_prompt=system_prompt,
        tools=tools,
        model_name=model_name,
        temperature=temperature,
        output_model=ReviewResponse,
        max_iterations=max_iterations,
        verbose=verbose,
    )



