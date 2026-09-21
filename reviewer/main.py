#!/usr/bin/env python3
"""
CodeReviewAgent - Main Entry Point

This script processes GitLab merge requests, reviews changed files,
and posts review comments back to the MR.

Usage:
    python main.py --gitlab-mr "https://gitlab.example.com/project/group/-/merge_requests/123"
"""

import argparse
import asyncio
import os
import sys
import time
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Import MCP Toolkit Manager
from core.MCP.toolkit_manager import initialize_mcp_toolkits, close_mcp_toolkits

# Import utility modules
from core.utils.url_parser import parse_gitlab_mr_url, should_exclude_file
from core.utils.gitlab_helpers import (
    get_gitlab_mr_info,
    get_gitlab_mr_changes,
    get_file_content,
    post_gitlab_comment,
    post_gitlab_note,
    upsert_summary_in_description,
    strip_summary_block,
    add_note_award_emojis,
    build_diff_position_map,
    resolve_inline_position,
    get_gitlab_compare,
    get_review_marker,
    upsert_review_marker,
    has_review_summary,
    get_mr_notes,
    add_mr_label,
    SCORE_REACTION_EMOJIS,
    SUMMARY_MARKER_TOKEN,
)
from core.agents.file_reviewer import review_comments_with_agent
from core.agents.dedup_agent import deduplicate_comments
from core.agents.deep_reviewer import (
    ReviewPassResult,
    go_files,
    review_changed_files,
    route_file,
)
from core.agents.route_classifier import classify_sql_involvement
from core.utils.comment_dedup import collect_existing_comments, describe_issue
from core.utils.repo_clone import clone_source_branch
from core.agents.effort_agent import estimate_review_effort
from core.agents.summary_agent import SummaryContext, generate_review_summary
from core.utils.comment_formatter import (
    format_single_issue,
    comment_body_has_text,
    format_review_summary,
    format_summary_description,
    extract_issues_for_review,
    get_severity,
    restore_issue_severities,
    restore_issue_problems,
    apply_severity_corrections,
)
from core.utils.review_verbosity import (
    all_labels,
    cap_comments,
    effort_label,
    has_effort_label,
    parse_verbosity,
)
from core.utils.mr_context import build_mr_context, fetch_jira_ticket_body
from core.utils.file_classifier import StaticFileClassifier
from core.config.tracing import build_thread_id, build_run_config
from core.config.langfuse_config import get_langfuse_handler, flush_langfuse
from core.config.langchain_config import (
    fallback_configured,
    get_default_model,
    get_fallback_model,
    int_env,
)
from core.llm.gateway import FALLBACK, LLMGateway
from core.utils.llm_retry import is_provider_hard_block, describe_error, parse_llm_error
from core.analytics import send_run_analytics
from dto.analytics import RunMetrics, record_error, record_warning


async def _post_review_summary(
    metrics: RunMetrics,
    summary,
    effort,
    gitlab_url: str,
    project_path: str,
    mr_id: str,
    gitlab_token: str,
) -> None:
    """Post the MR's review-summary comment, seed its rating reactions, and record it.

    Posted as a **plain note**, not as the unpositioned discussion a general
    finding gets. A discussion is a *resolvable thread*, and the summary is the
    one comment with nothing to resolve — it reports no defect and asks for no
    change, so its "Resolve thread" button is pure noise, and on a project that
    requires all threads resolved before merging it is an outright obstacle: the
    author has to dismiss the orientation note to merge. The findings keep their
    threads, where resolving actually means something.

    The cost is reply capture. GitLab's note API returns no ``discussion_id``, so
    ``metrics.posted_summary["discussion_id"]`` is None and the collector cannot
    match a human's answer to this comment (``comment_replies.parent_kind ==
    "summary"`` now only ever comes from rows written before this change). That is
    inherent rather than a regression: GitLab offers no thread reply on a plain
    note in the first place. Ratings are unaffected — the ``:one:``..``:five:``
    reactions and ``summary_scores`` key on the note id, which a plain note has.

    A **shorter** rendering also goes into the MR description
    (``REVIEW_SUMMARY_IN_DESCRIPTION``), which is the closest thing GitLab has to
    pinning: there is no pinned flag on a note, so the description — the one
    region that always renders above every comment — is the only place a summary
    is guaranteed to be seen without scrolling. It is *not* the same text: the
    description states what the MR **is** (``format_summary_description``: the
    overview prose plus an attribution line), while the reviewer-facing parts —
    what changed, where to focus, the effort estimate — stay here in the comment,
    which remains canonical, keeps the full text, and is the artifact people
    rate. The description copy carries no reactions and is not reported to
    analytics. Written *first* and independently of the note, so a failed note
    post still leaves the MR described, and a failed write still leaves a
    rateable comment.

    The body carries a hidden ``SUMMARY_MARKER_TOKEN`` comment; ``has_review_summary``
    looks for exactly that, so a re-run finds this note and never posts a second
    (it lists plain notes too — the review marker is one).

    Non-fatal end to end: a failure here records a warning and leaves the review
    untouched.
    """
    summary_text = format_review_summary(summary, effort=effort)

    # The description gets a *different*, shorter rendering than the comment —
    # the overview prose alone (see format_summary_description). The description
    # says what the MR is; the comment says how to review it, and only the second
    # is a briefing for someone else.
    mirror_enabled = os.getenv(
        "REVIEW_SUMMARY_IN_DESCRIPTION", "true"
    ).strip().lower() not in ("false", "0", "no", "off", "")
    description_text = format_summary_description(summary) if mirror_enabled else ""
    if description_text:
        mirror_error = await upsert_summary_in_description(
            gitlab_url, project_path, mr_id, gitlab_token, description_text
        )
        if mirror_error is None:
            print("  📌 Added the MR description summary")
        else:
            print(f"  ⚠️  Could not add the summary to the MR description (non-fatal): {mirror_error}")
            record_warning(
                metrics,
                "review_summary_description",
                f"failed to add the summary to the MR description: {mirror_error}",
            )
    elif mirror_enabled:
        # No overview to state — writing an empty block with a signature under it
        # is worse than writing nothing. The comment still gets posted below.
        print("  ℹ️  Summary has no overview; leaving the MR description untouched")

    body = f"<!-- {SUMMARY_MARKER_TOKEN} -->\n{summary_text}"
    posted, error = await post_gitlab_note(
        gitlab_url, project_path, mr_id, gitlab_token, body
    )
    if error is not None or not posted:
        print(f"  ⚠️  Could not post the review summary (non-fatal): {error}")
        record_warning(metrics, "review_summary", f"failed to post the review summary: {error}")
        return

    metrics.summary_posted = True
    print("  ✅ Posted review summary")

    note_id = posted.get("note_id")
    if note_id is None:
        # Posted, but with no id to key reactions or scores to — the comment is on
        # the MR and useful to a human, it just can't be rated. Say so.
        record_warning(
            metrics, "review_summary", "review summary posted but returned no note id; not rated"
        )
        return

    # Deliberately NOT appended to metrics.posted_comments: a summary is a
    # different artifact from a finding, and their ratings must never end up in
    # the same average. Reported on its own so the collector can store it (and
    # its scores) in dedicated tables, leaving every existing findings query
    # correct without a filter someone has to remember to add.
    metrics.posted_summary = {
        "note_id": note_id,
        "discussion_id": posted.get("discussion_id"),
        "model": LLMGateway.effective_model(),
    }

    reactions_enabled = os.getenv(
        "COMMENT_REACTIONS_ENABLED", "true"
    ).strip().lower() not in ("false", "0", "no", "off", "")
    if reactions_enabled:
        failed = await add_note_award_emojis(
            gitlab_url, project_path, mr_id, gitlab_token, note_id
        )
        if failed:
            record_warning(
                metrics,
                "comment_reaction",
                f"failed to seed rating reaction(s) {', '.join(':' + n + ':' for n in failed)} "
                f"on the review summary (note {note_id})",
            )


