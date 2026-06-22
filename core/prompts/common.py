"""
Shared prompt helpers used across all reviewer prompts.
"""

from typing import Optional


# Guidance shared across all reviewer prompts: which MCP to use depending on
# whether a file was changed in this merge request or is a pre-existing file.
AIAND_SOURCE_GUIDANCE = (
    "For files that have changes in this merge request, use the GitLab MCP "
    "tools (so you read the new version of the file). For old/unchanged files "
    "and existing code in the rest of the codebase, use the AIAND Source MCP "
    "(a semantic code index) to look them up for context."
)


def format_mr_context_section(mr_context: Optional[str]) -> str:
    """Render the shared MR-level context block for a reviewer prompt.

    `mr_context` is a preformatted block built once per MR by
    core.utils.mr_context.build_mr_context and threaded through the workflow
    state into every agent. Returns a short placeholder when no MR context is
    available (e.g. classifier-only or standalone runs).
    """
    if mr_context and mr_context.strip():
        return mr_context.strip()
    return "No MR-level context available."
