"""Review-Summary Agent — the orientation note a human reviewer reads first.

A whole-MR pass (not a per-file reviewer, so it does not go through
``core/workflow/graph.py``) that describes what the merge request *does*:
the features added, the bugs fixed, and where a human should concentrate.
It never reports defects — that is the reviewers' job, and their findings are
validated against the diff by the in-diff gate, while this agent's output is not.

Unlike the effort estimator, which is deliberately metadata-only, this one sees
real diffs (budgeted — see ``core/utils/summary_input.py``) because describing a
change requires reading it. It also gets the **cocoindex** code index with the MR
source branch, the same tool and the same branch discipline as every file
reviewer, so it can look up an unfamiliar caller or type in the surrounding
codebase instead of guessing from the diff alone.

Non-fatal throughout: any failure returns None and the review proceeds with no
summary comment.
"""

import time
from typing import Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from core.agents.tool_agent import create_tool_agent
from core.prompts import get_review_summary_prompt
from core.prompts.common import format_code_index_guidance
from core.utils.llm_retry import describe_error, maybe_print_traceback
from core.utils.summary_input import build_summary_input
from dto.general import ReviewSummary


class SummaryContext(BaseModel):
    """Everything the summary pass needs. ``source_branch`` is load-bearing: it
    is what makes a code-index lookup answer from the MR's own branch instead of
    the server's checked-out base, i.e. the pre-MR state of the code."""

    mr_data: dict
    changes: list
    jira_body: Optional[str] = ""
    source_branch: Optional[str] = None


async def _create_summary_agent(source_branch: Optional[str]):
    """Tool-enabled agent over the code index, emitting a ``ReviewSummary``.

    Only the ``cocoindex`` category: the summary's subject matter is the diff it
    was handed, and the code index is there purely to resolve what surrounds it.
    The GitLab MCP would only re-fetch files already inlined in the input, and
    the Jira ticket is already in the prompt via the shared MR context.
    """
    system_prompt = (
        f"{get_review_summary_prompt()}\n\n"
        f"{format_code_index_guidance(source_branch)}\n\n"
        "Use the code index sparingly and only to *understand* something the diff "
        "references but does not show — an existing function the new code calls, a "
        "type it implements, the convention a new file follows. A few targeted "
        "lookups, not a survey. If a lookup fails, continue without it; a summary "
        "built from the diff alone is far better than no summary."
    )
    return await create_tool_agent(
        system_prompt=system_prompt,
        tools=["cocoindex"],
        output_model=ReviewSummary,
        temperature=0.3,
        max_iterations=8,
        verbose=False,
    )


async def generate_review_summary(
    context: SummaryContext, config: Optional[RunnableConfig] = None
) -> Optional[ReviewSummary]:
    """Summarise the MR for a human reviewer. Returns None on failure (non-fatal)."""
    if not context.changes:
        return None

    start = time.time()
    print("  📝 Generating MR review summary...")
    try:
        agent = await _create_summary_agent(context.source_branch)
        payload = build_summary_input(
            mr_data=context.mr_data,
            changes=context.changes,
            jira_body=context.jira_body or "",
        )
        result = await agent.run(payload, config=config)

        if result is None or not isinstance(result, ReviewSummary) or result.is_empty():
            print("  ⚠️  Review summary came back empty; skipping the summary comment")
            return None

        print(
            f"  ✅ Review summary ready: {len(result.key_changes)} change(s), "
            f"{len(result.review_focus)} focus point(s) (⏱️  {time.time() - start:.2f}s)"
        )
        return result
    except Exception as e:
        # Non-fatal by design, like the effort estimate: a missing summary must
        # never cost the run its actual review findings.
        print(f"  ⚠️  Review summary failed after {time.time() - start:.2f}s (non-fatal): {describe_error(e)}")
        maybe_print_traceback(e, indent="     ")
        return None