#: Seconds of the overall budget held back from the deep pass for everything that
#: has to happen after it: the in-diff gate, the comment reviewer, the dedup agent
#: (which makes its own LLM call and a GitLab notes fetch) and posting each comment
#: with its retry backoff. A deep pass that spends the whole window leaves findings
#: nobody ever sees, which is strictly worse than reviewing fewer files.
DEEP_REVIEW_POST_RESERVE_S = 240


def _review_concurrency() -> int:
    """Reviewer-file pairs in flight at once. `FILE_BATCH_SIZE` governs the review
    pass again: with no orchestrator, concurrency is ours rather than a property of
    whether the model chose to emit parallel tool calls. The variable's name is
    historical — the pass is a worker pool, so this is a ceiling held continuously
    rather than the size of a batch everything waits on.
    """
    return max(1, int_env("FILE_BATCH_SIZE", 5))


async def _run_review_pass(
    metrics: RunMetrics,
    changes_to_review: list,
    exclusion_patterns: list,
    gitlab_url: str,
    project_path: str,
    gitlab_token: str,
    source_branch: str,
    target_branch: str,
    mr_id: str,
    head_sha: str,
    mr_context: str,
    thread_id: str,
    deadline: float = None,
) -> tuple:
    """Review the merge request: check out the branch, then run the reviewers.

    Returns ``(result, clone)`` — a :class:`ReviewPassResult` and the checkout for
    the caller to clean up. ``(None, None)`` when there is no checkout: every
    reviewer reads it, so there is nothing to review without one, and that is
    fatal rather than degraded — a run that silently reviewed nothing would
    report success and advance the marker.
    """
    clone = clone_source_branch(
        gitlab_url=gitlab_url,
        project_path=project_path,
        token=gitlab_token,
        source_branch=source_branch,
        target_branch=target_branch,
        mr_iid=mr_id,
        head_sha=head_sha,
    )
    if clone is None:
        # There is no second review path any more, and every reviewer reads the
        # checkout. Without one there is nothing to review, so this is fatal rather
        # than degraded — a run that silently reviewed nothing would report success
        # and advance the marker, which is the worst available outcome.
        record_error(
            metrics,
            "review_clone",
            "could not check out the merge request's source branch; the reviewers "
            "read the checkout, so there is nothing to review without it",
        )
        return None, None

    if clone.head and head_sha and clone.head != head_sha:
        # The reviewed commit was outside the shallow window, so the reviewers are
        # reading a different tree from the one the diffs and position maps
        # describe. Reviewable, but every line number is now approximate.
        record_warning(
            metrics,
            "review_clone",
            f"checkout is at {clone.head[:8]} but the review is of {head_sha[:8]}; "
            f"line numbers may not match the merge request diff",
        )

    # Classify deterministically, with the same static classifier the graph mode
    # routes on, and apply the same exclusions. The orchestrator is handed this
    # assignment; it does not decide what to review.
    classified: list = []
    reviewable_changes: dict = {}
    for change in changes_to_review:
        path = change.get("new_path") or change.get("old_path")
        if not path or change.get("deleted_file"):
            continue
        if should_exclude_file(path, exclusion_patterns):
            continue
        file_type = StaticFileClassifier.classify_file(path)
        if not route_file(file_type):
            continue
        classified.append((path, file_type))
        reviewable_changes[path] = change

    if not classified:
        print("ℹ️  No reviewable files after classification")
        return ReviewPassResult(sink=None, summary="no reviewable files"), clone

    # One judgement the extension cannot make: which of these Go files carry
    # enough SQL that the database reviewer should see them too. Decided by its own
    # classifier, before the orchestrator gets the plan, so the orchestrator stays a
    # pure dispatcher and the decision is a logged artifact rather than dispatch
    # behaviour. Fails closed — no extra reviews, static assignment stands.
    route_config = build_run_config(
        thread_id,
        run_name="sql_routing",
        extra_metadata={"mr_id": mr_id, "project_path": project_path, "commit": head_sha},
        tags=["code-review", "sql-routing"],
    )
    sql_paths, route_error = await classify_sql_involvement(
        go_files(classified), clone.path, config=route_config
    )
    if route_error:
        record_warning(metrics, "sql_routing", route_error)
    for path in sql_paths:
        record_warning(
            metrics,
            "sql_routing_added",
            f"{path} is a Go file with substantial SQL; also reviewed by sql_reviewer",
            context=path,
        )

    review_config = build_run_config(
        thread_id,
        run_name="review_pass",
        extra_metadata={"mr_id": mr_id, "project_path": project_path, "commit": head_sha},
        tags=["code-review", "review-pass"],
    )
    # Everything left of the budget after the checkout, minus a reserve for comment
    # filtering, dedup and posting — findings nobody posts are worth nothing.
    review_deadline = None
    if deadline is not None:
        review_deadline = max(
            time.monotonic() + 60.0, deadline - DEEP_REVIEW_POST_RESERVE_S
        )

    result = await review_changed_files(
        classified=classified,
        clone_root=clone.path,
        source_branch=source_branch,
        mr_context=mr_context,
        sql_paths=sql_paths,
        config=review_config,
        concurrency=_review_concurrency(),
        per_file_timeout_s=float(int_env("FILE_REVIEW_TIMEOUT_S", 1200)),
        deadline=review_deadline,
    )

    for path in result.failed_files:
        # Carry the reason into analytics too. "could not be reviewed" was true of
        # both a reviewer that raised and a reviewer that returned without recording,
        # which are different problems with different fixes.
        reason = result.failure_reasons.get(path, "")
        record_error(
            metrics,
            "file_review",
            f"{path} could not be reviewed" + (f" ({reason})" if reason else ""),
            context=path,
        )

    return result, clone


async def _drop_duplicate_comments(
    metrics: RunMetrics,
    candidates: list,
    gitlab_url: str,
    project_path: str,
    mr_id: str,
    gitlab_token: str,
    config=None,
) -> list:
    """Remove the comments that have already been said, and return the rest.

    The last judgement before posting, and the only stage that looks at the MR's
    *existing* comments. Every filter before it judges a finding on its own
    merits; none of them can see that the previous run already posted it (the
    review is incremental and remembers nothing but a commit SHA) or that a human
    reviewer raised it first.

    Placed after the comment reviewer and **before** ``cap_comments`` on purpose:
    a duplicate that survives to the cap occupies a slot a unique finding could
    have used, so the level's budget would be spent on comments nobody reads.

    Non-fatal at every step, and every failure degrades toward *posting*:
    - notes unfetchable → intra-run dedup only, recorded as a warning;
    - agent failure → only the exact-text matches are dropped, recorded as a
      warning (not an error: the outcome is the pre-feature behaviour, a
      duplicate on the MR, not a lost finding or a corrupted comment).
    """
    if not candidates:
        return candidates

    # Note the falsy set deliberately excludes "": `.env.example` ships every
    # flag with an empty value, and an empty assignment must mean "take the
    # default" for a flag documented as default-on. The sibling flags
    # (COMMENT_REACTIONS_ENABLED, REVIEW_SUMMARY_ENABLED, EFFORT_ESTIMATE_ENABLED,
    # REVIEW_SUMMARY_IN_DESCRIPTION) do count "" as off, which means copying the
    # template silently disables them — a divergence on purpose, not an oversight.
    if os.getenv("COMMENT_DEDUP_ENABLED", "true").strip().lower() in (
        "false", "0", "no", "off"
    ):
        print("\n⏭️  Comment dedup disabled (COMMENT_DEDUP_ENABLED)")
        return candidates

    print(f"\n🧹 Checking {len(candidates)} comment(s) against what's already on the MR...")

    notes = await get_mr_notes(gitlab_url, project_path, mr_id, gitlab_token)
    if notes is None:
        # Distinguishable from "the MR has no comments": the check still runs, it
        # just can't see across runs, and the run says so rather than reporting a
        # clean dedup pass it did not perform.
        record_warning(
            metrics,
            "comment_dedup_notes",
            "could not list the MR's existing comments; deduplicated within this run only",
        )
        existing = []
    else:
        existing = collect_existing_comments(notes)
        print(f"  📖 {len(existing)} existing comment(s) on the MR to compare against")

    kept, dropped, dedup_error = await deduplicate_comments(
        candidates, existing, config=config
    )

    if dedup_error is not None:
        record_warning(metrics, "comment_dedup", dedup_error)

    for record in dropped:
        location = describe_issue(record["issue"])
        print(f"  🔁 Dropping {location} — duplicate of {record['duplicate_of']}")
        record_warning(
            metrics,
            "comment_dropped_duplicate",
            f"{location} duplicates {record['duplicate_of']}: {record['reason']}",
            context=location,
        )

    metrics.comments_deduplicated = len(dropped)
    if dropped:
        print(f"  🧹 Removed {len(dropped)} duplicate comment(s); {len(kept)} remain")
    return kept


