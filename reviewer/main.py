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
    add_mr_label,
    SCORE_REACTION_EMOJIS,
    SUMMARY_MARKER_TOKEN,
)
from core.agents.file_reviewer import review_file_with_workflow, review_comments_with_agent
from core.agents.effort_agent import estimate_review_effort
from core.agents.summary_agent import SummaryContext, generate_review_summary
from core.utils.comment_formatter import (
    format_single_issue,
    format_review_summary,
    extract_issues_for_review,
    get_severity,
    restore_issue_severities,
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

    The same text is also mirrored into the **MR description**
    (``REVIEW_SUMMARY_IN_DESCRIPTION``), which is the closest thing GitLab has to
    pinning: there is no pinned flag on a note, so the description — the one
    region that always renders above every comment — is the only place a summary
    is guaranteed to be seen without scrolling. The note remains canonical; the
    description copy carries no reactions and is not reported to analytics.
    Mirrored *first* and independently of the note, so a failed note post still
    leaves the MR oriented, and a failed mirror still leaves a rateable comment.

    The body carries a hidden ``SUMMARY_MARKER_TOKEN`` comment; ``has_review_summary``
    looks for exactly that, so a re-run finds this note and never posts a second
    (it lists plain notes too — the review marker is one).

    Non-fatal end to end: a failure here records a warning and leaves the review
    untouched.
    """
    summary_text = format_review_summary(summary, effort=effort)

    mirror_enabled = os.getenv(
        "REVIEW_SUMMARY_IN_DESCRIPTION", "true"
    ).strip().lower() not in ("false", "0", "no", "off", "")
    if mirror_enabled:
        mirror_error = await upsert_summary_in_description(
            gitlab_url, project_path, mr_id, gitlab_token, summary_text
        )
        if mirror_error is None:
            print("  📌 Mirrored review summary into the MR description")
        else:
            print(f"  ⚠️  Could not mirror the summary into the MR description (non-fatal): {mirror_error}")
            record_warning(
                metrics,
                "review_summary_description",
                f"failed to mirror the review summary into the MR description: {mirror_error}",
            )

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


async def process_single_file(
    change: dict,
    exclusion_patterns: list,
    gitlab_url: str,
    project_path: str,
    source_branch: str,
    gitlab_token: str,
    mr_title: str,
    target_branch: str,
    mr_context: str = "",
    thread_id: str = None,
    mr_id: str = None,
    head_sha: str = None,
    model_by_file: dict = None,
) -> tuple:
    """
    Process a single file for review.

    Args:
        change: GitLab change object containing file information
        exclusion_patterns: List of file patterns to exclude
        gitlab_url: GitLab instance URL
        project_path: Project path in GitLab
        source_branch: Source branch name
        gitlab_token: GitLab API token
        mr_title: Merge request title
        target_branch: Target branch name
        thread_id: Tracing thread id grouping all runs of this MR review
        mr_id: Merge request IID (for trace metadata)
        head_sha: Current head commit SHA (for trace metadata)

    Returns:
        Tuple of (file_path, issues) or None if file should be skipped
    """
    file_path = None
    try:
        file_path = change.get("new_path") or change.get("old_path")
        if not file_path:
            return None
        
        # Skip deleted files
        if change.get("deleted_file", False):
            print(f"⏭️  Skipping deleted file: {file_path}")
            return None
        
        # Skip excluded files based on patterns
        if should_exclude_file(file_path, exclusion_patterns):
            print(f"⏭️  Skipping excluded file: {file_path}")
            return None
        
        file_start_time = time.time()
        
        print(f"\n{'=' * 60}")
        print(f"📝 Processing: {file_path}")
        print(f"{'=' * 60}")
        
        # Get file content
        file_content = await get_file_content(gitlab_url, project_path, file_path, source_branch, gitlab_token)
        if not file_content:
            print(f"⚠️  Could not fetch content for {file_path}, skipping...")
            return None
        
        # Get diff
        diff = change.get("diff", "")
        
        # Build the per-file tracing run config so this file's trace (and all of its
        # nested agent/LLM/tool runs) lands inside this MR's thread.
        review_config = None
        if thread_id:
            review_config = build_run_config(
                thread_id,
                run_name=f"review:{file_path}",
                extra_metadata={
                    "file_path": file_path,
                    "mr_id": mr_id,
                    "project_path": project_path,
                    "commit": head_sha,
                },
                tags=["code-review", "file-review"],
            )

        # Review file using LangGraph workflow. Bounded so one wedged call chain
        # (LLM endpoint or MCP server gone silent) fails this file — counted as a
        # file error, non-zero exit, marker not advanced — instead of stalling the
        # entire run indefinitely.
        print(f"🔍 Reviewing file...")
        file_timeout_s = int_env("FILE_REVIEW_TIMEOUT_S", 1200)
        try:
            issues = await asyncio.wait_for(
                review_file_with_workflow(
                    file_path=file_path,
                    content=file_content,
                    diff=diff,
                    merge_request_title=mr_title,
                    target_branch=target_branch,
                    source_branch=source_branch,
                    gitlab_token=gitlab_token,
                    gitlab_url=gitlab_url,
                    project_path=project_path,
                    mr_context=mr_context,
                    config=review_config,
                ),
                timeout=file_timeout_s,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"review timed out after {file_timeout_s}s (FILE_REVIEW_TIMEOUT_S)"
            )

        if issues is None:
            print(f"⚠️  Could not review {file_path}, skipping...")
            return None

        # Which model actually produced these issues. Read *after* the review, so
        # a file whose review completed on the fallback (because every primary
        # attempt was rate limited) is attributed to the fallback. Recorded per
        # file rather than per run because the failover latch flips mid-run.
        if model_by_file is not None:
            model_by_file[file_path] = LLMGateway.effective_model()


        file_elapsed_time = time.time() - file_start_time
        print(f"✅ Found {len(issues)} issue(s) (⏱️  Total file processing: {file_elapsed_time:.2f}s)")
        
        return (file_path, issues)
    
    except Exception as e:
        file_info = file_path if file_path else "unknown file"
        print(f"❌ Error processing file {file_info}: {describe_error(e)}")
        # Propagate so process_mr records this file as failed and the run exits
        # non-zero. Best-effort: sibling files in the batch are reviewed
        # independently (asyncio.gather with return_exceptions=True).
        raise


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

    # Process files concurrently in batches of 5
    all_comments = []
    # file path -> model that reviewed it, so each posted comment can be
    # attributed to the model that actually produced it (see model_by_file in
    # process_single_file). A run that fails over mid-way has both.
    model_by_file: dict = {}
    file_error_count = 0  # files whose review raised — drives the non-zero exit code
    batch_size = max(1, int_env("FILE_BATCH_SIZE", 5))
    total_batches = (len(changes_to_review) + batch_size - 1) // batch_size

    # Overall runtime budget: once exceeded, stop reviewing remaining files and
    # jump straight to comment filtering/posting with whatever was gathered, so a
    # slow/large MR still posts findings within the CI window instead of running
    # until the job is killed. The marker is NOT advanced when we truncate (see
    # below), so a re-run picks up the files we skipped.
    overall_timeout_s = int_env("OVERALL_TIMEOUT_S", 1500)  # 25 min
    overall_deadline = time.monotonic() + overall_timeout_s
    timed_out = False
    # Set when a file fails with an account-level provider block (data-policy /
    # guardrail / quota): every other file would fail identically, so we stop.
    provider_blocked = False
    # Set when the comment reviewer could not run, so the comments being posted came
    # from the deterministic fallback rather than the model's ranking + purge.
    comment_review_failed = False

    print(f"🔄 Processing {len(changes_to_review)} file(s) in {total_batches} batch(es) of up to {batch_size} files concurrently")
    print(f"⏳ Overall review budget: {overall_timeout_s}s\n")

    for batch_num in range(total_batches):
        if time.monotonic() >= overall_deadline:
            reviewed_so_far = batch_num * batch_size
            remaining = len(changes_to_review) - reviewed_so_far
            timed_out = True
            print(
                f"\n⏳ Overall timeout ({overall_timeout_s}s) reached after {batch_num}/{total_batches} "
                f"batch(es); skipping ~{remaining} remaining file(s) and proceeding to comment filtering/posting."
            )
            break

        start_idx = batch_num * batch_size
        end_idx = min(start_idx + batch_size, len(changes_to_review))
        batch = changes_to_review[start_idx:end_idx]

        print(f"\n{'=' * 60}")
        print(f"📦 Processing batch {batch_num + 1}/{total_batches} ({len(batch)} file(s))")
        print(f"{'=' * 60}")

        # Create tasks for concurrent processing
        tasks = [
            process_single_file(
                change=change,
                exclusion_patterns=exclusion_patterns,
                gitlab_url=gitlab_url,
                project_path=project_path,
                source_branch=source_branch,
                gitlab_token=gitlab_token,
                mr_title=mr_title,
                target_branch=target_branch,
                mr_context=mr_context,
                thread_id=thread_id,
                mr_id=mr_id,
                head_sha=head_sha,
                model_by_file=model_by_file,
            )
            for change in batch
        ]
        
        # Process batch concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Aggregate results
        for result in results:
            if isinstance(result, Exception):
                print(f"❌ Exception during file processing: {result}")
                file_error_count += 1
                record_error(metrics, "file_review", describe_error(result), exc=result)
                if is_provider_hard_block(result):
                    provider_blocked = True
                continue
            if result is not None:
                all_comments.append(result)

        print(f"\n✅ Batch {batch_num + 1}/{total_batches} completed")

        if provider_blocked:
            # Account-level block (data-policy/guardrail or quota) — every remaining
            # file would fail the same way. Stop and surface it clearly instead of
            # emitting the same error for the whole MR.
            print(
                "\n🛑 Aborting remaining files — the LLM provider is refusing all requests "
                "(OpenRouter data-policy/guardrail or quota). Fix the account/model config "
                "(openrouter.ai/settings/privacy, DEFAULT_LLM_MODEL, OPENROUTER_PROVIDER) and re-run."
            )
            break
    
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
        filtered_issues, comment_review_error = await review_comments_with_agent(
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
        # Deterministic backstop for the level's budget. The prompt states the cap,
        # but the prompt is a request and this is the last writer before posting —
        # same belt-and-braces as the suggestion field and the summary length. A
        # model that returns 40 comments at -vvv must not post 40.
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
