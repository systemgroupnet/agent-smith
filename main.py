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
import json
from fnmatch import fnmatch
from typing import Optional
from urllib.parse import quote, unquote, urlparse

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup proxy configuration for OpenAI API calls
# from core.config import setup_proxy
# setup_proxy()

# Import LangGraph workflow
from core.workflow.graph import review_workflow
from core.workflow.state import create_initial_state, StarterResponse

# Import MCP Toolkit Manager
from core.MCP.toolkit_manager import (
    initialize_mcp_toolkits,
    close_mcp_toolkits,
    get_mcp_toolkit_manager
)
from core.gitlab_api_client import get_gitlab_api_client


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


def should_exclude_file(file_path: str, exclusion_patterns: list) -> bool:
    """
    Check if a file should be excluded from review based on glob patterns.
    
    Args:
        file_path: Path to the file
        exclusion_patterns: List of glob patterns (e.g., ['*.md', 'docs/*', 'test/fixtures/*'])
        
    Returns:
        True if file should be excluded, False otherwise
    """
    if not exclusion_patterns:
        return False
    
    for pattern in exclusion_patterns:
        # Match against full path and filename
        if fnmatch(file_path, pattern) or fnmatch(file_path, f"*/{pattern}"):
            return True
    
    return False


async def call_gitlab_tool(tool_name: str, arguments: dict) -> Optional[dict]:
    """
    Helper function to call a GitLab MCP tool by name.
    
    Args:
        tool_name: Name of the GitLab tool to call
        arguments: Tool arguments
        
    Returns:
        Tool result or None if failed
    """
    try:
        manager = get_mcp_toolkit_manager()
        gitlab_tools = manager.get_gitlab_tools()
        
        # Find the tool
        tool = next((t for t in gitlab_tools if t.name == tool_name), None)
        if not tool:
            print(f"  ❌ GitLab tool '{tool_name}' not found")
            return None
        
        # Invoke the tool
        result = await tool.ainvoke(arguments)
        return result
        
    except asyncio.CancelledError as e:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        print(f"  ❌ Error calling GitLab tool {tool_name}: {e}")
        return None
    except Exception as e:
        print(f"  ❌ Error calling GitLab tool {tool_name}: {e}")
        return None


def _normalize_mcp_json_payload(result: object) -> Optional[dict]:
    """Normalize MCP responses that wrap JSON in text fields or strings."""
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            try:
                return json.loads(first["text"])
            except json.JSONDecodeError:
                return None
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None
    return result if isinstance(result, dict) else None


