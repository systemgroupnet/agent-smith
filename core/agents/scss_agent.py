"""
SCSS Reviewer Agent - Review SCSS/CSS files with MCP Tools

This module provides SCSS review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from core.utils.conventions_loader import load_conventions
from dto.general import ReviewResponse


class ScssContext(BaseModel):
    """Context for SCSS review"""
    content: str
    diff: str
    file_name: str
    file_path: Optional[str] = None
    jira_task_number: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_url: Optional[str] = None
    source_branch: Optional[str] = None
    project_path: Optional[str] = None


def get_scss_reviewer_prompt(context: ScssContext, conventions: str = "") -> str:
    """Get SCSS reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert SCSS/CSS reviewer for the application frontend.

## Critical Review Areas

### 1. SCSS Best Practices
- Proper nesting (max 3 levels)
- Efficient use of variables
- Mixins and functions usage

### 2. Performance
- Efficient selectors
- No redundant rules
- Proper use of imports

### 3. Maintainability
- Clear naming conventions
- Modular structure
- No magic numbers

### 4. Responsiveness
- Mobile-first approach
- Proper media queries
- Flexible layouts

## Severity Levels
- **high**: Performance issues, broken layouts
- **medium**: Maintainability concerns, code duplication
- **low**: Style improvements, naming suggestions

**CRITICAL**: For EVERY high severity issue, explain WHY it's critical and the potential impact.

## TOOLS AVAILABLE
You have access to MCP tools for GitLab, Jira, and Confluence that can help you:
- Fetch related SCSS files from the repository to check for consistency
- Look up Jira issues to understand styling requirements
- View Confluence documentation about design system and CSS standards
- Fetch related HTML templates to understand styling context
- Understand the design requirements

Use these tools if you need additional context to perform your review, but avoid unnecessary tool calls.

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


async def create_scss_reviewer_agent():
    """
    Create SCSS reviewer agent with MCP tools.
    
    The agent can use GitLab, Jira, and Confluence tools to fetch additional context.
    """
    return await create_review_agent_with_tools(
        system_prompt="",  # Will be replaced with context-specific prompt
        tools=["gitlab", "atlassian"],  # GitLab + Atlassian (Jira/Confluence) tools
        model_name="gpt-4o-mini",
        temperature=0.0,
        max_iterations=10,
        verbose=False,
    )


async def run_scss_review(context: ScssContext) -> ReviewResponse:
    """
    Run SCSS review with MCP tools.
    
    The agent will automatically use MCP tools if it needs additional
    context about the styles.
    
    Args:
        context: SCSS context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running SCSS review...")
    
    try:
        # Load conventions
        conventions_text = ""
        if (context.file_path and context.gitlab_url and context.project_path 
            and context.source_branch and context.gitlab_token):
            try:
                conventions_text = await load_conventions(
                    agent_type="scss",
                    file_path=context.file_path,
                    module_name=None,  # SCSS files typically don't have module context
                    gitlab_url=context.gitlab_url,
                    project_path=context.project_path,
                    source_branch=context.source_branch,
                    gitlab_token=context.gitlab_token
                )
            except Exception as conv_error:
                print(f"  ⚠️  Warning: Failed to load conventions: {conv_error}")
                conventions_text = ""
        
        # Create agent with tools
        agent = await create_scss_reviewer_agent()
        
        # Build input with context and conventions
        input_text = get_scss_reviewer_prompt(context, conventions_text)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ SCSS review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ SCSS review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])

