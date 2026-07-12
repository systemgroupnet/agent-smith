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
    get_gitlab_compare,
    get_review_marker,
    upsert_review_marker,
)
from core.agents.file_reviewer import review_file_with_workflow, review_comments_with_agent
from core.utils.comment_formatter import format_single_issue, extract_high_severity_issues, get_severity
from core.utils.mr_context import build_mr_context, fetch_jira_ticket_body
from core.utils.file_classifier import StaticFileClassifier
from core.config.tracing import build_thread_id, build_run_config
from core.config.langchain_config import get_llm_model, int_env
from core.analytics import send_run_analytics
from dto.analytics import RunMetrics, record_error


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
        thread_id: LangSmith thread id grouping all runs of this MR review
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
        
        # Build the per-file LangSmith run config so this file's trace (and all of its
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
        
        file_elapsed_time = time.time() - file_start_time
        print(f"✅ Found {len(issues)} issue(s) (⏱️  Total file processing: {file_elapsed_time:.2f}s)")
        
        return (file_path, issues)
    
    except Exception as e:
        file_info = file_path if file_path else "unknown file"
        print(f"❌ Error processing file {file_info}: {e}")
        # Propagate so process_mr records this file as failed and the run exits
        # non-zero. Best-effort: sibling files in the batch are reviewed
        # independently (asyncio.gather with return_exceptions=True).
        raise


async def llm_preflight() -> bool:
    """One tiny bounded LLM call before any review work.

    Fails fast (clear message + non-zero exit) when the LLM endpoint is dead —
    e.g. the internal reverse proxy swallowing requests — instead of hanging
    partway through a review with GitLab state already touched.
    """
    timeout_s = int_env("LLM_PREFLIGHT_TIMEOUT_S", 60)
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    print(f"🩺 LLM endpoint pre-flight ({base_url})...")
    try:
        await asyncio.wait_for(
            get_llm_model(max_tokens=100).ainvoke("Reply with the single word OK."),
            timeout=timeout_s,
        )
        print("✅ LLM endpoint reachable\n")
        return True
    except Exception as e:
        reason = str(e) or type(e).__name__
        print(f"❌ LLM endpoint pre-flight failed after <= {timeout_s}s: {reason}")
        print("   Aborting before review. Check the endpoint/proxy and retry.")
        return False


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
    try:
        return await _run_review(mr_url, metrics)
    except Exception as e:
        # Capture the fatal error into the event (status defaults to "error")
        # before it propagates, so unhandled crashes are debuggable in the collector.
        record_error(metrics, "fatal", str(e), exc=e)
        raise
    finally:
        metrics.duration_seconds = round(time.monotonic() - run_start, 3)
        await send_run_analytics(metrics)