async def _probe_fallback_endpoint(timeout_s: int) -> str:
    """One-line verdict on whether the fallback provider could take over.

    Called only when the primary answered the pre-flight with a throttle, and only
    to *report*: the run is not committed to the fallback here. A single 429 says
    the primary is busy this second, not that it will still be busy in five —
    while committing the whole review to the second provider on that one data
    point means running every file on the worse endpoint. The gateway switches
    per call, once a throttle has actually outlived a retry budget and earned it.

    What this buys is the line a person reading the pipeline log wants: "primary
    throttled, fallback answers" (the run will recover) versus "both throttled"
    (expect a timeout, and go look at the account limits).
    """
    if not fallback_configured():
        return "not configured — set FALLBACK_LLM_MODEL to give this run an escape hatch"
    target = LLMGateway.fallback_provider()
    probe = LLMGateway(
        lambda model: model,
        label="LLM pre-flight (fallback)",
        max_tokens=100,
        retries=False,
        pin=FALLBACK,
    )
    try:
        await asyncio.wait_for(
            probe.ainvoke("Reply with the single word OK."), timeout=timeout_s
        )
        return f"{target.describe()} answers — the run can fail over to it"
    except Exception as e:  # noqa: BLE001 — diagnostic only, never fatal
        info = parse_llm_error(e)
        return f"{target.describe()} also unavailable ({info.label}): {describe_error(e)[:110]}"


async def llm_preflight(metrics: RunMetrics) -> Optional[BaseException]:
    """One tiny bounded LLM call before any review work.

    Answers exactly two questions: is the endpoint **reachable**, and is the key
    **accepted**. Failing fast on either beats hanging partway through a review
    with GitLab state already touched — the reverse proxy swallowing requests is
    the case this was built for.

    Returns ``None`` when the review may proceed, or the exception that killed it.
    The caller records that exception: a preflight abort used to ``return 1``
    silently, so the run event said ``status="error"`` with ``error_count=0`` and
    no ``run_events`` row — a failure with nothing at all explaining it.

    **A rate limit is not a failed pre-flight.** A 429 came back *from the
    provider*, over the network, after authentication — which is a stronger
    liveness signal than anything else short of a completion, and it answers both
    questions with a yes. Aborting on it threw away the entire review over a
    condition that clears in seconds, ignored a configured fallback provider that
    never got a chance, and guaranteed zero comments where grinding through the
    retry/failover machinery might well have produced all of them. Observed on
    MR 11629: one throttled probe, run over, nothing reviewed.

    Hard denials (quota/credit) and provider config blocks still abort, because
    those are deterministic — every request in the run would fail identically.
    """
    timeout_s = int_env("LLM_PREFLIGHT_TIMEOUT_S", 60)
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    print(f"🩺 LLM endpoint pre-flight ({base_url})...")
    # retries=False: the probe reports the endpoint's state, it does not work
    # around it, and it has one wall-clock budget to do that in. Transient-failure
    # handling belongs to the per-call gateways during the review itself.
    probe = LLMGateway(
        lambda model: model,
        label="LLM pre-flight",
        max_tokens=100,
        retries=False,
    )
    try:
        await asyncio.wait_for(
            probe.ainvoke("Reply with the single word OK."),
            timeout=timeout_s,
        )
        print("✅ LLM endpoint reachable\n")
        return None
    except asyncio.TimeoutError as e:
        # wait_for's TimeoutError stringifies to "", so name the budget explicitly
        # or the recorded message is empty (see describe_error's docstring).
        err = TimeoutError(f"LLM pre-flight exceeded LLM_PREFLIGHT_TIMEOUT_S={timeout_s}s ({base_url})")
        err.__cause__ = e
        print(f"❌ LLM endpoint pre-flight failed after <= {timeout_s}s: {describe_error(err)}")
        print("   Aborting before review. Check the endpoint/proxy and retry.")
        return err
    except Exception as e:
        info = parse_llm_error(e)
        if info.is_rate_limit and not info.is_hard_limit and not info.is_config_block:
            print(f"⚠️  LLM endpoint is reachable but throttling ({info.label}).")
            escape = await _probe_fallback_endpoint(timeout_s)
            print(f"   Fallback provider: {escape}")
            print(
                "   Proceeding with the review — each call retries on its own "
                "backoff and fails over once the throttle outlives it.\n"
            )
            record_warning(
                metrics,
                "preflight",
                f"endpoint reachable but rate limited at pre-flight ({info.label}); "
                f"review proceeded. Fallback: {escape}. {describe_error(e)}",
            )
            return None
        print(f"❌ LLM endpoint pre-flight failed after <= {timeout_s}s: {describe_error(e)}")
        print("   Aborting before review. Check the endpoint/proxy and retry.")
        return e


async def process_mr(mr_url: str):
    """Process an MR and emit exactly one analytics event for the run.

    The review logic lives in ``_run_review``; this wrapper exists purely so the
    ``finally`` block fires on every exit path (the happy path, the "no new
    commits" / "no issues" early returns, and unhandled exceptions) and sends a
    single event. ``RunMetrics`` defaults ``status`` to ``"error"``, so a run
    that crashes before a clean exit path is recorded as a failure.
    """
    metrics = RunMetrics(mr_url=mr_url)
    run_start = time.monotonic()
    # The LLM fallback latch is sticky for a run, not for a process. In web mode
    # (app.py) one process serves many reviews, so an MR that got rate-limited
    # would otherwise demote every later review to the fallback model forever.
    LLMGateway.reset()
    try:
        return await _run_review(mr_url, metrics)
    except Exception as e:
        # Capture the fatal error into the event (status defaults to "error")
        # before it propagates, so unhandled crashes are debuggable in the collector.
        record_error(metrics, "fatal", describe_error(e), exc=e)
        raise
    finally:
        metrics.duration_seconds = round(time.monotonic() - run_start, 3)
        # Read the failover latch here rather than at the switch site: the finally
        # block is the one place every exit path passes through, so a run that
        # crashed after failing over still reports that it did.
        if LLMGateway.fallback_ever_used():
            metrics.model_fallback_used = True
            metrics.model_fallback_model = get_fallback_model()
        # Every provider transition, as a warning. `model_fallback_used` says only
        # *that* the run was mixed; these say when it switched, and — crucially —
        # whether it lost the fallback again and why. A revert is invisible
        # otherwise: when the retry on the primary succeeds the gateway absorbs the
        # block entirely, so no error event is recorded and the only trace is one
        # line in the CI log. That is how a run could switch to the fallback,
        # complete two file reviews on it, be returned to a throttled primary by a
        # single salvage call, and report nothing but `model_fallback_used = true`.
        for transition in LLMGateway.transitions():
            record_warning(metrics, transition.kind, transition.describe())
        await send_run_analytics(metrics)


