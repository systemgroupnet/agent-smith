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
)
from core.agents.file_reviewer import review_file_with_workflow, review_comments_with_agent
from core.utils.comment_formatter import format_single_issue, extract_high_severity_issues


async def process_single_file(
    change: dict,
    exclusion_patterns: list,
    gitlab_url: str,
    project_path: str,
    source_branch: str,
    gitlab_token: str,
    mr_title: str,
    target_branch: str
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
        
        # Review file using LangGraph workflow
        print(f"🔍 Reviewing file...")
        issues = await review_file_with_workflow(
            file_path=file_path,
            content=file_content,
            diff=diff,
            merge_request_title=mr_title,
            target_branch=target_branch,
            source_branch=source_branch,
            gitlab_token=gitlab_token,
            gitlab_url=gitlab_url,
            project_path=project_path
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
        return None


async def process_mr(mr_url: str):
    """Main function to process a GitLab merge request."""
    print(f"🚀 Processing GitLab MR: {mr_url}\n")
    
    # Initialize MCP toolkits
    print("🔌 Initializing MCP toolkits...")
    try:
        await initialize_mcp_toolkits()
    except Exception as e:
        print(f"⚠️  Warning: Failed to initialize some MCP toolkits: {e}")
        print("   Continuing with available tools...\n")
    
    # Parse MR URL
    mr_info = parse_gitlab_mr_url(mr_url)
    if not mr_info:
        sys.exit(1)
    
    # Get GitLab credentials
    gitlab_token = os.getenv("GITLAB_TOKEN")
    if not gitlab_token:
        print("Error: GITLAB_TOKEN not found in environment variables.")
        print("Please set GITLAB_TOKEN in your .env file.")
        sys.exit(1)
    
    gitlab_url = mr_info["domain"]
    project_path = mr_info["project_path"]
    mr_id = mr_info["mr_id"]
    
    print(f"📦 Project: {project_path}")
    print(f"🔢 MR ID: {mr_id}\n")
    
    # Fetch MR information
    print("📥 Fetching MR information...")
    mr_data = await get_gitlab_mr_info(gitlab_url, project_path, mr_id, gitlab_token)
    if not mr_data:
        print("Error: Could not fetch MR information.")
        sys.exit(1)
    
    mr_title = mr_data.get("title", "")
    source_branch = mr_data.get("source_branch", "")
    target_branch = mr_data.get("target_branch", "")
    
    # Get commit SHAs for posting line comments
    diff_refs = mr_data.get("diff_refs", {})
    base_sha = diff_refs.get("base_sha")
    start_sha = diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha")
    
    print(f"📄 Title: {mr_title}")
    print(f"🌿 Source Branch: {source_branch}")
    print(f"🎯 Target Branch: {target_branch}\n")
    
    # Fetch changes
    print("📥 Fetching changed files...")
    changes = await get_gitlab_mr_changes(gitlab_url, project_path, mr_id, gitlab_token)
    if not changes:
        print("Error: Could not fetch MR changes.")
        sys.exit(1)
    
    print(f"Found {len(changes)} changed file(s)\n")
    
    # Get exclusion patterns from environment variable
    exclusion_patterns_str = os.getenv("REVIEW_EXCLUDE_PATTERNS", "")
    exclusion_patterns = [p.strip() for p in exclusion_patterns_str.split(",") if p.strip()]
    if exclusion_patterns:
        print(f"🚫 Exclusion patterns: {', '.join(exclusion_patterns)}\n")
    
    # Process files concurrently in batches of 5
    all_comments = []
    batch_size = int(os.getenv("FILE_BATCH_SIZE", "5"))
    total_batches = (len(changes) + batch_size - 1) // batch_size
    
    print(f"🔄 Processing {len(changes)} file(s) in {total_batches} batch(es) of up to {batch_size} files concurrently\n")
    
    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min(start_idx + batch_size, len(changes))
        batch = changes[start_idx:end_idx]
        
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
                target_branch=target_branch
            )
            for change in batch
        ]
        
        # Process batch concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Aggregate results
        for result in results:
            if isinstance(result, Exception):
                print(f"❌ Exception during file processing: {result}")
                continue
            if result is not None:
                all_comments.append(result)
        
        print(f"\n✅ Batch {batch_num + 1}/{total_batches} completed")
    
    print(f"\n{'=' * 60}")
    print(f"✅ All batches completed: {len(all_comments)} file(s) reviewed successfully")
    print(f"{'=' * 60}")
    
    # Filter high-severity issues using comment reviewer agent
    print(f"\n{'=' * 60}")
    print(f"🔍 Filtering high-severity comments...")
    print(f"{'=' * 60}\n")
    
    all_high_severity_issues = extract_high_severity_issues(all_comments)
    
    print(f"📊 Found {len(all_high_severity_issues)} high-severity issues across all files")
    
    # Apply comment reviewer agent if there are high-severity issues
    if all_high_severity_issues:
        filtered_issues = await review_comments_with_agent(all_high_severity_issues)
    else:
        print(f"ℹ️  No high-severity issues to filter")
        return 

    # Post comments to GitLab
    print(f"\n{'=' * 60}")
    print(f"📤 Posting comments to GitLab MR...")
    print(f"{'=' * 60}\n")
    
    total_posted = 0
    total_failed = 0
    
    print(f"💬 Posting {len(filtered_issues)} comment(s) to GitLab...\n")
    
    for issue in filtered_issues:
        print(f"📝 Posting comment for {issue.file_name}:{issue.line}...")
        comment = format_single_issue(issue)
                
        if await post_gitlab_comment(
            gitlab_url, 
            project_path, 
            mr_id, 
            gitlab_token, 
            comment,
            file_path=issue.file_name,
            line_number=issue.line,
            base_sha=base_sha,
            start_sha=start_sha,
            head_sha=head_sha
        ):
            print(f"  ✅ Posted comment for line {issue.line}")
            total_posted += 1
        else:
            print(f"  ❌ Failed to post comment for line {issue.line}")
            total_failed += 1
    
    print(f"\n{'=' * 60}")
    print(f"✨ Review completed!")
    print(f"📤 Comments posted: {total_posted}")
    print(f"❌ Failed comments: {total_failed}")
    print(f"{'=' * 60}\n")


async def run_with_cleanup(mr_url: str):
    """Run the MR processing with proper cleanup in the same event loop."""
    try:
        await process_mr(mr_url)
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
    asyncio.run(run_with_cleanup(args.gitlab_mr))


if __name__ == "__main__":
    main()
