"""
Pbook Reviewer Agent - Review error message definition files with MCP Tools

This module provides pbook review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

import time
from typing import Optional
from pydantic import BaseModel
from langchain_core.runnables import RunnableConfig

from core.agents.tool_agent import create_review_agent_with_tools
from core.utils.md_loader import load_md_content
from core.utils.llm_retry import maybe_print_traceback
from core.prompts import get_pbook_reviewer_prompt
from dto.general import ReviewResponse


class PbookContext(BaseModel):
    """Context for pbook review"""
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


async def create_pbook_reviewer_agent():
    """
    Create pbook reviewer agent with MCP tools.
    
    The agent can use GitLab, Jira, and Confluence tools to fetch additional context.
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab", "atlassian", "cocoindex"],  # GitLab + Atlassian (Jira/Confluence) + cocoindex (code index)
        temperature=0.3,
        max_iterations=10,
        verbose=False,
    )


async def run_pbook_review(context: PbookContext, config: Optional[RunnableConfig] = None) -> ReviewResponse:
    """
    Run pbook review with MCP tools.
    
    The agent will automatically use MCP tools if it needs additional
    context about error messages.
    
    Args:
        context: Pbook context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    start_time = time.time()
    print(f"  🔍 Running pbook review...")
    
    try:
        # Load additional content
        additional_content = ""
        if (context.file_path and context.gitlab_url and context.project_path 
            and context.source_branch and context.gitlab_token):
            try:
                additional_content = await load_md_content(
                    agent_type="pbook",
                    file_path=context.file_path,
                    module_name=None,  # Pbook files typically don't have module context
                    gitlab_url=context.gitlab_url,
                    project_path=context.project_path,
                    source_branch=context.source_branch,
                    gitlab_token=context.gitlab_token
                )
            except Exception as conv_error:
                print(f"  ⚠️  Warning: Failed to load additional content: {conv_error}")
                additional_content = ""
        
        # Create agent with tools
        agent = await create_pbook_reviewer_agent()
        
        # Build input with context and additional content
        input_text = get_pbook_reviewer_prompt(context, additional_content)
        
        # Run agent
        result = await agent.run(input_text, config=config)
        
        elapsed_time = time.time() - start_time
        print(f"  ✅ Pbook review complete: {len(result.issues)} issues found (⏱️  {elapsed_time:.2f}s)")
        return result
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        print(f"  ❌ Pbook review failed after {elapsed_time:.2f}s: {e}")
        maybe_print_traceback(e)
        # Surface the failure instead of swallowing it into an empty result, so
        # this file is recorded as errored and the run exits non-zero. Other
        # files are unaffected (they are reviewed concurrently and isolated).
        raise

