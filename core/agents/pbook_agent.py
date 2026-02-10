"""
Pbook Reviewer Agent - Review error message definition files with MCP Tools

This module provides pbook review agent with MCP tool integration for fetching
external data (GitLab, Jira, Confluence) as needed during the review process.
"""

from typing import Optional
from pydantic import BaseModel

from core.agents.tool_agent import create_review_agent_with_tools
from core.utils.conventions_loader import load_conventions
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


def get_pbook_reviewer_prompt(context: PbookContext, conventions: str = "") -> str:
    """Get pbook reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert reviewer for pbook (error message) files in the application.

## Critical Review Areas

### 1. Error Message Quality
- Clear, descriptive error messages
- Consistent formatting and tone
- Proper bilingual support (Persian/English)

### 2. Error Code Structure
- Unique error codes
- Consistent naming conventions
- Proper categorization

### 3. Translation Quality
- Accurate translations
- Cultural appropriateness
- Consistent terminology

### 4. Completeness
- All required error scenarios covered
- Proper error context included
- User-friendly messages

## Severity Levels
- **high**: Missing critical error messages, incorrect translations, duplicate error codes
- **medium**: Unclear messages, inconsistent formatting
- **low**: Minor wording improvements, style issues

**CRITICAL**: For EVERY high severity issue, explain WHY it's critical and the potential impact.

## TOOLS AVAILABLE
You have access to MCP tools for GitLab, Jira, and Confluence that can help you:
- Fetch related error message files from the repository to check for duplicates
- Look up Jira issues to understand the context of error messages
- View Confluence documentation about error message standards
- Understand the broader error handling strategy

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


async def create_pbook_reviewer_agent():
    """
    Create pbook reviewer agent with MCP tools.
    
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


async def run_pbook_review(context: PbookContext) -> ReviewResponse:
    """
    Run pbook review with MCP tools.
    
    The agent will automatically use MCP tools if it needs additional
    context about error messages.
    
    Args:
        context: Pbook context with file information
        
    Returns:
        ReviewResponse with identified issues
    """
    print(f"  🔍 Running pbook review...")
    
    try:
        # Load conventions
        conventions_text = ""
        if (context.file_path and context.gitlab_url and context.project_path 
            and context.source_branch and context.gitlab_token):
            try:
                conventions_text = await load_conventions(
                    agent_type="pbook",
                    file_path=context.file_path,
                    module_name=None,  # Pbook files typically don't have module context
                    gitlab_url=context.gitlab_url,
                    project_path=context.project_path,
                    source_branch=context.source_branch,
                    gitlab_token=context.gitlab_token
                )
            except Exception as conv_error:
                print(f"  ⚠️  Warning: Failed to load conventions: {conv_error}")
                conventions_text = ""
        
        # Create agent with tools
        agent = await create_pbook_reviewer_agent()
        
        # Build input with context and conventions
        input_text = get_pbook_reviewer_prompt(context, conventions_text)
        
        # Run agent
        result = await agent.run(input_text)
        
        print(f"  ✅ Pbook review complete: {len(result.issues)} issues found")
        return result
        
    except Exception as e:
        print(f"  ❌ Pbook review failed: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        # Return empty response on error
        return ReviewResponse(issues=[])

