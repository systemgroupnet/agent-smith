"""Comment Deduplication Agent.

The last stage before posting, and the only one that reads the merge request's
existing comments. It decides nothing about whether a finding is *good* — every
other filter has already run — only whether it has **already been said**, either
by an earlier run of this agent (the review is incremental and stateless between
runs) or by a human reviewer.

The agent returns **ids to drop, never comments**. That is the whole design: the
comment reviewer synthesizes its output, and everything downstream of it —
``restore_issue_severities``, the posting loop's position safety net — exists to
repair what that synthesis breaks. A second synthesizing stage would need the
same repairs and would find new ways to lose a severity or rewrite a path. An id
cannot corrupt text, severity or position; the caller filters its own
authoritative list.

Failure is a no-op, not a fallback: :func:`deduplicate_comments` returns the
deterministic pre-pass result and an error string, and the caller posts
everything else. There is nothing sensible for an LLM-less "semantic" dedup to
do, and posting a duplicate is the status quo this stage improves on, whereas
dropping a real finding is a defect that ships.
"""

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.runnables import RunnableConfig

from core.agents.base_agent import BaseReviewAgent
from core.prompts import get_comment_dedup_prompt
from core.utils.comment_dedup import (
    apply_drops,
    build_dedup_input,
    describe_issue,
    deterministic_duplicates,
    resolve_drops,
    target_candidate,
)
from core.utils.llm_retry import describe_error, maybe_print_traceback
from dto.general import CommentDedupResponse


class CommentDedupAgent(BaseReviewAgent[CommentDedupResponse]):
    """Agent that names the candidate comments already said elsewhere."""

    def __init__(self, model_name: str = None, temperature: float = 0.0):
        super().__init__(
            model_name=model_name,
            temperature=temperature,
            output_model=CommentDedupResponse,
        )

    def get_system_prompt(self) -> str:
        return get_comment_dedup_prompt()


def create_comment_dedup_agent(
    model_name: str = None, temperature: float = 0.0
) -> CommentDedupAgent:
    """Factory for the comment deduplication agent."""
    return CommentDedupAgent(model_name=model_name, temperature=temperature)


async def deduplicate_comments(
    candidates: Sequence[Any],
    existing: Sequence[dict],
    config: Optional[RunnableConfig] = None,
) -> Tuple[List[Any], List[dict], Optional[str]]:
    """Drop the candidates that repeat an existing MR comment or each other.

    Args:
        candidates: The comments about to be posted (``ReviewIssue`` objects on
            the comment reviewer's success path, plain dicts on its fallback
            path — both shapes are read defensively throughout).
        existing: The MR's current comments, from
            :func:`core.utils.comment_dedup.collect_existing_comments`. Empty
            when the notes could not be fetched, which reduces this to an
            intra-run check rather than skipping it.
        config: Tracing run config carrying the MR session metadata.

    Returns:
        ``(kept, dropped, error)`` — the comments to post, one record per drop
        (``{index, issue, duplicate_of, reason}``), and ``None`` on success or a
        description of the failure when the agent could not run. On failure the
        deterministic drops still stand: they are textual identity matches, not
        judgements, and they do not depend on the model.
    """
    total = len(candidates)
    if total == 0:
        return list(candidates), [], None

    drops: Dict[int, Tuple[str, str]] = deterministic_duplicates(candidates, existing)
    if drops:
        print(f"  🧮 Exact-duplicate pre-pass matched {len(drops)} comment(s)")

    # Nothing left for the agent to judge: the pre-pass already matched every
    # candidate, or a single candidate has nothing to be a duplicate *of* (no
    # existing MR comment, no sibling). Either way the round trip would only buy
    # an answer the arithmetic already gives.
    remaining = total - len(drops)
    if remaining == 0 or (remaining <= 1 and not existing):
        kept, dropped = _finish(candidates, drops)
        return kept, dropped, None

    start_time = time.time()
    print(
        f"  🤖 Running comment dedup agent on {total} candidate(s) "
        f"against {len(existing)} existing MR comment(s)..."
    )

    try:
        agent = create_comment_dedup_agent()
        payload, _omitted = build_dedup_input(candidates, existing)
        result = await agent.run(payload, config=config)
        if result is None:
            # `with_structured_output` yields None when the model answers without
            # calling the schema tool — a plain refusal, prose, or a name mismatch.
            # It is not an exception, so it lands here rather than below, and
            # reading `.duplicates` off it would raise *outside* this try and take
            # the whole run down at the last stage before posting. Observed on a
            # real MR. Treated as "no duplicates found", which is this stage's
            # designed failure direction.
            raise ValueError(
                "dedup agent returned no structured output (the model did not call "
                "the response schema)"
            )
    except Exception as e:
        elapsed = time.time() - start_time
        detail = describe_error(e)
        print(f"  ⚠️  Comment dedup agent failed after {elapsed:.2f}s: {detail}")
        maybe_print_traceback(e, indent="     ")
        print("  ℹ️  Posting every candidate the exact-match pre-pass kept — a missed "
              "duplicate is noise, a dropped finding is a defect that ships")
        kept, dropped = _finish(candidates, drops)
        return kept, dropped, f"comment dedup agent failed: {detail}"

    # Reading the response is inside its own guard for the same reason: everything
    # between the model answering and the drop list being applied must fail toward
    # posting, never toward an exception at the last stage before the comments go
    # out. `_read_model_drops` already tolerates junk *entries*; this covers a
    # response object that is the wrong shape entirely.
    try:
        model_drops = _read_model_drops(result, candidates, drops, len(existing))
        reasoning = getattr(result, "reasoning", "") or ""
    except Exception as e:
        detail = describe_error(e)
        print(f"  ⚠️  Could not read the dedup agent's response: {detail}")
        kept, dropped = _finish(candidates, drops)
        return kept, dropped, f"comment dedup response unreadable: {detail}"

    elapsed = time.time() - start_time
    print(
        f"  ✅ Dedup agent flagged {len(model_drops)} duplicate(s) "
        f"(⏱️  {elapsed:.2f}s)"
    )
    if reasoning:
        print(f"  💡 Reasoning: {reasoning}")

    drops.update(model_drops)
    kept, dropped = _finish(candidates, drops)
    return kept, dropped, None


