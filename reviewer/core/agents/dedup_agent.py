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

**Failure has a ladder, and every rung fails toward posting.** In production the
structured call failed in 30–63% of runs per release (mostly a provider
rejecting the forced ``tool_choice``), and on every such run only the exact-text
rule stood — which is exactly when the bursts of repeats landed. So:

1. the structured call (forced ``tool_choice``), with the gateway's retries;
2. once more as a **plain completion** asking for the same JSON, parsed
   leniently — a different route through the provider, no new gateway policy;
3. if that fails too, the **positional fallback**: drop only a candidate that
   sits within a few lines of a comment *this agent* already posted on the very
   same file *and* says the same thing — never on a human's comment, never an
   escalation.

The rule-based pre-pass (exact text, and "a human already rejected this")
runs before all of it and stands whatever the model does. The whole model side
is bounded in wall-clock so retries cannot eat the time reserved for posting,
and when the stage's budget is already gone before the model is asked (a slow
comment fetch spent it), the model is skipped and the fallback runs directly.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from core.agents.base_agent import BaseReviewAgent
from core.llm.gateway import LLMGateway
from core.prompts import get_comment_dedup_prompt
from core.prompts.dedup_prompts import get_comment_dedup_json_format
from core.utils.comment_dedup import (
    apply_drops,
    build_dedup_input,
    describe_issue,
    deterministic_duplicates,
    parse_dedup_json,
    positional_duplicates,
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
    timeout_s: Optional[float] = None,
    warnings_sink: Optional[List[dict]] = None,
    skip_model: Optional[str] = None,
) -> Tuple[List[Any], List[dict], Optional[str]]:
    """Drop the candidates that repeat an existing MR comment or each other.

    Args:
        candidates: The comments about to be posted (``ReviewIssue`` objects on
            the comment reviewer's success path, plain dicts on its fallback
            path — both shapes are read defensively throughout).
        existing: The MR's current comment threads, from
            :func:`core.utils.comment_dedup.fetch_existing_comments`. Empty
            when they could not be fetched, which reduces this to an intra-run
            check rather than skipping it.
        config: Tracing run config carrying the MR session metadata.
        timeout_s: Wall-clock bound on the model side of the stage (the
            structured call, its retries and the plain-JSON retry together), so
            a provider that keeps failing slowly cannot eat the time held back
            for posting. None or <= 0 means unbounded.
        warnings_sink: Optional list; a failed structured call that the
            plain-JSON retry recovered from appends one
            ``{"stage": "comment_dedup_retry", "message"}`` entry. Not a
            ``comment_dedup`` warning — the stage did its job — but the
            structured path's failure rate is worth watching on its own.
        skip_model: When set, the model is not asked at all — the caller's
            budget for the stage is already spent — and this text is returned
            as the error. The rule-based drops and the positional fallback
            still apply, exactly as after a model failure. Ignored when the
            model would not have been asked anyway (nothing left to judge).

    Returns:
        ``(kept, dropped, error)`` — the comments to post, one record per drop
        (``{index, issue, duplicate_of, reason}``), and ``None`` on success or a
        description of the failure when the model could not answer at all. On
        failure the rule-based drops still stand and the positional fallback
        adds the agent's own repeats; neither depends on the model.
    """
    total = len(candidates)
    if total == 0:
        return list(candidates), [], None

    drops: Dict[int, Tuple[str, str]] = deterministic_duplicates(candidates, existing)
    if drops:
        print(f"  🧮 Rule-based pre-pass matched {len(drops)} comment(s)")

    # Nothing left for the agent to judge: the pre-pass already matched every
    # candidate, or a single candidate has nothing to be a duplicate *of* (no
    # existing MR comment, no sibling). Either way the round trip would only buy
    # an answer the arithmetic already gives.
    remaining = total - len(drops)
    if remaining == 0 or (remaining <= 1 and not existing):
        kept, dropped = _finish(candidates, drops)
        return kept, dropped, None

    if skip_model:
        print(f"  ⏱️  {skip_model}")
        return _fall_back(candidates, existing, drops, skip_model)

    start_time = time.time()
    print(
        f"  🤖 Running comment dedup agent on {total} candidate(s) "
        f"against {len(existing)} existing MR thread(s)..."
    )

    failures: List[str] = []
    bound = timeout_s if timeout_s and timeout_s > 0 else None
    try:
        payload, _omitted = build_dedup_input(candidates, existing)
        result = await asyncio.wait_for(_judge(payload, config, failures), timeout=bound)
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        detail = f"comment dedup agent timed out after {elapsed:.0f}s"
        if bound is not None:
            detail += f" (COMMENT_DEDUP_TIMEOUT_S budget {bound:.0f}s)"
        if failures:
            detail += " after " + "; ".join(failures)
        print(f"  ⏱️  {detail}")
        return _fall_back(candidates, existing, drops, detail)
    except Exception as e:  # noqa: BLE001 — every failure here degrades toward posting
        elapsed = time.time() - start_time
        detail = "; ".join(failures) or describe_error(e)
        print(f"  ⚠️  Comment dedup agent unavailable after {elapsed:.2f}s: {detail}")
        return _fall_back(candidates, existing, drops, f"comment dedup agent failed: {detail}")

    if failures and warnings_sink is not None:
        warnings_sink.append({
            "stage": "comment_dedup_retry",
            "message": (
                f"{failures[0]}; recovered with a plain-JSON retry (no forced tool_choice)"
            )[:600],
        })

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
        return _fall_back(
            candidates, existing, drops, f"comment dedup response unreadable: {detail}"
        )

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


