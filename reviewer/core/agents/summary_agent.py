"""Review-Summary Agent — the orientation note a human reviewer reads first.

A whole-MR pass (not a per-file reviewer, so it does not go through
``core/workflow/graph.py``) that describes what the merge request *does*:
the features added, the bugs fixed, and where a human should concentrate.
It never reports defects — that is the reviewers' job, and their findings are
validated against the diff by the in-diff gate, while this agent's output is not.

Unlike the effort estimator, which is deliberately metadata-only, this one sees
real diffs (budgeted — see ``core/utils/summary_input.py``) because describing a
change requires reading it.

It has **no tools**. It used to be a tool-enabled agent over a semantic code index,
so it could look up a caller or a type the diff referenced but did not show; that
index is gone, and its only other candidate — the GitLab MCP — would just re-fetch
files already inlined in the input. What is left is one structured call over the
payload it is handed, which also removes an agent loop, its iteration budget and
its salvage path from a pass whose whole job is to describe what it was given.

Non-fatal throughout: any failure returns None and the review proceeds with no
summary comment.
"""

import time
from typing import Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from core.agents.base_agent import StructuredOutputAgent
from core.prompts import get_review_summary_prompt
from core.utils.llm_retry import describe_error, maybe_print_traceback
from core.utils.summary_input import build_summary_input
from dto.general import ReviewSummary


class SummaryContext(BaseModel):
    """Everything the summary pass needs.

    ``source_branch`` is carried for the callers that build this context and for
    the trace, not for a lookup: with no tools there is no ref to name.
    """

    mr_data: dict
    changes: list
    jira_body: Optional[str] = ""
    source_branch: Optional[str] = None


async def generate_review_summary(
    context: SummaryContext, config: Optional[RunnableConfig] = None
) -> Optional[ReviewSummary]:
    """Summarise the MR for a human reviewer. Returns None on failure (non-fatal)."""
    if not context.changes:
        return None

    start = time.time()
    print("  📝 Generating MR review summary...")
    try:
        agent = StructuredOutputAgent(
            system_prompt=get_review_summary_prompt(),
            output_model=ReviewSummary,
            temperature=0.3,
        )
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
