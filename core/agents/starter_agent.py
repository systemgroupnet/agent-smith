"""
Starter Agent - File Classification Agent

This agent classifies files from merge requests and extracts metadata
such as file type, module name, package name, and Jira task number.
"""

from core.agents.base_agent import StructuredOutputAgent
from core.prompts import get_starter_agent_prompt
from core.workflow.state import StarterResponse


# Create the starter agent instance
starter_agent = StructuredOutputAgent(
    system_prompt=get_starter_agent_prompt(),
    model_name="openai/gpt-5-nano",
    temperature=0.0,
    output_model=StarterResponse
)