async def _run_review(mr_url: str, metrics: RunMetrics):
    """Main function to process a GitLab merge request."""
    print(f"🚀 Processing GitLab MR: {mr_url}\n")

    # Resolve Langfuse tracing up front so a misconfiguration surfaces as one
    # startup line instead of silently producing an empty project. Non-fatal:
    # returns None (and the review runs untraced) whenever it can't be used.
    get_langfuse_handler()

    # Identity FIRST — before anything that can fail. parse_gitlab_mr_url is pure
    # string parsing (no network), and metrics.model is an env read, so both can
    # run here. They used to sit *after* the LLM pre-flight and MCP init, which
    # meant every early failure — most commonly the pre-flight itself, when the
    # endpoint is throttled or hanging — emitted an event with project_path,
    # mr_id and model all NULL. Those runs were unattributable: a dashboard
    # grouped by project silently dropped them, so a failing reviewer looked like
    # a reviewer that simply hadn't run.
    mr_info = parse_gitlab_mr_url(mr_url)
    if not mr_info:
        metrics.status = "config_error"
        record_error(metrics, "config", f"could not parse a GitLab MR URL from {mr_url!r}")
        return 1

    gitlab_url = mr_info["domain"]
    project_path = mr_info["project_path"]
    mr_id = mr_info["mr_id"]
    metrics.project_path = project_path
    metrics.mr_id = str(mr_id)
    # The configured (primary) model. If the run later fails over, that is
    # reported separately via model_fallback_used / model_fallback_model rather
    # than by rewriting this field.
    metrics.model = get_default_model()

    print(f"📦 Project: {project_path}")
    print(f"🔢 MR ID: {mr_id}\n")

    # Get GitLab credentials
    gitlab_token = os.getenv("GITLAB_TOKEN")
    if not gitlab_token:
        print("Error: GITLAB_TOKEN not found in environment variables.")
        print("Please set GITLAB_TOKEN in your .env file.")
        metrics.status = "config_error"
        record_error(metrics, "config", "GITLAB_TOKEN is not set")
        return 1

    # Fail fast if the LLM endpoint is unreachable or the key is rejected. A
    # *throttled* endpoint is not a failure — see llm_preflight.
    preflight_error = await llm_preflight(metrics)
    if preflight_error is not None:
        metrics.status = "config_error"
        record_error(
            metrics, "preflight", describe_error(preflight_error), exc=preflight_error
        )
        return 1

    # Initialize MCP toolkits
    print("🔌 Initializing MCP toolkits...")
    try:
        await initialize_mcp_toolkits()
    except Exception as e:
        print(f"⚠️  Warning: Failed to initialize some MCP toolkits: {describe_error(e)}")
        print("   Continuing with available tools...\n")
        record_error(metrics, "mcp_init", describe_error(e), exc=e)

    # Fetch MR information
    print("📥 Fetching MR information...")
    mr_data = await get_gitlab_mr_info(gitlab_url, project_path, mr_id, gitlab_token)
    if not mr_data:
        print("Error: Could not fetch MR information.")
        metrics.status = "error"
        return 1

    # Strip the agent's own mirrored summary out of the description immediately,
    # in place, so *every* later consumer reads only what a human wrote. The
    # description is an input as well as an output surface here — it feeds every
    # reviewer prompt via build_mr_context, the Jira-key scan, and the summary
    # agent's own payload (which takes the whole mr_data, so sanitising a local
    # copy would miss it). Left in, an MR reviewed twice would hand the agent its
    # previous summary back as the author's statement of intent.
    if mr_data.get("description"):
        mr_data["description"] = strip_summary_block(mr_data["description"])

    mr_title = mr_data.get("title", "")
    source_branch = mr_data.get("source_branch", "")
    target_branch = mr_data.get("target_branch", "")
    metrics.mr_title = mr_title
    metrics.source_branch = source_branch
    metrics.target_branch = target_branch
    # The MR author's username rides along with the posted-comment records so
    # the collector's evaluation tables know whose MR each scored comment is on.
    metrics.mr_author_username = (mr_data.get("author") or {}).get("username")

    # How much to post is a per-MR decision, taken from an MR label so a human can
    # ask for more and re-run without touching pipeline config. Resolved here, once,
    # because it widens which severities are collected for review at all — mediums
    # never reach the in-diff gate at the default level.
    verbosity = parse_verbosity(mr_data.get("labels"))
    metrics.review_verbosity = verbosity.name
    if verbosity.level:
        print(f"🔊 Verbosity label found: {verbosity.describe()}")
    else:
        print(f"🔈 Default verbosity (add {' / '.join(all_labels())} to the MR for more)")

    # Get commit SHAs for posting line comments
    diff_refs = mr_data.get("diff_refs", {})
    base_sha = diff_refs.get("base_sha")
    start_sha = diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha")
    metrics.head_sha = head_sha
    
    print(f"📄 Title: {mr_title}")
    print(f"🌿 Source Branch: {source_branch}")
    print(f"🎯 Target Branch: {target_branch}\n")

    # Build a per-MR thread id so every run of this review (one trace per
    # file, plus the comment-reviewer trace) is grouped into a single thread. Keying
    # on the MR means incremental re-reviews append to the same thread.
    thread_id = build_thread_id(project_path, mr_id)
    print(f"🧵 Trace session: {thread_id}\n")

    # Incremental review: read the marker note recording the last commit we reviewed
    # so we only review commits added since then instead of the whole MR diff.
    last_sha, marker_note_id = await get_review_marker(
        gitlab_url, project_path, mr_id, gitlab_token
    )
    metrics.last_reviewed_sha = last_sha
    if last_sha and head_sha and last_sha == head_sha:
        print(f"✅ No new commits since last review ({head_sha[:8]}). Nothing to do.")
        metrics.review_type = "none"
        metrics.status = "no_op"
        return 0

    # Fetch changes
    print("📥 Fetching changed files...")
    changes = await get_gitlab_mr_changes(gitlab_url, project_path, mr_id, gitlab_token)
    if not changes:
        print("Error: Could not fetch MR changes.")
        metrics.status = "error"
        return 1

    metrics.files_changed_total = len(changes)
    print(f"Found {len(changes)} changed file(s) in the full MR\n")

    # Decide which files to actually review. On the first run (no marker) we review the
    # full MR. On later runs we review only the incremental diff between the previously
    # reviewed commit and the current head. The full `changes` list is still used below to
    # build the shared MR context so reviewers keep full cross-file awareness.
    changes_to_review = changes
    metrics.review_type = "full"
    if last_sha and head_sha and last_sha != head_sha:
        print(f"🔁 Incremental review: comparing {last_sha[:8]}..{head_sha[:8]}")
        incremental = await get_gitlab_compare(
            gitlab_url, project_path, last_sha, head_sha, gitlab_token
        )
        if incremental is None:
            print("⚠️  Compare failed (e.g. force-push/rebase); falling back to full MR review.")
        elif len(incremental) == 0:
            print("✅ No code changes between last reviewed commit and head; updating marker only.")
            await upsert_review_marker(
                gitlab_url, project_path, mr_id, gitlab_token, head_sha, marker_note_id
            )
            metrics.review_type = "none"
            metrics.status = "no_op"
            return 0
        else:
            changes_to_review = incremental
            metrics.review_type = "incremental"
            print(f"Reviewing {len(changes_to_review)} changed file(s) from new commits\n")

    # Kick off the review-effort estimate concurrently with the file reviews. Placed
    # here (after the incremental early-returns above) so it only starts once we're
    # actually going to review — no orphaned task on a "no new commits"/"no code
    # changes" exit. It only needs MR metadata (file list, per-file line counts, dir
    # tree, scope) computed from the FULL `changes`, not the review results, so it
    # overlaps the much-longer review pass for ~no added wall-clock. Awaited (and its
    # "review effort::N/5" label applied) in the labeling section below; non-fatal.
    # Skipped when disabled or when an effort label is already on the MR (no
    # re-estimation on incremental re-runs).
    existing_labels = [str(lbl) for lbl in (mr_data.get("labels") or [])]
    effort_estimate_enabled = os.getenv(
        "EFFORT_ESTIMATE_ENABLED", "true"
    ).strip().lower() not in ("false", "0", "no", "off", "")
    # Matches the scoped "review effort::4/5" *and* the legacy space-separated
    # "Review effort 4/5", so an MR labelled before the scoped form was adopted is
    # still recognised — otherwise it would be re-estimated and end up carrying both.
    effort_already_labeled = has_effort_label(existing_labels)
    effort_task = None
    if effort_estimate_enabled and not effort_already_labeled:
        effort_config = build_run_config(
            thread_id,
            run_name="effort_estimate",
            extra_metadata={"mr_id": mr_id, "project_path": project_path, "commit": head_sha},
            tags=["code-review", "effort-estimate"],
        )
        effort_task = asyncio.create_task(estimate_review_effort(changes, config=effort_config))
    elif effort_already_labeled:
        print("🧮 Review-effort label already set — skipping estimation\n")

    # Get exclusion patterns from environment variable
    exclusion_patterns_str = os.getenv("REVIEW_EXCLUDE_PATTERNS", "")
    exclusion_patterns = [p.strip() for p in exclusion_patterns_str.split(",") if p.strip()]
    if exclusion_patterns:
        print(f"🚫 Exclusion patterns: {', '.join(exclusion_patterns)}\n")
    
    # Build the shared MR context once — injected into every file's review so
    # each reviewer knows what else changed in this MR (cuts false positives and
    # surfaces cross-file breakage).
    mr_description = mr_data.get("description", "") or ""
    changed_paths = []
    for change in changes:
        path = change.get("new_path") or change.get("old_path")
        if not path:
            continue
        if change.get("deleted_file", False):
            change_type = "deleted"
        elif change.get("new_file", False):
            change_type = "added"
        elif change.get("renamed_file", False):
            change_type = "renamed"
        else:
            change_type = "modified"
        changed_paths.append(f"{change_type}: {path}")

    jira_key = StaticFileClassifier.extract_jira_task_number(f"{mr_title}\n{mr_description}")
    jira_ticket_body = await fetch_jira_ticket_body(jira_key)
    mr_context = build_mr_context(
        mr_title=mr_title,
        mr_description=mr_description,
        changed_paths=changed_paths,
        jira_ticket_body=jira_ticket_body,
        jira_key="" if jira_key == "N/A" else jira_key,
    )
    print(
        f"🧩 Built shared MR context: {len(changed_paths)} changed file(s), "
        f"Jira: {jira_key if jira_key != 'N/A' else 'none'}\n"
    )

    # Kick off the review summary alongside the effort estimate — the orientation
    # note a human reviewer reads before the findings. Placed here (rather than
    # with the effort task) because it wants the Jira ticket body, which is only
    # fetched just above. Posted once per MR, gated on whether that comment
    # already exists rather than on "is this the first pass": a first pass that
    # dies on a rate limit leaves no summary and the retry should still post one,
    # while a pass that posted a summary and *then* died must not post a second.
    # It reads the FULL `changes`, not the incremental slice, since it describes
    # the whole MR — and runs concurrently, so it costs ~no wall-clock against the
    # much longer review pass. Non-fatal: a failure just means no summary comment.
    summary_enabled = os.getenv(
        "REVIEW_SUMMARY_ENABLED", "true"
    ).strip().lower() not in ("false", "0", "no", "off", "")
    summary_task = None
    if summary_enabled:
        if await has_review_summary(gitlab_url, project_path, mr_id, gitlab_token):
            print("📝 Review summary already posted on this MR — skipping\n")
        else:
            summary_config = build_run_config(
                thread_id,
                run_name="review_summary",
                extra_metadata={"mr_id": mr_id, "project_path": project_path, "commit": head_sha},
                tags=["code-review", "review-summary"],
            )
            summary_task = asyncio.create_task(
                generate_review_summary(
                    SummaryContext(
                        mr_data=mr_data,
                        changes=changes,
                        jira_body=jira_ticket_body,
                        source_branch=source_branch,
                    ),
                    config=summary_config,
                )
            )

    # ---- the review pass -------------------------------------------------
    # One path: an orchestrator delegates every classified file to its specialist
    # reviewer subagent over a real checkout, and any file it fails to dispatch is
    # reviewed directly by the same specialist. There is no second review
    # architecture to fall back to — the reviewers read the checkout, so a run
    # without one has nothing to review and says so.
    #
    # The budget starts **before** the clone, not after: measured on
    # acme-shop, checking out the source branch took 403s of a 1500s budget
    # (download-bound — ~190s of it the server sending 40MiB, 3s writing 28k
    # files). A deadline computed afterwards would hand the coverage pass a fresh
    # full budget on top. One clock for the whole review.
    overall_timeout_s = int_env("OVERALL_TIMEOUT_S", 1500)  # 25 min
    overall_deadline = time.monotonic() + overall_timeout_s
    print(f"⏳ Overall review budget: {overall_timeout_s}s\n")

    metrics.review_mode = "deep"
    review_result, src_clone = await _run_review_pass(
        metrics=metrics,
        changes_to_review=changes_to_review,
        exclusion_patterns=exclusion_patterns,
        gitlab_url=gitlab_url,
        project_path=project_path,
        gitlab_token=gitlab_token,
        source_branch=source_branch,
        target_branch=target_branch,
        mr_id=mr_id,
        head_sha=head_sha,
        mr_context=mr_context,
        thread_id=thread_id,
        deadline=overall_deadline,
    )

    all_comments = review_result.all_comments if review_result else []
    file_error_count = len(review_result.failed_files) if review_result else 0
    # No checkout means no review at all: surfaced as a config error so the run
    # exits non-zero with the marker held, rather than reporting an empty success.
    provider_blocked = src_clone is None
    timed_out = bool(review_result and review_result.truncated)
    # Set when the comment reviewer could not run, so the comments being posted came
    # from the deterministic fallback rather than the model's ranking + purge.
    comment_review_failed = False
    # file path -> the model that reviewed it. The deep path resolves one model per
    # run rather than per file, so every comment is attributed to whatever the
    # gateway had active; a mid-run failover still shows up in the run-level flags.
    model_by_file: dict = {}

    # The checkout has done its job — every reviewer has run. Removed here rather
    # than in a finally: the review pass is the only consumer, and everything after
    # this point (gate, comment filtering, dedup, posting) works from the API's
    # diffs. A crash before this line leaks a temp directory in a container that is
    # about to exit, which is the cheapest failure mode available.
    if src_clone is not None:
        src_clone.cleanup()

    print(f"\n{'=' * 60}")
    print(f"✅ Review pass done: {len(all_comments)} file(s) reviewed successfully")
    if file_error_count:
        print(f"⚠️  {file_error_count} file(s) failed to review (the run will exit non-zero)")
    if timed_out:
        print(f"⏳ Review pass truncated by the overall timeout — some files were not reviewed")
    print(f"{'=' * 60}")
    metrics.timed_out = timed_out

    # Aggregate per-run analytics from this pass. Files that returned None were
    # skipped (deleted/excluded/unfetchable); exceptions were counted as failures.
    metrics.files_reviewed = len(all_comments)
    metrics.files_failed = file_error_count
    metrics.files_skipped = max(
        0, len(changes_to_review) - len(all_comments) - file_error_count
    )
    for _file_path, issues in all_comments:
        metrics.issues_total += len(issues)
        for issue in issues:
            # Issues may be ReviewIssue objects (comment-reviewer path) or plain
            # dicts (the reviewer's fallback path), so read severity via the shared
            # dict-or-object helper — same convention as the posting loop below.
            severity = (get_severity(issue) or "").strip().lower()
            if severity == "high":
                metrics.issues_high += 1
            elif severity == "medium":
                metrics.issues_medium += 1
            elif severity == "low":
                metrics.issues_low += 1

    # Record that we've reviewed up to the current head, so the next pipeline run only
    # reviews newer commits. Done here (after the review pass, before the early-returns
    # below) so even runs that find no issues still advance the marker.
    #
    # Skip advancing when any file failed to review OR the run was truncated by the
    # overall timeout: leaving the marker where it is means a re-run re-enters the
    # incremental compare and reviews the file(s) we didn't get to, instead of
    # short-circuiting on "no new commits" and reporting a complete review that
    # never happened. Trade-off: on that retry, files that DID succeed get their
    # inline comments posted again (GitLab does not de-duplicate discussions) — a
    # visible duplicate is preferable to silently skipping un-reviewed files.
    if head_sha and file_error_count == 0 and not timed_out:
        if await upsert_review_marker(
            gitlab_url, project_path, mr_id, gitlab_token, head_sha, marker_note_id
        ):
            print(f"🔖 Review marker updated to {head_sha[:8]}")
    elif head_sha and timed_out:
        print("⏭️  Not advancing review marker — run hit the overall timeout; a re-run will review the remaining files")
    elif head_sha and file_error_count:
        print(f"⏭️  Not advancing review marker — {file_error_count} file(s) failed; a re-run will retry them")

    # The review pass is done — label the MR so it's visible the agent ran, and
    # apply the review-effort estimate. All non-fatal (a label failure never
    # affects the review). Skipped when a provider block aborted the run (no real
    # review happened) — then also drop the effort task rather than leave it
    # running. GitLab's add_labels is incremental and auto-creates labels, so this
    # needs no repo-side setup. A label already on the MR is not re-added.
    async def _label_mr(label: str) -> None:
        if label in existing_labels:
            print(f"🏷️  MR already labeled '{label}' — skipping")
            return
        if await add_mr_label(gitlab_url, project_path, mr_id, gitlab_token, label):
            existing_labels.append(label)
            print(f"🏷️  Labeled MR as '{label}'")
        else:
            record_warning(
                metrics, "mr_label", f"failed to add '{label}' label to the MR", context=label
            )

    effort = None
    if provider_blocked:
        if effort_task is not None:
            effort_task.cancel()
            try:
                await effort_task
            except BaseException:
                pass
    else:
        # ai-reviewed (override via MR_REVIEW_LABEL; empty disables it)
        review_label = os.getenv("MR_REVIEW_LABEL", "ai-reviewed").strip()
        if review_label:
            await _label_mr(review_label)

        # review effort::N/5 — await the estimate started at the top of the run.
        if effort_task is not None:
            try:
                effort = await effort_task
            except Exception:
                effort = None
            if effort:
                await _label_mr(effort_label(effort))

    # Post the review summary here — after the review pass but *before* any
    # findings, so it lands first in the MR timeline and orients the reviewer
    # before they hit the comments. Awaited rather than fire-and-forget:
    # run_with_cleanup tears the event loop down immediately after process_mr, so
    # a detached task would simply be killed. Non-fatal at every step.
    if summary_task is not None:
        if provider_blocked:
            summary_task.cancel()
            try:
                await summary_task
            except BaseException:
                pass
        else:
            try:
                summary = await summary_task
            except Exception as e:
                summary = None
                print(f"  ⚠️  Review summary failed (non-fatal): {describe_error(e)}")
            if summary is not None:
                await _post_review_summary(
                    metrics=metrics,
                    summary=summary,
                    effort=effort,
                    gitlab_url=gitlab_url,
                    project_path=project_path,
                    mr_id=mr_id,
                    gitlab_token=gitlab_token,
                )

    # Filter the findings using the comment reviewer agent
    print(f"\n{'=' * 60}")
    print(f"🔍 Filtering comments (verbosity: {verbosity.describe()})...")
    print(f"{'=' * 60}\n")

    issues_for_review = extract_issues_for_review(all_comments, verbosity.severities)

    scope = "/".join(verbosity.severities)
    print(f"📊 Found {len(issues_for_review)} {scope}-severity issues across all files")

    # Build inline-position maps from the FULL MR diff (GitLab inline positions are
    # against the MR diff_refs, not the incremental compare). Maps each changed
    # file's real new-path -> {new_line: old_line|None}. Used twice: by the in-diff
    # gate right below, and by the posting loop to place inline comments on the
    # exact right line (with old_line for context lines, which GitLab requires).
    position_maps: dict = {}
    for change in changes:
        p = change.get("new_path") or change.get("old_path")
        if p:
            position_maps[p] = build_diff_position_map(change.get("diff", "") or "")

    # Deterministic in-diff gate. Reviewers see the whole file (for context) but
    # self-report file_name/line per issue, so issues on unchanged code — or with a
    # fabricated file / miscounted line — slip through. Resolve every issue against
    # the authoritative MR diff BEFORE the comment reviewer: a line outside the
    # diff hunks can never be a valid inline comment, so drop it here instead of
    # letting it take one of the level's comment slots and end up as a noisy
    # general note. Each drop is recorded as a warning for analytics.
    in_diff_issues = []
    for issue in issues_for_review:
        file_name = issue.get("file_name") if isinstance(issue, dict) else issue.file_name
        line = issue.get("line") if isinstance(issue, dict) else issue.line
        _real_path, _old_line, in_diff, file_in_mr = resolve_inline_position(
            file_name, line, position_maps
        )
        if in_diff:
            in_diff_issues.append(issue)
        elif not file_in_mr:
            print(f"  🚫 Dropping issue {file_name}:{line} — file not in this MR (likely hallucinated)")
            record_warning(
                metrics,
                "issue_dropped_unknown_file",
                f"{file_name}:{line} references a file that is not part of this MR; dropped before comment filtering",
                context=f"{file_name}:{line}",
            )
        else:
            print(f"  🚫 Dropping issue {file_name}:{line} — line not in the MR diff")
            record_warning(
                metrics,
                "issue_dropped_not_in_diff",
                f"{file_name}:{line} is not a line in the MR diff; dropped before comment filtering",
                context=f"{file_name}:{line}",
            )
    dropped_count = len(issues_for_review) - len(in_diff_issues)
    if dropped_count:
        print(
            f"🚧 In-diff gate: dropped {dropped_count} issue(s) not on MR diff lines; "
            f"{len(in_diff_issues)} remain"
        )
    issues_for_review = in_diff_issues

    # Apply comment reviewer agent if there are high-severity issues
    if issues_for_review:
        comment_config = build_run_config(
            thread_id,
            run_name="comment_review",
            extra_metadata={
                "mr_id": mr_id,
                "project_path": project_path,
                "commit": head_sha,
            },
            tags=["code-review", "comment-review"],
        )
        filtered_issues, severity_corrections, comment_review_error = await review_comments_with_agent(
            issues_for_review, config=comment_config, verbosity=verbosity
        )
        # Deterministic backstop for the *severity*, before anything reads it. The
        # comment reviewer synthesizes its output and ReviewIssue.severity defaults
        # to "low", so a response that omits the field labels every finding it
        # selected as low — which then reaches the comment badge, the analytics store
        # and the dashboards. Observed on three default-verbosity runs (where only
        # highs are ever passed in) that posted nothing but "low" comments.
        filtered_issues, severity_fixes = restore_issue_severities(
            filtered_issues, issues_for_review, verbosity.severities
        )
        if severity_fixes:
            print(
                f"  🩹 Restored the reviewers' severity on {len(severity_fixes)} "
                f"comment(s) the comment reviewer re-labelled"
            )
            for file_name, line, was, now in severity_fixes:
                record_warning(
                    metrics,
                    "comment_severity_restored",
                    f"{file_name}:{line} came back from the comment reviewer as "
                    f"'{was}'; restored to '{now}' from the reviewers' finding",
                    context=f"{file_name}:{line}",
                )
        # Deterministic backstop for the *text*, the same failure through the same
        # door as the severity above: the comment reviewer synthesizes its output
        # and ReviewIssue.problem defaults to "", so a response that names a
        # finding and omits its description posts a comment whose whole body is
        # the severity badge — a red dot on the diff that says nothing. Recovered
        # from the reviewers' own finding at that position where there is one, and
        # dropped where there is not: a severity has a defensible fallback, a
        # description has none. Placed before dedup so the dedup agent compares
        # real text rather than "(no text)", and before the cap so a text-less
        # comment can never occupy a slot a real finding could have taken.
        filtered_issues, problem_fixes, problem_drops = restore_issue_problems(
            filtered_issues, issues_for_review
        )
        if problem_fixes:
            print(
                f"  🩹 Restored the reviewers' text on {len(problem_fixes)} "
                f"comment(s) the comment reviewer returned with no description"
            )
            for file_name, line in problem_fixes:
                record_warning(
                    metrics,
                    "comment_problem_restored",
                    f"{file_name}:{line} came back from the comment reviewer with an "
                    f"empty problem; restored the reviewers' own text",
                    context=f"{file_name}:{line}",
                )
        if problem_drops:
            print(
                f"  🚫 {len(problem_drops)} comment(s) had no description and no "
                f"matching finding to recover one from — not posted"
            )
            for file_name, line in problem_drops:
                record_warning(
                    metrics,
                    "comment_dropped_empty",
                    f"{file_name}:{line} came back from the comment reviewer with an "
                    f"empty problem and matched no finding; dropped rather than "
                    f"posting a comment with no text",
                    context=f"{file_name}:{line}",
                )

        # Duplicate suppression, against the MR's existing comments and within
        # this batch. Runs *before* the cap so a repeat never occupies a slot a
        # unique finding could have taken.
        dedup_config = build_run_config(
            thread_id,
            run_name="comment_dedup",
            extra_metadata={
                "mr_id": mr_id,
                "project_path": project_path,
                "commit": head_sha,
            },
            tags=["code-review", "comment-dedup"],
        )
        filtered_issues = await _drop_duplicate_comments(
            metrics,
            filtered_issues,
            gitlab_url,
            project_path,
            mr_id,
            gitlab_token,
            config=dedup_config,
        )

        # Deterministic backstop for the level's budget. The prompt states the cap,
        # but the prompt is a request and this is the last writer before posting —
        # same belt-and-braces as the suggestion field and the summary length. A
        # model that returns 40 comments at -vvv must not post 40.
        # Honour the comment reviewer's explicit demotions. This runs *after* the
        # restore above, which is what makes a demotion meaningful: the model cannot
        # change severity by editing the field (it is overwritten), only by naming
        # the finding, so a correction is always deliberate. A finding demoted below
        # the level's scope is dropped rather than re-badged — at the default
        # verbosity "this is actually a medium" means "this should not be posted",
        # which is the entire point of correcting it.
        filtered_issues, demoted = apply_severity_corrections(
            filtered_issues, severity_corrections, verbosity.severities
        )
        if demoted:
            print(
                f"  ⚖️  {len(demoted)} comment(s) demoted out of scope by the comment "
                f"reviewer — not posted"
            )
            for file_name, line, was, now, why in demoted:
                record_warning(
                    metrics,
                    "comment_severity_demoted",
                    f"{file_name}:{line} was labelled '{was}' by the reviewer but the "
                    f"comment reviewer judged it '{now}' ({why or 'no reason given'}); "
                    f"out of scope for verbosity {verbosity.name}, not posted",
                    context=f"{file_name}:{line}",
                )

        filtered_issues, over_cap = cap_comments(filtered_issues, verbosity)
        if over_cap:
            print(
                f"  ✂️  {len(over_cap)} comment(s) over the {verbosity.max_comments}-comment "
                f"cap for {verbosity.name} — not posted"
            )
            for issue in over_cap:
                file_name = issue.get("file_name") if isinstance(issue, dict) else issue.file_name
                line = issue.get("line") if isinstance(issue, dict) else issue.line
                record_warning(
                    metrics,
                    "comment_over_cap",
                    f"{file_name}:{line} not posted — over the {verbosity.max_comments}-comment "
                    f"cap for verbosity {verbosity.name}",
                    context=f"{file_name}:{line}",
                )
        metrics.comments_selected = len(filtered_issues)
        if comment_review_error is not None:
            # Degraded, not fatal: a bounded deterministic selection was substituted
            # (see fallback_comment_selection). Recorded as an error because the run
            # posted comments that never passed the reviewer's ranking or its
            # compile-time purge — that changes what a human sees on the MR, and
            # used to be visible only in the CI log.
            comment_review_failed = True
            record_error(
                metrics,
                "comment_review",
                comment_review_error,
                context=f"{len(issues_for_review)} issue(s) in, "
                        f"{len(filtered_issues)} posted by deterministic fallback",
            )
    else:
        print(f"ℹ️  No {scope}-severity issues to filter")
        # A timeout truncation is a partial review (marker held), but it exits 0 —
        # it posted what it could, by design; only real file errors exit non-zero.
        if provider_blocked:
            metrics.status = "config_error"
        elif timed_out:
            metrics.status = "timeout"
        else:
            metrics.status = "partial_failure" if file_error_count else "success"
        return 1 if file_error_count else 0

    # Post comments to GitLab
    print(f"\n{'=' * 60}")
    print(f"📤 Posting comments to GitLab MR...")
    print(f"{'=' * 60}\n")

    # position_maps (built before the in-diff gate above) drive inline placement.
    # Every issue reaching this loop already passed the gate, but the comment
    # reviewer synthesizes its output and may rewrite file_name/line, so the
    # resolution below — with its unknown-file drop and general-note fallback —
    # stays as a safety net.
    total_posted = 0
    total_failed = 0

    # Seed each posted comment with the :one:..:five: rating reactions so humans
    # can score it with one click (the collector's GitLab webhook stores the
    # scores). Default-on; set COMMENT_REACTIONS_ENABLED falsy to skip seeding.
    reactions_enabled = os.getenv(
        "COMMENT_REACTIONS_ENABLED", "true"
    ).strip().lower() not in ("false", "0", "no", "off", "")

    print(f"💬 Posting {len(filtered_issues)} comment(s) to GitLab...\n")

    for issue in filtered_issues:
        # Issues may be ReviewIssue objects (comment-reviewer success path) or plain
        # dicts (the reviewer's fallback path returns the raw high-severity items),
        # so read fields defensively — same dict-or-object handling as
        # comment_formatter.format_single_issue.
        file_name = issue.get("file_name") if isinstance(issue, dict) else issue.file_name
        line = issue.get("line") if isinstance(issue, dict) else issue.line
        severity = get_severity(issue)
        print(f"📝 Posting comment for {file_name}:{line}...")

        real_path, old_line, in_diff, file_in_mr = resolve_inline_position(
            file_name, line, position_maps
        )
        # Formatted after resolution: only a comment landing on a diff line can
        # carry an applyable ```suggestion block (GitLab ignores the syntax on a
        # note off the diff), so in_diff decides how the fix is rendered.
        comment = format_single_issue(issue, applyable=in_diff)

        # Drop issues whose file isn't part of this MR at all. Each reviewer
        # reviews one real changed file but self-reports file_name on every issue
        # and can fabricate it — observed: a Go MR produced a bogus Python
        # "src/core/engine.py:142" shell-injection finding for a file that does
        # not exist in the repo. Such a file can never be an inline position, and
        # posting it as a general note only surfaces the hallucination, so skip it
        # and record a warning for analytics instead.
        if not file_in_mr:
            print(f"  🚫 Skipping {file_name}:{line} — not a file in this MR (likely hallucinated)")
            record_warning(
                metrics,
                "comment_dropped_unknown_file",
                f"{file_name}:{line} references a file that is not part of this MR; dropped as a likely hallucination",
                context=f"{file_name}:{line}",
            )
            continue

        # Final cross-check, on the artifact rather than a field: never POST a
        # comment that says nothing. Every guard before this one protects a single
        # stage against a single known failure — the reviewer prompts and the
        # findings sink at the source, the comment reviewer's prompt and
        # restore_issue_problems over its synthesis. This asks the reader's own
        # question, so it holds for a stage nobody has thought of yet, and it is
        # placed after the unknown-file drop so the warning only ever fires for a
        # comment that would otherwise genuinely have been posted. If it does fire,
        # a guard upstream was bypassed — that is the signal, not the drop.
        if not comment_body_has_text(comment):
            print(f"  🚫 Skipping {file_name}:{line} — the comment body carries no text")
            record_warning(
                metrics,
                "comment_dropped_empty_body",
                f"{file_name}:{line} rendered to a body with nothing but the severity "
                f"badge; dropped at the posting loop, which means a guard upstream "
                f"was bypassed",
                context=f"{file_name}:{line}",
            )
            continue

        # Each retried POST attempt lands here as a warning (transient failure the
        # post recovered from); a permanent failure is recorded as an error below.
        retry_warnings: list = []
        if in_diff:
            # Valid inline position resolved from the diff — post on the exact line
            # (old_line set => context line, needs both sides).
            posted_note, post_error = await post_gitlab_comment(
                gitlab_url,
                project_path,
                mr_id,
                gitlab_token,
                comment,
                file_path=real_path,
                line_number=int(line),
                base_sha=base_sha,
                start_sha=start_sha,
                head_sha=head_sha,
                old_line=old_line,
                warnings_sink=retry_warnings,
            )
        else:
            # The model's line isn't part of the MR diff, so an inline position is
            # impossible; post a general note prefixed with the location instead of
            # attempting (and failing) an inline post.
            located = f"**`{file_name}:{line}`**\n\n{comment}" if line is not None else comment
            posted_note, post_error = await post_gitlab_comment(
                gitlab_url,
                project_path,
                mr_id,
                gitlab_token,
                located,
                warnings_sink=retry_warnings,
            )
            record_warning(
                metrics,
                "comment_not_in_diff",
                f"{file_name}:{line} is not a line in the MR diff; posted as a general note",
                context=f"{file_name}:{line}",
            )
        for w in retry_warnings:
            record_warning(
                metrics,
                w.get("stage", "comment_post"),
                w.get("message", ""),
                context=f"{file_name}:{line}",
            )
        if post_error is None:
            print(f"  ✅ Posted comment for line {line}")
            total_posted += 1
            note_id = (posted_note or {}).get("note_id")
            if note_id is None:
                # Posted but the response carried no note id — reactions can't be
                # keyed to it, so the comment can't be scored. Surface why.
                record_warning(
                    metrics,
                    "comment_reaction",
                    "posted comment returned no note id; rating reactions skipped",
                    context=f"{file_name}:{line}",
                )
            else:
                canonical = real_path or file_name
                metrics.posted_comments.append({
                    "note_id": note_id,
                    "discussion_id": posted_note.get("discussion_id"),
                    "comment_type": posted_note.get("comment_type"),
                    "file": canonical,  # canonical MR path when resolved
                    "line": line,
                    "severity": severity,
                    # The model that reviewed this file. Falls back to whatever is
                    # active now if the path can't be matched (e.g. the comment
                    # reviewer rewrote file_name while synthesizing its output).
                    "model": (
                        model_by_file.get(canonical)
                        or model_by_file.get(file_name)
                        or LLMGateway.effective_model()
                    ),
                })
                if reactions_enabled:
                    failed_emojis = await add_note_award_emojis(
                        gitlab_url, project_path, mr_id, gitlab_token, note_id
                    )
                    if failed_emojis:
                        record_warning(
                            metrics,
                            "comment_reaction",
                            f"failed to seed rating reaction(s) {', '.join(':' + n + ':' for n in failed_emojis)} on note {note_id}",
                            context=f"{file_name}:{line}",
                        )
                    else:
                        print(f"  🗳️  Seeded 1-5 rating reactions on note {note_id}")
        else:
            print(f"  ❌ Failed to post comment for line {line}")
            total_failed += 1
            record_error(metrics, "comment_post", post_error, context=f"{file_name}:{line}")
    
    print(f"\n{'=' * 60}")
    print(f"✨ Review completed!")
    print(f"📤 Comments posted: {total_posted}")
    print(f"❌ Failed comments: {total_failed}")
    print(f"{'=' * 60}\n")

    metrics.comments_posted = total_posted
    metrics.comments_failed = total_failed
    if provider_blocked:
        metrics.status = "config_error"
    elif timed_out:
        metrics.status = "timeout"
    else:
        # A failed comment reviewer counts as a partial failure: the run posted, but
        # what it posted skipped the ranking and the compile-time purge. Without this
        # the status stayed "success" and nothing distinguished it from a clean run.
        degraded = file_error_count or total_failed or comment_review_failed
        metrics.status = "partial_failure" if degraded else "success"

    # Non-zero exit if any file failed to review or any comment failed to post, so
    # GitLab CI reflects the failure instead of showing a green job. A timeout
    # truncation exits 0 on purpose — it degraded gracefully and posted its
    # findings; the held marker makes a later run pick up the remaining files.
    return 1 if (file_error_count or total_failed) else 0


