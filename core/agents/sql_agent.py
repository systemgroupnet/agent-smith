"""
SQL Reviewer Agent - Review SQL migrations and table operations with MCP Tools

This module provides SQL review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

import time
from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from core.utils.md_loader import load_md_content
from core.prompts import get_sql_reviewer_prompt
from dto.general import ReviewResponse


class SqlContext(BaseModel):
    """Context for SQL review"""
    content: str
    diff: str
    file_name: str
    file_path: Optional[str] = None
    jira_task_number: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_url: Optional[str] = None
    source_branch: Optional[str] = None
    project_path: Optional[str] = None
    mr_context: Optional[str] = None


async def create_sql_reviewer_agent():
    """
    Create SQL reviewer agent with MCP tools.
    
    The agent can use GitLab, Jira, and Confluence tools to fetch additional context.
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab", "atlassian"],  # GitLab + Atlassian (Jira/Confluence) tools
        temperature=0.3,
        max_iterations=10,
        verbose=False,
    )


async def run_sql_review(context: SqlContext) -> ReviewResponse:
    """
    Run SQL review with MCP tools.
    
    The agent will automatically use MCP tools if it needs additional
    context about the database changes.
    
    Args:
        context: SQL context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    start_time = time.time()
    print(f"  🔍 Running SQL review...")
    
    try:
        # Load additional content
        additional_content = ""
        if (context.file_path and context.gitlab_url and context.project_path 
            and context.source_branch and context.gitlab_token):
            try:
                additional_content = await load_md_content(
                    agent_type="sql",
                    file_path=context.file_path,
                    module_name=None,  # SQL files typically don't have module context
                    gitlab_url=context.gitlab_url,
                    project_path=context.project_path,
                    source_branch=context.source_branch,
                    gitlab_token=context.gitlab_token
                )
            except Exception as conv_error:
                print(f"  ⚠️  Warning: Failed to load additional content: {conv_error}")
                additional_content = ""
        
        # Create agent with tools
        agent = await create_sql_reviewer_agent()
        
        # Build input with context and additional content
        input_text = get_sql_reviewer_prompt(context, additional_content)
        
        # Run agent
        result = await agent.run(input_text)
        
        elapsed_time = time.time() - start_time
        print(f"  ✅ SQL review complete: {len(result.issues)} issues found (⏱️  {elapsed_time:.2f}s)")
        return result
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        print(f"  ❌ SQL review failed after {elapsed_time:.2f}s: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])