def _finish(
    candidates: Sequence[Any], drops: Dict[int, Tuple[str, str]]
) -> Tuple[List[Any], List[dict]]:
    """Resolve drop chains, then split the list. Never returns an empty group.

    ``resolve_drops`` is what guarantees a group of mutual duplicates leaves one
    comment posted; a rescued candidate is simply no longer dropped.
    """
    targets = {
        index: target_candidate(duplicate_of, len(candidates))
        for index, (duplicate_of, _reason) in drops.items()
    }
    honoured = resolve_drops(targets)
    rescued = set(drops) - honoured
    for index in sorted(rescued):
        print(
            f"  ↩️  Keeping {describe_issue(candidates[index])} — every comment it was "
            f"said to duplicate is itself being dropped"
        )
    final = {index: drops[index] for index in sorted(honoured)}
    return apply_drops(candidates, final)


def _read_model_drops(
    result: CommentDedupResponse,
    candidates: Sequence[Any],
    already: Dict[int, Tuple[str, str]],
    existing_count: int,
) -> Dict[int, Tuple[str, str]]:
    """Validate the agent's drop list against the authoritative candidate list.

    Everything unusable is discarded rather than guessed at: an id outside the
    list, the pydantic ``-1`` default from an unreadable value, a candidate
    named twice, or a self-reference. The result of discarding is that the
    comment gets posted.

    One whole-response guard: a model that drops **every** candidate when the MR
    has no existing comments cannot be right — with nothing already posted,
    every duplicate must be between two candidates, and that always leaves one
    survivor. Such a response is ignored entirely rather than picked apart.
    """
    total = len(candidates)
    drops: Dict[int, Tuple[str, str]] = {}
    for entry in result.duplicates:
        index = entry.candidate_id
        if not (0 <= index < total):
            print(f"  ⚠️  Dedup agent named candidate {index}, which is not in the list — ignored")
            continue
        if index in drops or index in already:
            continue
        target = target_candidate(entry.duplicate_of, total)
        if target == index:
            print(f"  ⚠️  Dedup agent said candidate {index} duplicates itself — ignored")
            continue
        drops[index] = (
            entry.duplicate_of or "an existing comment",
            entry.reason or "flagged as a duplicate by the dedup agent",
        )

    if drops and existing_count == 0 and len(drops) + len(already) >= total:
        # Impossible by construction: with nothing already on the MR, every
        # duplicate is between two candidates, and that always leaves one
        # posted. A response that empties the list is not a strict answer to a
        # different question, it is a broken one — discard it whole rather than
        # keep the arbitrary subset the chain resolver would rescue.
        print(
            f"  ⚠️  Dedup agent marked all {total} candidate(s) as duplicates but the MR "
            f"has no existing comments — ignoring the whole response"
        )
        return {}
    return drops