async def _run_review(mr_url: str, metrics: RunMetrics):
    """Main function to process a GitLab merge request."""
    print(f"🚀 Processing GitLab MR: {mr_url}\n")

    # Fail fast if the LLM endpoint is unreachable — nothing below works without it.
    if not await llm_preflight():
        return 1

    # Initialize MCP toolkits
    print("🔌 Initializing MCP toolkits...")
    try:
        await initialize_mcp_toolkits()
    except Exception as e:
        print(f"⚠️  Warning: Failed to initialize some MCP toolkits: {e}")
        print("   Continuing with available tools...\n")
        record_error(metrics, "mcp_init", str(e), exc=e)
    
    # Record the model up front so even an early-exit run reports which model was
    # configured (matches the reviewer's default in langchain_config / CLAUDE.md).
    metrics.model = os.getenv("DEFAULT_LLM_MODEL", "deepseek/deepseek-v4-pro")

    # Parse MR URL
    mr_info = parse_gitlab_mr_url(mr_url)
    if not mr_info:
        metrics.status = "config_error"
        return 1

    # Get GitLab credentials
    gitlab_token = os.getenv("GITLAB_TOKEN")
    if not gitlab_token:
        print("Error: GITLAB_TOKEN not found in environment variables.")
        print("Please set GITLAB_TOKEN in your .env file.")
        metrics.status = "config_error"
        return 1

    gitlab_url = mr_info["domain"]
    project_path = mr_info["project_path"]
    mr_id = mr_info["mr_id"]
    metrics.project_path = project_path
    metrics.mr_id = str(mr_id)

    print(f"📦 Project: {project_path}")
    print(f"🔢 MR ID: {mr_id}\n")
    
    # Fetch MR information
    print("📥 Fetching MR information...")
    mr_data = await get_gitlab_mr_info(gitlab_url, project_path, mr_id, gitlab_token)
    if not mr_data:
        print("Error: Could not fetch MR information.")
        metrics.status = "error"
        return 1

    mr_title = mr_data.get("title", "")
    source_branch = mr_data.get("source_branch", "")
    target_branch = mr_data.get("target_branch", "")
    metrics.mr_title = mr_title
    metrics.source_branch = source_branch
    metrics.target_branch = target_branch

    # Get commit SHAs for posting line comments
    diff_refs = mr_data.get("diff_refs", {})
    base_sha = diff_refs.get("base_sha")
    start_sha = diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha")
    metrics.head_sha = head_sha
    
    print(f"📄 Title: {mr_title}")
    print(f"🌿 Source Branch: {source_branch}")
    print(f"🎯 Target Branch: {target_branch}\n")

    # Build a per-MR LangSmith thread id so every run of this review (one trace per
    # file, plus the comment-reviewer trace) is grouped into a single thread. Keying
    # on the MR means incremental re-reviews append to the same thread.
    thread_id = build_thread_id(project_path, mr_id)
    print(f"🧵 LangSmith thread: {thread_id}\n")

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

    # Process files concurrently in batches of 5
    all_comments = []
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
                record_error(metrics, "file_review", str(result), exc=result)
                continue
            if result is not None:
                all_comments.append(result)
        
        print(f"\n✅ Batch {batch_num + 1}/{total_batches} completed")
    
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

    # Filter high-severity issues using comment reviewer agent
    print(f"\n{'=' * 60}")
    print(f"🔍 Filtering high-severity comments...")
    print(f"{'=' * 60}\n")
    
    all_high_severity_issues = extract_high_severity_issues(all_comments)
    
    print(f"📊 Found {len(all_high_severity_issues)} high-severity issues across all files")
    
    # Apply comment reviewer agent if there are high-severity issues
    if all_high_severity_issues:
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
        filtered_issues = await review_comments_with_agent(
            all_high_severity_issues, config=comment_config
        )
        metrics.comments_selected = len(filtered_issues)
    else:
        print(f"ℹ️  No high-severity issues to filter")
        # A timeout truncation is a partial review (marker held), but it exits 0 —
        # it posted what it could, by design; only real file errors exit non-zero.
        metrics.status = "partial_failure" if (file_error_count or timed_out) else "success"
        return 1 if file_error_count else 0

    # Post comments to GitLab
    print(f"\n{'=' * 60}")
    print(f"📤 Posting comments to GitLab MR...")
    print(f"{'=' * 60}\n")
    
    total_posted = 0
    total_failed = 0
    
    print(f"💬 Posting {len(filtered_issues)} comment(s) to GitLab...\n")
    
    for issue in filtered_issues:
        # Issues may be ReviewIssue objects (comment-reviewer success path) or plain
        # dicts (the reviewer's fallback path returns the raw high-severity items),
        # so read fields defensively — same dict-or-object handling as
        # comment_formatter.format_single_issue.
        file_name = issue.get("file_name") if isinstance(issue, dict) else issue.file_name
        line = issue.get("line") if isinstance(issue, dict) else issue.line
        print(f"📝 Posting comment for {file_name}:{line}...")
        comment = format_single_issue(issue)

        post_error = await post_gitlab_comment(
            gitlab_url,
            project_path,
            mr_id,
            gitlab_token,
            comment,
            file_path=file_name,
            line_number=line,
            base_sha=base_sha,
            start_sha=start_sha,
            head_sha=head_sha
        )
        if post_error is None:
            print(f"  ✅ Posted comment for line {line}")
            total_posted += 1
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
    metrics.status = "partial_failure" if (file_error_count or total_failed or timed_out) else "success"

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
        print("\n🔌 Cleaning up MCP sessions...")
        try:
            await close_mcp_toolkits()
            print("✅ MCP sessions closed successfully")
        except Exception as e:
            print(f"⚠️  Error closing MCP sessions: {e}")


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
        print(f"❌ Fatal error during review: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(exit_code or 0)


if __name__ == "__main__":
    main()