async def run_with_cleanup(mr_url: str) -> int:
    """Run the MR processing with proper cleanup in the same event loop.

    Returns the process exit code (0 = success, non-zero = a review or comment-post
    error occurred) so the CLI can propagate it to the shell / GitLab CI.
    """
    try:
        exit_code = await process_mr(mr_url)
        return exit_code if exit_code is not None else 0
    finally:
        # Push buffered Langfuse spans before the process exits — a one-shot CI
        # container would otherwise die inside the batch interval and lose the
        # run's traces. Non-fatal, and a no-op when tracing is off.
        flush_langfuse()

        print("\n🔌 Cleaning up MCP sessions...")
        try:
            await close_mcp_toolkits()
            print("✅ MCP sessions closed successfully")
        except Exception as e:
            print(f"⚠️  Error closing MCP sessions: {describe_error(e)}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="CodeReviewAgent - AI-powered code review for GitLab merge requests"
    )
    parser.add_argument(
        "--gitlab-mr",
        type=str,
        required=True,
        help="GitLab merge request URL (e.g., https://gitlab.example.com/group/project/-/merge_requests/123)"
    )
    
    args = parser.parse_args()
    
    # Check for OpenAI API key
    if not os.getenv("OPENROUTER_API_KEY"):
        print("Error: OPENROUTER_API_KEY not found in environment variables.")
        print("Please set OPENROUTER_API_KEY in your .env file.")
        sys.exit(1)
    
    # Run async main function with cleanup in the same event loop
    try:
        exit_code = asyncio.run(run_with_cleanup(args.gitlab_mr))
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user")
        sys.exit(130)
    except Exception as e:
        # Any unhandled error means the review did not complete — fail loudly so
        # GitLab CI does not treat the job as successful.
        print(f"❌ Fatal error during review: {describe_error(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(exit_code or 0)


if __name__ == "__main__":
    main()
