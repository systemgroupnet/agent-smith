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
import re
import sys
from typing import Optional
from urllib.parse import quote, unquote, urlparse

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup proxy configuration for OpenAI API calls
from core.config import setup_proxy
setup_proxy()

# Import LangGraph workflow
from core.workflow.graph import review_workflow
from core.workflow.state import create_initial_state, StarterResponse

# Import GitLab MCP client (MCP ONLY - No fallback to direct API)
from core.MCP.gitlab_mcp_client import (
    get_gitlab_mr_info_via_mcp,
    get_gitlab_mr_changes_via_mcp,
    get_gitlab_file_content_via_mcp,
    post_gitlab_comment_via_mcp
)

def parse_gitlab_mr_url(url: str) -> Optional[dict]:
    """
    Parse GitLab MR URL to extract project path and MR ID.
    
    Examples:
        https://gitlab.example.com/group/project/-/merge_requests/123
        https://gitlab.example.com/group/project/-/merge_requests/123/diffs
    """
    # Decode URL-encoded characters (e.g., %2F -> /) to handle Windows/encoded URLs
    decoded_url = unquote(url)
    
    # Extract domain and path separately
    parsed = urlparse(decoded_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path
    
    # Extract project path and MR ID from the path
    # Path format: /group/project/-/merge_requests/123
    match = re.search(r'^/(.+?)/-/merge_requests/(\d+)', path)
    if not match:
        print(f"Error: Could not parse GitLab MR URL: {url}")
        print("Expected format: https://gitlab.example.com/group/project/-/merge_requests/123")
        return None
    
    project_path = match.group(1)  # group/project (without leading /)
    mr_id = match.group(2)
    
    return {
        "domain": domain,
        "project_path": project_path,
        "mr_id": mr_id
    }


async def get_gitlab_mr_info(gitlab_url: str, project_path: str, mr_id: str, token: str) -> Optional[dict]:
    """
    Fetch merge request information from GitLab using MCP.
    """
    print(f"  🔍 [GitLab] Using MCP to fetch MR info...")
    return await get_gitlab_mr_info_via_mcp(project_path, mr_id)


async def get_gitlab_mr_changes(gitlab_url: str, project_path: str, mr_id: str, token: str) -> Optional[list]:
    """
    Fetch changed files and diffs from GitLab MR using MCP.
    """
    print(f"  🔍 [GitLab] Using MCP to fetch MR changes...")
    return await get_gitlab_mr_changes_via_mcp(project_path, mr_id)


async def get_file_content(gitlab_url: str, project_path: str, file_path: str, branch: str, token: str) -> Optional[str]:
    """
    Fetch file content from GitLab repository using MCP.
    """
    print(f"  🔍 [GitLab] Using MCP to fetch file content...")
    return await get_gitlab_file_content_via_mcp(project_path, file_path, branch)


async def review_file_with_workflow(
    file_path: str,
    content: str,
    diff: str,
    merge_request_title: str,
    target_branch: str,
    source_branch: str,
    gitlab_token: Optional[str],
    gitlab_domain: Optional[str],
    project_path: Optional[str]
) -> Optional[list]:
    """Review a file using LangGraph workflow."""
    try:
        input_text = f"""
file_path: {file_path}
content:
{content}
diff:
{diff}
merge_request_title: {merge_request_title}
target_branch: {target_branch}
source_branch: {source_branch}
"""
        
        # Create initial state
        initial_state = create_initial_state(
            input_text=input_text,
            file_path=file_path,
            content=content,
            diff=diff,
            merge_request_title=merge_request_title,
            target_branch=target_branch,
            source_branch=source_branch,
            gitlab_token=gitlab_token,
            gitlab_domain=gitlab_domain,
            project_path=project_path
        )
        
        # Run workflow
        final_state = await review_workflow.ainvoke(initial_state)
        
        # Extract issues from output
        if final_state.get("error"):
            print(f"  ❌ Workflow error: {final_state['error']}")
            return []
        
        output = final_state.get("output")
        if output and isinstance(output, dict):
            issues = output.get("issues", [])
            return issues
        
        return []
    except Exception as e:
        print(f"Error reviewing file {file_path}: {e}")
        return None



def format_comment(issues: list, file_name: str, file_path: str) -> str:
    """Format review issues as a GitLab comment."""
    if not issues:
        return f"✅ **{file_name}** - No issues found!\n\n"
    
    comment = f"## 📝 Review for `{file_path}`\n\n"
    comment += f"Found **{len(issues)}** issue(s):\n\n"
    
    # Group by severity (handle both dict and object formats)
    def get_severity(issue):
        return issue.get("severity", "low") if isinstance(issue, dict) else issue.severity
    
    def get_line(issue):
        return issue.get("line", 1) if isinstance(issue, dict) else issue.line
    
    def get_problem(issue):
        return issue.get("problem", "") if isinstance(issue, dict) else issue.problem
    
    def get_suggestion(issue):
        return issue.get("suggestion", "") if isinstance(issue, dict) else issue.suggestion
    
    high_issues = [i for i in issues if get_severity(i) == "high"]
    medium_issues = [i for i in issues if get_severity(i) == "medium"]
    low_issues = [i for i in issues if get_severity(i) == "low"]
    
    if high_issues:
        comment += "### 🔴 High Priority Issues\n\n"
        for i, issue in enumerate(high_issues, 1):
            comment += f"**{i}. Line {get_line(issue)}**\n"
            comment += f"- **Problem**: {get_problem(issue)}\n"
            comment += f"- **Suggestion**: {get_suggestion(issue)}\n\n"
    
    if medium_issues:
        comment += "### 🟡 Medium Priority Issues\n\n"
        for i, issue in enumerate(medium_issues, 1):
            comment += f"**{i}. Line {get_line(issue)}**\n"
            comment += f"- **Problem**: {get_problem(issue)}\n"
            comment += f"- **Suggestion**: {get_suggestion(issue)}\n\n"
    
    if low_issues:
        comment += "### 🟢 Low Priority Issues\n\n"
        for i, issue in enumerate(low_issues, 1):
            comment += f"**{i}. Line {get_line(issue)}**\n"
            comment += f"- **Problem**: {get_problem(issue)}\n"
            comment += f"- **Suggestion**: {get_suggestion(issue)}\n\n"
    
    return comment


async def post_gitlab_comment(
    gitlab_url: str,
    project_path: str,
    mr_id: str,
    token: str,
    comment: str,
    file_path: Optional[str] = None,
    line_number: Optional[int] = None,
    base_sha: Optional[str] = None,
    start_sha: Optional[str] = None,
    head_sha: Optional[str] = None
) -> bool:
    """
    Post a comment to GitLab MR using MCP.
    """
    print(f"  🔍 [GitLab] Using MCP to post comment...")
    return await post_gitlab_comment_via_mcp(
        project_path, mr_id, comment, file_path, line_number
    )


async def process_mr(mr_url: str):
    """Main function to process a GitLab merge request."""
    print(f"🚀 Processing GitLab MR: {mr_url}\n")
    
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
    
    # Process each file
    gitlab_domain = gitlab_url.replace("https://", "").replace("http://", "")
    all_comments = []
    
    for change in changes:
        file_path = change.get("new_path") or change.get("old_path")
        if not file_path:
            continue
        
        # Skip deleted files
        if change.get("deleted_file", False):
            print(f"⏭️  Skipping deleted file: {file_path}")
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
            gitlab_domain=gitlab_domain,
            project_path=project_path
        )
        
        if issues is None:
            print(f"⚠️  Could not review {file_path}, skipping...")
            continue
        
        print(f"✅ Found {len(issues)} issue(s)")
        
        # Store issues with file path for later posting
        all_comments.append((file_path, issues))
    
    # Post comments to GitLab
    print(f"\n{'=' * 60}")
    print(f"📤 Posting comments to GitLab MR...")
    print(f"{'=' * 60}\n")
    
    total_posted = 0
    total_failed = 0
    
    for file_path, issues in all_comments:
        if not issues:
            # Post a general success comment for files with no issues
            comment = f"✅ **{file_path}** - No issues found!"
            print(f"💬 Posting summary for {file_path}...")
            if await post_gitlab_comment(gitlab_url, project_path, mr_id, gitlab_token, comment):
                print(f"✅ Comment posted successfully")
                total_posted += 1
            else:
                print(f"❌ Failed to post comment")
                total_failed += 1
        else:
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
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY not found in environment variables.")
        print("Please set OPENAI_API_KEY in your .env file.")
        sys.exit(1)
    
    # Run async main function
    asyncio.run(process_mr(args.gitlab_mr))


if __name__ == "__main__":
    main()

