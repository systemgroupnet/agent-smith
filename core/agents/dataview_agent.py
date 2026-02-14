"""
Dataview Reviewer Agent - Review dataview registration files with MCP Tools

This module provides dataview review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from core.utils.conventions_loader import load_conventions
from core.prompts import get_dataview_reviewer_prompt
from dto.general import ReviewResponse


class DataviewContext(BaseModel):
    """Context for dataview review"""
    content: str
    diff: str
    file_name: str
    file_path: Optional[str] = None
    package_name: Optional[str] = None
    module_name: Optional[str] = None
    jira_task_number: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_url: Optional[str] = None
    source_branch: Optional[str] = None
    project_path: Optional[str] = None


async def create_dataview_reviewer_agent():
    """
    Create dataview reviewer agent with MCP tools.
    
    The agent can use GitLab, Jira, and Confluence tools to fetch additional context.
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab", "atlassian"],  # GitLab + Atlassian (Jira/Confluence) tools
        model_name="anthropic/claude-sonnet-4.5",
        temperature=0.3,
        max_iterations=15,
        verbose=False,
    )


async def run_dataview_review(context: DataviewContext) -> ReviewResponse:
    """
    Run dataview review with MCP tools.
    
    The agent will automatically use MCP tools to fetch requirements
    and related documentation as needed.
    
    Args:
        context: Dataview context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running dataview review...")
    
    try:
        # Load conventions
        conventions_text = ""
        if (context.file_path and context.gitlab_url and context.project_path 
            and context.source_branch and context.gitlab_token):
            try:
                conventions_text = await load_conventions(
                    agent_type="dataview",
                    file_path=context.file_path,
                    module_name=context.module_name,
                    gitlab_url=context.gitlab_url,
                    project_path=context.project_path,
                    source_branch=context.source_branch,
                    gitlab_token=context.gitlab_token
                )
            except Exception as conv_error:
                print(f"  ⚠️  Warning: Failed to load conventions: {conv_error}")
                conventions_text = ""
        
        # Create agent with tools
        agent = await create_dataview_reviewer_agent()
        
        # Build input with context and conventions
        input_text = get_dataview_reviewer_prompt(context, conventions_text)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ Dataview review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ Dataview review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])

