"""Comment filtering for the review pipeline.

This module also hosted ``review_file_with_workflow``, the per-file entry point
into the LangGraph workflow. Reviewing a file is the deep-agent path's job now
(``core/agents/deep_reviewer.py``) and the workflow is gone, so what remains is
the comment reviewer's call site.
"""

import asyncio
import json
import time
from typing import Optional, List, Tuple
from langchain_core.runnables import RunnableConfig
from core.utils.comment_formatter import fallback_comment_selection
from core.utils.review_verbosity import DEFAULT as DEFAULT_VERBOSITY, Verbosity
from core.utils.llm_retry import describe_error, maybe_print_traceback


async def review_comments_with_agent(
    issues: list,
    config: Optional[RunnableConfig] = None,
    verbosity: Optional[Verbosity] = None,
) -> Tuple[list, list, Optional[str]]:
    """
    Filter and rank the review comments using the comment reviewer agent.

    Args:
        issues: List of ReviewIssue objects/dicts that passed the in-diff gate
        config: Optional tracing run config carrying the MR session metadata
        verbosity: The MR's verbosity level, which sets the comment budget and is
            also the cap the deterministic fallback honors. Defaults to terse.

    Returns:
        ``(selected, severity_corrections, error)`` — the comments to post, the
        severity demotions the agent asked for (see
        :func:`core.utils.comment_formatter.apply_severity_corrections`), and
        ``None`` on success or a description of the failure.

    The error is **returned, not swallowed**. This stage used to catch everything,
    print a warning and pass every issue through, which recorded nothing: the run
    event showed a plausible ``comments_selected`` and the only trace was a line in
    the CI log. So a failure here was invisible in analytics — the 86 guardrail
    errors that motivated this all landed under ``stage="file_review"`` and none
    under the comment reviewer, even while it was failing.

    A failure is degraded, not fatal: the caller records it and posts a bounded,
    compile-time-purged selection (:func:`fallback_comment_selection`) rather than
    losing every finding the run produced.
    """
    if not issues:
        return [], [], None

    from core.agents.comment_reviewer_agent import create_comment_reviewer_agent

    v = verbosity or DEFAULT_VERBOSITY
    start_time = time.time()
    print(f"  🤖 Running comment reviewer agent on {len(issues)} issue(s) at verbosity {v.describe()}...")

    try:
        # Create the agent
        agent = create_comment_reviewer_agent(verbosity=v)

        # Convert issues to proper format if needed
        issues_list = []
        for issue in issues:
            if isinstance(issue, dict):
                issues_list.append(issue)
            else:
                issues_list.append(issue.model_dump())

        # Create input as JSON string
        input_text = json.dumps({"issues": issues_list}, indent=2)

        # Run the agent
        result = await agent.run(input_text, config=config)

        elapsed_time = time.time() - start_time
        print(f"  ✅ Agent selected {len(result.selected_issues)} critical issues (⏱️  {elapsed_time:.2f}s)")
        print(f"  📊 Filtered out {result.filtered_count} less critical issues")
        print(f"  💡 Reasoning: {result.reasoning}")

        corrections = list(getattr(result, "severity_corrections", None) or [])
        if corrections:
            print(f"  ⚖️  Agent flagged {len(corrections)} severity mislabel(s)")
        return result.selected_issues, corrections, None

    except Exception as e:
        elapsed_time = time.time() - start_time
        detail = describe_error(e)
        print(f"  ⚠️  Error running comment reviewer agent after {elapsed_time:.2f}s: {detail}")
        maybe_print_traceback(e, indent="     ")

        selected, purged, dropped = fallback_comment_selection(issues, v.max_comments)
        print(
            f"  ℹ️  Falling back to a deterministic selection: posting {len(selected)} "
            f"of {len(issues)} — purged {len(purged)} compile-time claim(s), "
            f"cut {len(dropped)} over the {v.max_comments}-comment cap ({v.name})"
        )
        for issue in dropped:
            problem = issue.get("problem", "") if isinstance(issue, dict) else getattr(issue, "problem", "")
            print(f"       ↳ not posted (over cap): {str(problem)[:100]}")
        return selected, [], f"comment reviewer agent failed: {detail}"
