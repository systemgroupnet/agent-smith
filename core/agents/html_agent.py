"""
HTML Reviewer Agent - Review HTML template files with MCP Tools

This module provides HTML review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from dto.general import ReviewResponse


class HtmlContext(BaseModel):
    """Context for HTML review"""
    content: str
    diff: str
    file_name: str
    file_path: Optional[str] = None
    jira_task_number: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_url: Optional[str] = None
    source_branch: Optional[str] = None
    project_path: Optional[str] = None


def get_html_reviewer_prompt(context: HtmlContext) -> str:
    """Get HTML reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert HTML reviewer for the application frontend.

## Critical Review Areas

### 1. Semantic HTML
- Proper use of semantic tags
- Correct structure and nesting
- Accessibility attributes

### 2. Accessibility
- ARIA labels where needed
- Keyboard navigation support
- Screen reader compatibility

### 3. Best Practices
- No inline styles
- Proper form structure
- Valid HTML5

### 4. Angular Templates
- Correct directive usage
- Proper data binding
- Event handler patterns

## Severity Levels
- **high**: Accessibility violations, broken functionality
- **medium**: Semantic issues, missing best practices
- **low**: Style improvements, minor optimizations

**CRITICAL**: For EVERY high severity issue, explain WHY it's critical and the potential impact.

## TOOLS AVAILABLE
You have access to MCP tools for GitLab, Jira, and Confluence that can help you:
- Fetch related HTML templates from the repository
- Look up Jira issues to understand UI requirements
- View Confluence documentation about HTML/accessibility standards
- Fetch related TypeScript component files to understand data binding
- Understand the UI/UX requirements

Use these tools if you need additional context to perform your review, but avoid unnecessary tool calls.

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


async def create_html_reviewer_agent():
    """
    Create HTML reviewer agent with MCP tools.
    
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


async def run_html_review(context: HtmlContext) -> ReviewResponse:
    """
    Run HTML review with MCP tools.
    
    The agent will automatically use MCP tools if it needs additional
    context about the HTML templates.
    
    Args:
        context: HTML context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running HTML review...")
    
    try:
        # Create agent with tools
        agent = await create_html_reviewer_agent()
        
        # Build input with context
        input_text = get_html_reviewer_prompt(context)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ HTML review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ HTML review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])

