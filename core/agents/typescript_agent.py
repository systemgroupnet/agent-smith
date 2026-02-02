"""
TypeScript Reviewer Agent - Review TypeScript files with MCP Tools

This module provides TypeScript review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from dto.general import ReviewResponse


class TypescriptContext(BaseModel):
    """Context for TypeScript review"""
    content: str
    diff: str
    file_name: str
    file_path: Optional[str] = None
    jira_task_number: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_url: Optional[str] = None
    source_branch: Optional[str] = None
    project_path: Optional[str] = None


def get_typescript_reviewer_prompt(context: TypescriptContext) -> str:
    """Get TypeScript reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert TypeScript reviewer for the application frontend.

## Critical Review Areas

### 1. Type Safety
- Proper type annotations
- No use of 'any' without justification
- Correct interface/type definitions

### 2. Code Quality
- Proper error handling
- Clean code structure
- No code duplication

### 3. Best Practices
- Modern TypeScript patterns
- Proper async/await usage
- Correct null/undefined handling

### 4. Angular/Framework Specific
- Proper component structure
- Correct service usage
- Proper RxJS patterns

## Severity Levels
- **high**: Type errors, runtime bugs, security issues
- **medium**: Code quality issues, suboptimal patterns
- **low**: Style improvements, naming suggestions

**CRITICAL**: For EVERY high severity issue, explain WHY it's critical and the potential impact.

## TOOLS AVAILABLE
You have access to MCP tools for GitLab, Jira, and Confluence that can help you:
- Fetch related TypeScript files from the repository (components, services, models)
- Look up Jira issues to understand feature requirements
- View Confluence documentation about frontend architecture and standards
- Understand the broader context of the component/service

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


async def create_typescript_reviewer_agent():
    """
    Create TypeScript reviewer agent with MCP tools.
    
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


async def run_typescript_review(context: TypescriptContext) -> ReviewResponse:
    """
    Run TypeScript review with MCP tools.
    
    The agent will automatically use MCP tools if it needs additional
    context about the TypeScript code.
    
    Args:
        context: TypeScript context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running TypeScript review...")
    
    try:
        # Create agent with tools
        agent = await create_typescript_reviewer_agent()
        
        # Build input with context
        input_text = get_typescript_reviewer_prompt(context)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ TypeScript review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ TypeScript review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])

