"""
Dataview Reviewer Agent - Review dataview registration files with MCP Tools

This module provides dataview review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
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


def get_dataview_reviewer_prompt(context: DataviewContext) -> str:
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
        # Create agent with tools
        agent = await create_dataview_reviewer_agent()
        
        # Build input with context
        input_text = get_dataview_reviewer_prompt(context)
        
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

