"""
SCSS Agent System Prompts

This module contains system prompts for the SCSS/CSS review agent.
Reviews SCSS/CSS files for the application frontend.
"""

from typing import TYPE_CHECKING

from core.prompts.common import (
    format_file_content_section,
    format_mr_context_section,
    format_code_index_guidance,
    NO_COMPILE_ERRORS_RULE,
    SUGGESTION_FIELD_RULE,
    SYMBOL_FORMATTING_RULE,
)

if TYPE_CHECKING:
    from core.agents.scss_agent import ScssContext


def get_scss_reviewer_prompt(context: "ScssContext", additional_content: str = "") -> str:
    """Get SCSS reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert SCSS/CSS reviewer for the application frontend.

{NO_COMPILE_ERRORS_RULE}

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

## SUGGESTION FIELD FORMAT
{SUGGESTION_FIELD_RULE}

{SYMBOL_FORMATTING_RULE}

## TOOLS AVAILABLE
You have access to MCP tools for GitLab, Jira, Confluence, and the cocoindex code index that can help you:
- Fetch related SCSS files from the repository to check for consistency
- Look up Jira issues to understand styling requirements
- View Confluence documentation about design system and CSS standards
- Fetch related HTML templates to understand styling context
- Understand the design requirements

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
