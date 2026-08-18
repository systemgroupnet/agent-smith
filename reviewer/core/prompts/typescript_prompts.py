"""
TypeScript Agent System Prompts

This module contains system prompts for the TypeScript review agent.
Reviews TypeScript files in the application frontend.
"""

from typing import TYPE_CHECKING

from core.prompts.common import (
    SEVERITY_RUBRIC,
    format_file_content_section,
    format_mr_context_section,
    format_code_index_guidance,
    NO_COMPILE_ERRORS_RULE,
    SUGGESTION_FIELD_RULE,
    SYMBOL_FORMATTING_RULE,
)

if TYPE_CHECKING:
    from core.agents.review_contexts import ReviewPromptContext


def get_typescript_reviewer_prompt(context: "ReviewPromptContext", additional_content: str = "") -> str:
    """Get TypeScript reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert TypeScript reviewer for the application frontend.

{NO_COMPILE_ERRORS_RULE}

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

{SEVERITY_RUBRIC}

## SUGGESTION FIELD FORMAT
{SUGGESTION_FIELD_RULE}

{SYMBOL_FORMATTING_RULE}

## TOOLS AVAILABLE
You have access to MCP tools for GitLab, Jira, Confluence, and the cocoindex code index that can help you:
- Fetch related TypeScript files from the repository (components, services, models)
- Look up Jira issues to understand feature requirements
- View Confluence documentation about frontend architecture and standards
- Understand the broader context of the component/service

{format_code_index_guidance(context.source_branch)}

Use these tools if you need additional context to perform your review, but avoid unnecessary tool calls.

=== Additional Instructions ===
{additional_content if additional_content else "No additional instructions defined for this agent."}

=== Merge Request Context ===
{format_mr_context_section(context.mr_context)}

=== Context Information ===
{context_section}

=== File Content ===
{format_file_content_section(context.content)}

=== Git Diff ===
```
{context.diff}
```"""
