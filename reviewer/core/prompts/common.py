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


# Explains the line-number gutter that format_file_content_section prepends to
# every line of the reviewed file.
LINE_NUMBER_GUTTER_NOTE = (
    "Every line below is prefixed with `<line number>|`. That gutter is NOT part "
    "of the file: never copy it into a `suggestion` and never count it as source "
    "columns. Report the gutter number as an issue's `line` — it is the same "
    "numbering as the new side of the Git Diff (its `+` and context lines), which "
    "is what the merge request comments are anchored to."
)


def format_file_content_section(content: Optional[str]) -> str:
    """Render the reviewed file as a fenced block with a 1-based line-number gutter.

    GitLab returns the file as raw text with no line numbers, yet every reviewer
    is asked to report an exact `line` per issue — leaving the model to count
    rows down from a diff hunk header. A miscount is not a cosmetic error: the
    in-diff gate in main.py resolves each issue's (file_name, line) against the
    diff hunks and *drops* the ones that miss, so the finding is lost. Numbering
    the lines turns that counting task into a lookup.

    The numbers are those of the file on the MR source branch, i.e. the diff's
    new side, which is the side GitLab positions inline comments against.
    """
    if not content:
        return "(file content unavailable)"

    lines = content.splitlines()
    if not lines:
        return "(file is empty)"

    width = len(str(len(lines)))
    numbered = "\n".join(
        f"{number:>{width}}|{line}" for number, line in enumerate(lines, start=1)
    )
    return f"{LINE_NUMBER_GUTTER_NOTE}\n```\n{numbered}\n```"


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