async def _judge(
    payload: str, config: Optional[RunnableConfig], failures: List[str]
) -> CommentDedupResponse:
    """Ask the model: the structured call first, then once more as plain JSON.

    Each failure is appended to ``failures`` as it happens, so the caller can
    say what went wrong even when a timeout cancelled this midway.
    """
    try:
        agent = create_comment_dedup_agent()
        result = await agent.run(payload, config=config)
        if result is None:
            # `with_structured_output` yields None when the model answers without
            # calling the schema tool — a plain refusal, prose, or a name mismatch.
            # It is not an exception, so reading `.duplicates` off it would raise
            # far from here. Observed on a real MR; it is the plain retry's cue.
            raise ValueError(
                "dedup agent returned no structured output (the model did not call "
                "the response schema)"
            )
        return result
    except Exception as e:  # noqa: BLE001 — the plain retry handles every cause
        failures.append(f"structured call: {describe_error(e)}")
        print(f"  ⚠️  Comment dedup structured call failed: {describe_error(e)}")
        maybe_print_traceback(e, indent="     ")

    print("  🔁 Retrying the dedup check once as a plain-JSON completion "
          "(no forced tool_choice)...")
    try:
        return await plain_json_dedup(payload, config=config)
    except Exception as e:
        failures.append(f"plain-JSON retry: {describe_error(e)}")
        raise


async def plain_json_dedup(
    payload: str, config: Optional[RunnableConfig] = None
) -> CommentDedupResponse:
    """The dedup question as a plain completion, parsed leniently.

    Same system prompt plus a response-format addendum, no tools bound and no
    ``tool_choice`` — nothing for a provider to reject on the call's *shape*,
    which is what the structured call's failures were about. It still goes
    through :class:`LLMGateway` (``structured=False``, since nothing is forced),
    so provider choice, transient retries and failover are the gateway's as for
    every other call. Messages are passed directly rather than through a prompt
    template, which would read the format example's braces as variables.
    """
    gateway = LLMGateway(
        lambda model: model,
        label="CommentDedupAgent plain-JSON retry",
        structured=False,
    )
    messages = [
        SystemMessage(content=get_comment_dedup_prompt() + get_comment_dedup_json_format()),
        HumanMessage(content=payload),
    ]
    message = await gateway.ainvoke(messages, config=config)
    return parse_dedup_json(_message_text(message))


def _message_text(message: Any) -> str:
    """The text of a chat answer, whether ``content`` is a string or blocks."""
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


def _fall_back(
    candidates: Sequence[Any],
    existing: Sequence[dict],
    drops: Dict[int, Tuple[str, str]],
    error: str,
) -> Tuple[List[Any], List[dict], str]:
    """The model could not answer: rule-based drops plus the positional fallback.

    The positional rule only drops a candidate against a comment *this agent*
    already posted a few lines away on the same file, about the same thing (see
    ``positional_duplicates``) — the repeat bursts production showed came from
    exactly these runs, when only the exact-text rule stood. Everything else is posted: a missed duplicate is
    noise, a dropped finding is a defect that ships.
    """
    positional = positional_duplicates(candidates, existing, drops)
    if positional:
        print(
            f"  📍 Positional fallback matched {len(positional)} repeat(s) of this "
            f"agent's own earlier comments"
        )
    print("  ℹ️  Posting everything else — a missed duplicate is noise, a dropped "
          "finding is a defect that ships")
    drops.update(positional)
    kept, dropped = _finish(candidates, drops)
    if positional:
        error = f"{error}; positional fallback dropped {len(positional)}"
    return kept, dropped, error


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
