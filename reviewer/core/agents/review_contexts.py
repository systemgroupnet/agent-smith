"""The one context shape every reviewer prompt is rendered from.

The eight per-type reviewers used to each own a near-identical pydantic context
(``SqlContext``, ``ServiceContext``, …) differing only in which of the same
twelve fields they declared. That mattered when each reviewer was a separate
module with its own agent and its own call site. It stopped mattering when the
prompts became the only consumer: every getter in ``core/prompts/`` reads
attributes off whatever it is handed — ``content``, ``diff``, ``file_path``,
``mr_context``, ``source_branch``, ``package_name``, ``module_name``,
``jira_task_number`` — and nothing checks the type.

So there is one model with the union of those fields, and eight identical
constructions collapse into one. The alternative was moving eight classes into a
new module purely so the deep-agent path could import them, which is the shape of
a refactor that never finishes.

**Content is normally empty here; the diff is not.** A reviewer reads the file
itself, from the checkout, so the prompt carries instructions and merge-request
context but not the code. ``format_file_content_section("")`` renders
"(file content unavailable)" and the deep-mode addendum immediately tells the
model where the code actually is. The diff *is* filled in (capped): the checkout
cannot say which lines the merge request changed, and a reviewer without it
reviews code that was already there.
"""

from typing import Optional

from pydantic import BaseModel


class ReviewPromptContext(BaseModel):
    """Everything any reviewer prompt might interpolate.

    Every field is optional or defaulted: a prompt asks for what it needs and
    ignores the rest, and a missing value renders as a placeholder rather than
    raising. Field names match the originals exactly, so the prompt getters are
    untouched by this consolidation.
    """

    # The reviewed file. Empty in practice — the reviewer reads it from disk.
    content: str = ""
    # The file's unified diff for this run, capped. The one input not on disk.
    diff: str = ""
    file_name: str = ""
    file_path: Optional[str] = None

    # Go package/module, used by the service and dataview prompts.
    package_name: Optional[str] = None
    module_name: Optional[str] = None

    # Merge-request level context, shared by every reviewer.
    mr_context: Optional[str] = None
    source_branch: Optional[str] = None
    jira_task_number: Optional[str] = None

    # Kept because the prompts' `.aid` guidance mentions them; no prompt reads
    # them directly any more, and nothing in this path makes GitLab calls from a
    # prompt context.
    project_path: Optional[str] = None
    gitlab_url: Optional[str] = None
    gitlab_token: Optional[str] = None
