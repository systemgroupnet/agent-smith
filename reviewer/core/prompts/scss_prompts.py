"""
SCSS Agent System Prompts

This module contains system prompts for the SCSS/CSS review agent.
Reviews SCSS/CSS files for a web frontend.
"""

from typing import TYPE_CHECKING

from core.prompts.common import (
    SEVERITY_RUBRIC,
    format_file_content_section,
    format_mr_context_section,
    NO_COMPILE_ERRORS_RULE,
    SUGGESTION_FIELD_RULE,
    SYMBOL_FORMATTING_RULE,
    PLAIN_LANGUAGE_RULE,
    EMPTY_FINDING_RULE,
)

if TYPE_CHECKING:
    from core.agents.review_contexts import ReviewPromptContext


def get_scss_reviewer_prompt(context: "ReviewPromptContext", additional_content: str = "") -> str:
    """Get SCSS reviewer system prompt with tool usage instructions."""
    
    # Build additional context information
    context_info = []
    if context.file_path:
        context_info.append(f"File Path: {context.file_path}")
    if context.jira_task_number:
        context_info.append(f"Jira Task: {context.jira_task_number}")
    
    context_section = "\n".join(context_info) if context_info else "No additional context"
    
    return f"""You are an expert SCSS/CSS reviewer for a web frontend.

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

{SEVERITY_RUBRIC}

## SUGGESTION FIELD FORMAT
{SUGGESTION_FIELD_RULE}

{SYMBOL_FORMATTING_RULE}

{PLAIN_LANGUAGE_RULE}

{EMPTY_FINDING_RULE}

## TOOLS AVAILABLE
You review from a real checkout of this merge request's source branch: `read_file` reads any file, `grep` and `glob` search the whole repository, `ls` looks around. Use them to resolve what the diff references but does not show — a definition, a caller, a sibling implementation — instead of assuming. There are no repository, Jira or Confluence tools; the merge request details and its Jira ticket, when it has one, are already in the Merge Request Context section below.

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
