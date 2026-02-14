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
import json
from typing import Optional

from dotenv import load_dotenv

# Load environment variables
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
from core.utils.file_reviewer import review_file_with_workflow, review_comments_with_agent
from core.utils.comment_formatter import get_severity, group_issues_by_file


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
    
    print(f"mr_data --->>>> {mr_data}")

    # Normalize MCP responses that may wrap JSON or return a list
    # normalized_mr = _normalize_mcp_json_payload(mr_data)
    # if not normalized_mr and isinstance(mr_data, list):
    #     for item in mr_data:
    #         if isinstance(item, dict) and isinstance(item.get("text"), str):
    #             try:
    #                 normalized_mr = json.loads(item["text"])
    #                 break
    #             except json.JSONDecodeError:
    #                 continue
    #         if isinstance(item, dict) and ("title" in item or "source_branch" in item):
    #             normalized_mr = item
    #             break
    # if not normalized_mr and isinstance(mr_data, dict):
    #     normalized_mr = mr_data
    # if not normalized_mr:
    #     print("Error: Could not parse MR information.")
    #     sys.exit(1)

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
    
    # Process each file
    all_comments = []
    
    for change in changes:
        file_path = change.get("new_path") or change.get("old_path")
        if not file_path:
            continue
        
        # Skip deleted files
        if change.get("deleted_file", False):
            print(f"⏭️  Skipping deleted file: {file_path}")
            continue
        
        # Skip excluded files based on patterns
        if should_exclude_file(file_path, exclusion_patterns):
            print(f"⏭️  Skipping excluded file: {file_path}")
            continue
        
        print(f"\n{'=' * 60}")
        print(f"📝 Processing: {file_path}")
        print(f"{'=' * 60}")
        
        # Get file content
        file_content = await get_file_content(gitlab_url, project_path, file_path, source_branch, gitlab_token)
        if not file_content:
            print(f"⚠️  Could not fetch content for {file_path}, skipping...")
            continue
        
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
            continue
        
        print(f"✅ Found {len(issues)} issue(s)")
        
        # Store issues with file path for later posting
        all_comments.append((file_path, issues))
    
    # Filter high-severity issues using comment reviewer agent
    print(f"\n{'=' * 60}")
    print(f"🔍 Filtering high-severity comments...")
    print(f"{'=' * 60}\n")
    
    # Collect all high-severity issues from all files
    all_high_severity_issues = []
    for file_path, issues in all_comments:
        high_issues = [issue for issue in issues if get_severity(issue) == "high"]
        all_high_severity_issues.extend(high_issues)
    
    print(f"📊 Found {len(all_high_severity_issues)} high-severity issues across all files")
    
    # Apply comment reviewer agent if there are high-severity issues
    if all_high_severity_issues:
        filtered_issues = await review_comments_with_agent(all_high_severity_issues)
    else:
        filtered_issues = all_high_severity_issues
        print(f"ℹ️  No high-severity issues to filter")
    
    # Group filtered issues back by file_path for posting
    filtered_comments_by_file = group_issues_by_file(filtered_issues)
    
    # Post comments to GitLab
    print(f"\n{'=' * 60}")
    print(f"📤 Posting comments to GitLab MR...")
    print(f"{'=' * 60}\n")
    
    total_posted = 0
    total_failed = 0
    
    for file_path, issues in filtered_comments_by_file.items():
        # Only post comments for files with issues
        if issues:
            # Post individual comments for each issue on the specific line
            print(f"💬 Posting {len(issues)} comment(s) for {file_path}...")
            for issue in issues:
                # Handle both dict and object formats
                severity = issue.get("severity", "low") if isinstance(issue, dict) else issue.severity
                line = issue.get("line", 1) if isinstance(issue, dict) else issue.line
                problem = issue.get("problem", "") if isinstance(issue, dict) else issue.problem
                suggestion = issue.get("suggestion", "") if isinstance(issue, dict) else issue.suggestion
                
                # Format severity emoji
                severity_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(severity, "⚪")
                
                # Format the comment for this specific issue
                comment = f"{severity_emoji} **{severity.upper()}** - Line {line}\n\n"
                comment += f"**Problem:** {problem}\n\n"
                comment += f"**Suggestion:** {suggestion}"
                
                # Post comment on the specific line
                if await post_gitlab_comment(
                    gitlab_url, 
                    project_path, 
                    mr_id, 
                    gitlab_token, 
                    comment,
                    file_path=file_path,
                    line_number=line,
                    base_sha=base_sha,
                    start_sha=start_sha,
                    head_sha=head_sha
                ):
                    print(f"  ✅ Posted comment for line {line}")
                    total_posted += 1
                else:
                    print(f"  ❌ Failed to post comment for line {line}")
                    total_failed += 1
    
    print(f"\n{'=' * 60}")
    print(f"✨ Review completed!")
    total_issues = sum(len(issues) for _, issues in all_comments)
    print(f"📊 Total issues found: {total_issues}")
    print(f"📤 Comments posted: {total_posted}")
    print(f"❌ Failed comments: {total_failed}")
    print(f"{'=' * 60}\n")


async def run_with_cleanup(mr_url: str):
    """Run the MR processing with proper cleanup in the same event loop."""
    try:
        await process_mr(mr_url)
    finally:
        # Clean up MCP sessions in the SAME event loop
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
