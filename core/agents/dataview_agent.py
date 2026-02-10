"""
Dataview Reviewer Agent - Review dataview registration files with MCP Tools

This module provides dataview review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from core.utils.conventions_loader import load_conventions
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


def get_dataview_reviewer_prompt(context: DataviewContext, conventions: str = "") -> str:
    """Get dataview reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.package_name:
        context_info.append(f"Package: {context.package_name}")
    if context.module_name:
        context_info.append(f"Module: {context.module_name}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert reviewer for dataview files in the application.

## Critical Review Areas

### 1. Dataview Definition
- Proper dataview registration
- Correct field mappings
- Valid data sources

### 2. Query Optimization
- Efficient data retrieval
- Proper joins and filters
- No N+1 query issues

### 3. Data Transformation
- Correct data formatting
- Proper type conversions
- Consistent output structure

### 4. Business Logic Alignment
- Matches business requirements
- Proper access control
- Correct data aggregation

## Severity Levels
- **high**: Incorrect data mapping, security issues, broken queries
- **medium**: Suboptimal queries, missing validations
- **low**: Style issues, naming improvements

**CRITICAL**: For EVERY high severity issue, explain WHY it's critical and the potential impact.

## TOOLS AVAILABLE
You have access to MCP tools for GitLab, Jira, and Confluence that can help you:
- Fetch related dataview files from the repository for consistency
- Look up Jira issues to understand dataview requirements
- View Confluence documentation about dataview patterns and standards
- Fetch related service files to understand data sources
- Understand the business requirements for the dataview

Use these tools if you need additional context to perform your review, especially for business logic alignment.

=== Project Conventions ===
{conventions if conventions else "No specific conventions defined for this file type."}

=== Context Information ===
{context_section}

=== File Content ===
```
{context.content}
```

=== Git Diff ===
```
{context.diff}
```"""


async def create_dataview_reviewer_agent():
    """
    Create dataview reviewer agent with MCP tools.
    
    The agent can use GitLab, Jira, and Confluence tools to fetch additional context.
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab", "atlassian"],  # GitLab + Atlassian (Jira/Confluence) tools
        model_name="gpt-4o",
        temperature=0.0,
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

