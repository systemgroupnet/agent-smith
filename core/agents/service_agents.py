"""
Service Agents - Semantic and Business Logic Reviewers with MCP Tools

This module provides two specialized agents for reviewing Go service files:
1. Semantic Reviewer - Technical code quality and Go best practices
2. Business Logic Reviewer - Business requirements fulfillment

Both agents now use MCP tools to automatically fetch external data (Jira, Confluence, GitLab)
as needed during the review process. The LLM chooses which tools to use.
"""

import asyncio
import os
from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from core.utils.conventions_loader import load_conventions
from core.prompts import get_semantic_reviewer_prompt, get_business_logic_reviewer_prompt
from dto.general import ReviewResponse


class ServiceContext(BaseModel):
    """Context for service review agents"""
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


# =============================================================================
# Agent Factories
# =============================================================================

async def create_semantic_reviewer_agent():
    """
    Create semantic code reviewer agent with GitLab MCP tools.
    
    The agent can use GitLab tools to fetch additional file context if needed.
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab"],  # Only GitLab tools for semantic review
        model_name="anthropic/claude-sonnet-4.5",
        temperature=0.3,
        max_iterations=10,
        verbose=False,
    )


async def create_business_logic_reviewer_agent():
    """
    Create business logic reviewer agent with Jira, Confluence, and GitLab MCP tools.
    
    The agent can use:
    - Jira tools to fetch task/requirement information
    - Confluence tools to fetch documentation
    - GitLab tools to fetch related files
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab", "atlassian"],  # GitLab + Atlassian (Jira/Confluence) tools
        model_name="gpt-4o",
        temperature=0.5,
        max_iterations=15,
        verbose=False,
    )


# =============================================================================
# Review Functions
# =============================================================================

async def run_semantic_review(context: ServiceContext) -> ReviewResponse:
    """
    Run semantic code review with MCP tools.
    
    The agent will automatically use GitLab tools if it needs additional
    context about the codebase.
    
    Args:
        context: Service context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running semantic code review...")
    
    try:
        # Load conventions
        conventions_text = ""
        if (context.file_path and context.gitlab_url and context.project_path 
            and context.source_branch and context.gitlab_token):
            try:
                conventions_text = await load_conventions(
                    agent_type="service",
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
        agent = await create_semantic_reviewer_agent()
        
        # Build input with context and conventions
        input_text = get_semantic_reviewer_prompt(context, conventions_text)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ Semantic review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ Semantic review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])


async def run_business_logic_review(context: ServiceContext) -> ReviewResponse:
    """
    Run business logic review with MCP tools.
    
    The agent will automatically use Jira/Confluence tools to fetch
    requirements and documentation as needed.
    
    Args:
        context: Service context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running business logic review...")
    
    try:
        # Create agent with tools
        agent = await create_business_logic_reviewer_agent()
        
        # Build input with context
        input_text = get_business_logic_reviewer_prompt(context)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ Business logic review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ Business logic review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])


async def run_combined_service_review(context: ServiceContext) -> ReviewResponse:
    """
    Run both semantic and business logic reviews in parallel.
    
    Both agents will automatically use MCP tools as needed:
    - Semantic reviewer: Uses GitLab tools for code context
    - Business logic reviewer: Uses Jira/Confluence/GitLab tools for requirements
    
    Business logic review can be disabled by setting BUSINESS_LOGIC_REVIEW_ENABLED=false
    in the environment variables.
    
    Args:
        context: Service context with file information
        
    Returns:
        Combined ReviewResponse with all issues
    """
    print(f"\n📝 Starting combined service review (parallel)")
    print(f"   File: {context.file_name}")
    
    # Check if business logic review is enabled
    business_logic_enabled = os.getenv("BUSINESS_LOGIC_REVIEW_ENABLED", "true").lower() == "true"
    
    try:
        # Always run semantic review
        semantic_task = run_semantic_review(context)
        
        # Conditionally run business logic review
        if business_logic_enabled:
            print(f"  ℹ️  Business logic review enabled")
            business_task = run_business_logic_review(context)
            
            semantic_result, business_result = await asyncio.gather(
                semantic_task,
                business_task,
                return_exceptions=True
            )
            
            # Handle exceptions
            if isinstance(semantic_result, Exception):
                print(f"  ⚠️  Semantic review exception: {semantic_result}")
                semantic_result = ReviewResponse(issues=[])
            
            if isinstance(business_result, Exception):
                print(f"  ⚠️  Business logic review exception: {business_result}")
                business_result = ReviewResponse(issues=[])
            
            # Combine results
            all_issues = semantic_result.issues + business_result.issues
            
            print(f"\n✅ Combined review complete:")
            print(f"   - Semantic issues: {len(semantic_result.issues)}")
            print(f"   - Business logic issues: {len(business_result.issues)}")
            print(f"   - Total issues: {len(all_issues)}")
        else:
            print(f"  ℹ️  Business logic review disabled")
            semantic_result = await semantic_task
            
            # Handle exceptions
            if isinstance(semantic_result, Exception):
                print(f"  ⚠️  Semantic review exception: {semantic_result}")
                semantic_result = ReviewResponse(issues=[])
            
            all_issues = semantic_result.issues
            
            print(f"\n✅ Review complete (semantic only):")
            print(f"   - Semantic issues: {len(semantic_result.issues)}")
            print(f"   - Total issues: {len(all_issues)}")
        
        return ReviewResponse(issues=all_issues)
        
    except Exception as e:
        print(f"  ❌ Combined review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        return ReviewResponse(issues=[])