async def get_gitlab_mr_info(gitlab_url: str, project_path: str, mr_id: str, token: str) -> Optional[dict]:
    """
    Fetch merge request information from GitLab using MCP or REST API fallback.
    """
    # Try MCP first
    manager = get_mcp_toolkit_manager()
    if manager.is_initialized and manager.get_gitlab_tools():
        print(f"  🔍 [GitLab] Using MCP to fetch MR info...")
        result = await call_gitlab_tool("get_merge_request", {
            "project_id": project_path,
            "merge_request_iid": mr_id
        })
        if result:
            normalized = _normalize_mcp_json_payload(result)
            if normalized:
                return normalized
        print(f"  ⚠️  [GitLab] MCP call failed, falling back to REST API...")
    
    # Fallback to REST API
    print(f"  🔍 [GitLab] Using REST API to fetch MR info...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    return api_client.get_merge_request(project_path, mr_id)


async def get_gitlab_mr_changes(gitlab_url: str, project_path: str, mr_id: str, token: str) -> Optional[list]:
    """
    Fetch changed files and diffs from GitLab MR using MCP or REST API fallback.
    """
    # Try MCP first
    manager = get_mcp_toolkit_manager()
    if manager.is_initialized and manager.get_gitlab_tools():
        print(f"  🔍 [GitLab] Using MCP to fetch MR changes...")
        result = await call_gitlab_tool("get_merge_request_diffs", {
            "project_id": project_path,
            "merge_request_iid": mr_id
        })
        
        if result:
            # The result might be a dict with changes array or directly an array
            if isinstance(result, dict):
                changes = result.get("changes", result.get("diffs", []))
            else:
                changes = result if isinstance(result, list) else []
            if changes:
                return changes
        print(f"  ⚠️  [GitLab] MCP call failed, falling back to REST API...")
    
    # Fallback to REST API
    print(f"  🔍 [GitLab] Using REST API to fetch MR changes...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    return api_client.get_merge_request_changes(project_path, mr_id)


async def get_file_content(gitlab_url: str, project_path: str, file_path: str, branch: str, token: str) -> Optional[str]:
    """
    Fetch file content from GitLab repository using MCP or REST API fallback.
    """
    # Try MCP first
    manager = get_mcp_toolkit_manager()
    if manager.is_initialized and manager.get_gitlab_tools():
        print(f"  🔍 [GitLab] Using MCP to fetch file content...")
        result = await call_gitlab_tool("get_file_contents", {
            "project_id": project_path,
            "file_path": file_path,
            "ref": branch
        })
        
        if result:
            # The result might be a dict with content or directly the content
            if isinstance(result, dict):
                content = result.get("content", result.get("text", ""))
            else:
                content = str(result)
            if content:
                return content
        print(f"  ⚠️  [GitLab] MCP call failed, falling back to REST API...")
    
    # Fallback to REST API
    print(f"  🔍 [GitLab] Using REST API to fetch file content...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    return api_client.get_file_content(project_path, file_path, branch)


async def review_file_with_workflow(
    file_path: str,
    content: str,
    diff: str,
    merge_request_title: str,
    target_branch: str,
    source_branch: str,
    gitlab_token: Optional[str],
    gitlab_url: Optional[str],
    project_path: Optional[str]
) -> Optional[list]:
    """Review a file using LangGraph workflow."""
    
    async def _run_workflow() -> Optional[list]:
        """Inner function to run the workflow in isolation."""
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
            gitlab_url=gitlab_url,
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
    
    # Run workflow in a shielded task to isolate from MCP cancel scopes
    task = asyncio.create_task(_run_workflow())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as e:
        # If cancelled (e.g., by lingering MCP cancel scope), wait for task and clear cancellation
        cancel_msg = str(e)
        try:
            # Wait for the task to complete anyway
            result = await task
            # Clear cancellation state so the main loop can continue
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            return result
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            print(f"  ⚠️  Workflow cancelled while reviewing {file_path}: {cancel_msg}")
            return None
        except Exception as inner_e:
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
            print(f"  ⚠️  Workflow error after cancel for {file_path}: {inner_e}")
            return None
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


def get_severity(issue) -> str:
    """
    Extract severity from an issue (handles both dict and object formats).
    
    Args:
        issue: Review issue (dict or object)
    
    Returns:
        Severity string ("high", "medium", "low")
    """
    return issue.get("severity", "low") if isinstance(issue, dict) else issue.severity


async def review_comments_with_agent(issues: list) -> list:
    """
    Filter and rank high-severity comments using the comment reviewer agent.
    
    Args:
        issues: List of high-severity ReviewIssue objects/dicts
    
    Returns:
        List of top 10 most critical issues
    """
    if not issues:
        return []
    
    from core.agents.comment_reviewer_agent import create_comment_reviewer_agent
    from dto.general import ReviewIssue
    import json
    
    print(f"  🤖 Running comment reviewer agent on {len(issues)} high-severity issues...")
    
    try:
        # Create the agent
        agent = create_comment_reviewer_agent()
        
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
        result = await agent.run(input_text)
        
        print(f"  ✅ Agent selected {len(result.selected_issues)} critical issues")
        print(f"  📊 Filtered out {result.filtered_count} less critical issues")
        print(f"  💡 Reasoning: {result.reasoning}")
        
        # Return selected issues as dicts
        return [issue.model_dump() for issue in result.selected_issues]
        
    except Exception as e:
        print(f"  ⚠️  Error running comment reviewer agent: {e}")
        print(f"  ℹ️  Falling back to all {len(issues)} issues")
        return issues


def group_issues_by_file(issues: list) -> dict:
    """
    Group a flat list of issues by file_path.
    
    Args:
        issues: List of ReviewIssue objects/dicts
    
    Returns:
        Dictionary mapping file_path to list of issues: {file_path: [issues]}
    """
    grouped = {}
    for issue in issues:
        # Extract file_name (handles both dict and object formats)
        file_name = issue.get("file_name") if isinstance(issue, dict) else issue.file_name
        
        if file_name not in grouped:
            grouped[file_name] = []
        grouped[file_name].append(issue)
    
    return grouped


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
    Post a comment to GitLab MR using MCP or REST API fallback.
    """
    # Try MCP first
    manager = get_mcp_toolkit_manager()
    if manager.is_initialized and manager.get_gitlab_tools():
        print(f"  🔍 [GitLab] Using MCP to post comment...")
        
        try:
            if file_path and line_number:
                # Create a thread/discussion on a specific line
                result = await call_gitlab_tool("create_merge_request_thread", {
                    "project_id": project_path,
                    "merge_request_iid": mr_id,
                    "body": comment,
                    "position": {
                        "new_path": file_path,
                        "new_line": line_number
                    }
                })
            else:
                # General comment
                result = await call_gitlab_tool("create_merge_request_note", {
                    "project_id": project_path,
                    "merge_request_iid": mr_id,
                    "body": comment
                })
            
            if result:
                return True
            print(f"  ⚠️  [GitLab] MCP call failed, falling back to REST API...")
        except Exception as e:
            print(f"  ⚠️  [GitLab] MCP error: {e}, falling back to REST API...")
    
    # Fallback to REST API
    print(f"  🔍 [GitLab] Using REST API to post comment...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    
    try:
        if file_path and line_number:
            # Create a discussion with position (inline comment)
            position = {
                "base_sha": base_sha,
                "start_sha": start_sha,
                "head_sha": head_sha,
                "position_type": "text",
                "new_path": file_path,
                "new_line": line_number
            }
            result = api_client.create_merge_request_discussion(
                project_path, mr_id, comment, position
            )
        else:
            # General note
            result = api_client.create_merge_request_note(
                project_path, mr_id, comment
            )
        
        return result is not None
    except Exception as e:
        print(f"  ❌ Failed to post comment: {e}")
        return False


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
    normalized_mr = _normalize_mcp_json_payload(mr_data)
    if not normalized_mr and isinstance(mr_data, list):
        for item in mr_data:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                try:
                    normalized_mr = json.loads(item["text"])
                    break
                except json.JSONDecodeError:
                    continue
            if isinstance(item, dict) and ("title" in item or "source_branch" in item):
                normalized_mr = item
                break
    if not normalized_mr and isinstance(mr_data, dict):
        normalized_mr = mr_data
    if not normalized_mr:
        print("Error: Could not parse MR information.")
        sys.exit(1)

    mr_title = normalized_mr.get("title", "")
    source_branch = normalized_mr.get("source_branch", "")
    target_branch = normalized_mr.get("target_branch", "")
    
    # Get commit SHAs for posting line comments
    diff_refs = normalized_mr.get("diff_refs", {})
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

