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

import os
import re
from typing import Optional, Type, TypeVar, List, Union
from pydantic import BaseModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableConfig
# In LangChain v1 the legacy agent constructors live in langchain-classic.
from langchain_classic.agents import create_openai_tools_agent, AgentExecutor
from langchain_core.tools import BaseTool

from core.config.langchain_config import (
    get_model_for_agent,
    get_structured_model_for_agent,
    get_default_model,
)
from core.utils.llm_retry import with_llm_retry
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
            # Keep the tool call/result trace so we can (a) diagnose why a run
            # looped to max_iterations and (b) reconstruct a structured answer
            # from what was gathered when the agent doesn't emit clean JSON.
            return_intermediate_steps=True,
        )

    def _parse_text_output(self, output: str):
        """Best-effort parse of the agent's free-text answer into ``output_model``.

        A tool-calling agent returns free text that usually *contains* the JSON
        answer but often wraps it in prose and several fenced code blocks — e.g. a
        ```go snippet quoting the reviewed code, followed by the real ```json
        result. ``PydanticOutputParser.parse`` can latch onto the wrong fence and
        raise, so we try progressively more forgiving strategies. Returns ``None``
        (rather than raising) when no JSON can be recovered, so the caller can fall
        back to an LLM re-format.
        """
        parser = PydanticOutputParser(pydantic_object=self.output_model)

        # 1) Native parse — handles a clean JSON body or a single ```json block.
        try:
            return parser.parse(output)
        except Exception:
            pass

        # 2) Scan fenced code blocks, last first (the final answer comes last),
        #    skipping non-JSON blocks such as ```go snippets embedded in the prose.
        for block in reversed(re.findall(r"```(?:json)?\s*(.*?)```", output, re.DOTALL)):
            block = block.strip()
            if block.startswith("{"):
                try:
                    return self.output_model.model_validate_json(block)
                except Exception:
                    continue

        # 3) Widest {...} span anywhere in the text.
        start, end = output.find("{"), output.rfind("}")
        if start != -1 and end > start:
            try:
                return self.output_model.model_validate_json(output[start:end + 1])
            except Exception:
                pass

        return None

    @staticmethod
    def _tool_trace_enabled() -> bool:
        """Whether to print the per-run tool-call trace. On by default; set
        LOG_TOOL_TRACE=false to silence the verbose block in CI logs."""
        return os.getenv("LOG_TOOL_TRACE", "true").strip().lower() in ("1", "true", "yes", "on")

    def _log_run(self, steps, output):
        """Surface what the agent actually did — the key diagnostic for a run that
        loops to max_iterations (tool failures vs. genuine over-exploration).
        The verbose per-call trace is gated by LOG_TOOL_TRACE (default on)."""
        if steps and self._tool_trace_enabled():
            print(f"  🔧 [ToolEnabledAgent] {len(steps)} tool call(s) this run:")
            for i, step in enumerate(steps, 1):
                try:
                    action, observation = step
                except (ValueError, TypeError):
                    continue
                obs = observation if isinstance(observation, str) else str(observation)
                obs_snippet = " ".join(obs.split())[:300]
                print(f"     {i}. {getattr(action, 'tool', '?')}({getattr(action, 'tool_input', '')}) -> {obs_snippet}")
        # Always surface the max-iterations sentinel — it's a one-liner and the
        # most important signal, independent of the verbose trace toggle.
        if isinstance(output, str) and "Agent stopped due to max iterations" in output:
            print("  ⚠️  [ToolEnabledAgent] hit max_iterations without emitting a final answer")

    def _get_structured_llm(self):
        """LLM for the salvage structured-output call.

        function_calling routes to the same providers as the agent's tool calls
        (avoids the json-schema/strict data-policy 404), but it sends a forced
        tool_choice, which thinking-mode models (DeepSeek V4) reject — so this
        uses the structured model variant, which disables reasoning for those
        models only. The agent loop itself stays on self.llm with reasoning on.
        """
        llm = get_structured_model_for_agent(self.model_name, temperature=self.temperature)
        return llm.with_structured_output(self.output_model, method="function_calling")

    def _build_structuring_prompt(self, output, steps):
        """Bounded prompt to derive structured output from what the agent gathered,
        used when the agent's own final text isn't parseable JSON (prose, or the
        max_iterations sentinel). Per-observation size is capped to bound tokens."""
        parts = [
            "Produce the required structured output for this code review, based only "
            "on the reviewer notes and tool results below. If no genuine problem is "
            "evident, return an empty issues list."
        ]
        if isinstance(output, str) and output and "Agent stopped due to max iterations" not in output:
            parts.append(f"\nReviewer notes:\n{output[:4000]}")
        if steps:
            parts.append("\nContext gathered via tools:")
            for step in steps[-12:]:
                try:
                    action, observation = step
                except (ValueError, TypeError):
                    continue
                obs = observation if isinstance(observation, str) else str(observation)
                parts.append(f"- {getattr(action, 'tool', '?')}: {obs[:1500]}")
        return "\n".join(parts)

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

            # Invoke agent, retrying transient gateway failures (e.g. the egress
            # security proxy's burst 403) but not hard quota/limit denials.
            result = await with_llm_retry(
                lambda: self.agent_executor.ainvoke(input_data, config=config),
                label=f"{self.__class__.__name__} agent",
            )

            # Extract the final output + the tool call/result trace
            output = result.get("output", "")
            steps = result.get("intermediate_steps", []) or []
            self._log_run(steps, output)

            # Parse structured output if model specified
            if self.output_model:
                parsed_output = self._parse_text_output(output)
                if parsed_output is None:
                    # Agent didn't emit parseable JSON (prose, or it hit
                    # max_iterations and returned the sentinel string). Derive the
                    # structured result from what it actually gathered, via a single
                    # reliable with_structured_output call.
                    print("  ⚠️  [ToolEnabledAgent] No parseable final answer; deriving structured output from gathered context")
                    prompt = self._build_structuring_prompt(output, steps)
                    parsed_output = await with_llm_retry(
                        lambda: self._get_structured_llm().ainvoke(prompt),
                        label=f"{self.__class__.__name__} salvage",
                    )
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

            # Extract the final output + the tool call/result trace
            output = result.get("output", "")
            steps = result.get("intermediate_steps", []) or []
            self._log_run(steps, output)

            # Parse structured output if model specified
            if self.output_model:
                parsed_output = self._parse_text_output(output)
                if parsed_output is None:
                    print("  ⚠️  [ToolEnabledAgent] No parseable final answer; deriving structured output from gathered context")
                    parsed_output = self._get_structured_llm().invoke(
                        self._build_structuring_prompt(output, steps)
                    )
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
                elif tool_name.lower() == "cocoindex":
                    resolved_tools.extend(tools_by_category["cocoindex"])
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



