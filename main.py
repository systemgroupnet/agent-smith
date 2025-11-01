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

# Import reviewers
from starter_agent import starter_agent, StarterResponse
from service_reviewer import ServiceReviewerContext, run_combined_review
from sql_reviewer import SqlReviewerContext, sql_reviewer
from dataview_reviewer import DataviewReviewerContext, dataview_reviewer
from test_reviewer import TestReviewerContext, test_reviewer
from pbook_reviewer import PbookReviewerContext, pbook_reviewer
from agents import Runner, RunConfig
from scss_reviewer import ScssReviewerContext, scss_reviewer

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


def get_gitlab_mr_info(gitlab_url: str, project_path: str, mr_id: str, token: str) -> Optional[dict]:
    """Fetch merge request information from GitLab API."""
    headers = {"PRIVATE-TOKEN": token}
    encoded_project = quote(project_path, safe='')
    
    mr_url = f"{gitlab_url}/api/v4/projects/{encoded_project}/merge_requests/{mr_id}"
    
    try:
        response = requests.get(mr_url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching MR info: {e}")
        if hasattr(e.response, 'text'):
            print(f"Response: {e.response.text}")
        return None


def get_gitlab_mr_changes(gitlab_url: str, project_path: str, mr_id: str, token: str) -> Optional[list]:
    """Fetch changed files and diffs from GitLab MR."""
    headers = {"PRIVATE-TOKEN": token}
    encoded_project = quote(project_path, safe='')
    
    changes_url = f"{gitlab_url}/api/v4/projects/{encoded_project}/merge_requests/{mr_id}/changes"
    
    try:
        response = requests.get(changes_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("changes", [])
    except requests.exceptions.RequestException as e:
        print(f"Error fetching MR changes: {e}")
        if hasattr(e.response, 'text'):
            print(f"Response: {e.response.text}")
        return None


def get_file_content(gitlab_url: str, project_path: str, file_path: str, branch: str, token: str) -> Optional[str]:
    """Fetch file content from GitLab repository."""
    headers = {"PRIVATE-TOKEN": token}
    encoded_project = quote(project_path, safe='')
    encoded_file = quote(file_path, safe='')
    
    file_url = f"{gitlab_url}/api/v4/projects/{encoded_project}/repository/files/{encoded_file}/raw?ref={branch}"
    
    try:
        response = requests.get(file_url, headers=headers)
        if response.status_code == 200:
            return response.text
        return None
    except requests.exceptions.RequestException as e:
        print(f"Warning: Could not fetch file {file_path}: {e}")
        return None


async def classify_file(
    file_path: str,
    content: str,
    diff: str,
    merge_request_title: str,
    target_branch: str,
    source_branch: str
) -> Optional[StarterResponse]:
    """Classify a file using the starter agent."""
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
        
        result = await Runner.run(
            starter_agent,
            input=[{
                "role": "user",
                "content": [{"type": "input_text", "text": input_text}]
            }],
            context=None,
            run_config=RunConfig()
        )
        
        if result.final_output:
            return result.final_output
        return None
    except Exception as e:
        print(f"Error classifying file {file_path}: {e}")
        return None


async def review_service_file(
    classification: StarterResponse,
    diff: str,
    gitlab_token: Optional[str],
    gitlab_domain: Optional[str],
    project_path: Optional[str],
    source_branch: Optional[str]
) -> Optional[list]:
    """Review a service file."""
    try:
        context = ServiceReviewerContext(
            workflow_input_as_text=f"Review {classification.file_name}",
            content=classification.content,
            diff=diff or "",
            jira_task_number=classification.jira_task_number if classification.jira_task_number != "UNKNOWN" else None,
            package_name=classification.package_name,
            module_name=classification.module_name,
            file_name=classification.file_name,
            file_path=None,  # Can be extracted from classification if needed
            gitlab_token=gitlab_token,
            gitlab_domain=gitlab_domain.replace("https://", "").replace("http://", "") if gitlab_domain else None,
            source_branch=source_branch,
            project_path=project_path
        )
        
        result = await run_combined_review(context)
        return result.issues if result else []
    except Exception as e:
        print(f"Error reviewing service file {classification.file_name}: {e}")
        return []


async def review_scss_file(classification: StarterResponse, diff: str, gitlab_token: Optional[str], gitlab_domain: Optional[str], project_path: Optional[str], source_branch: Optional[str]) -> Optional[list]:
    """Review a scss file."""
    try:
        context = ScssReviewerContext(
            workflow_input_as_text=f"Review {classification.file_name}",
            content=classification.content,
            diff=diff or "",
            file_name=classification.file_name,
        )
        result = await Runner.run(
            scss_reviewer,
            input=[{
                "role": "user",
                "content": [{"type": "input_text", "text": "Review this scss file"}]
            }],
            context=context,
            run_config=RunConfig()
        )
        if result.final_output:
            return result.final_output.issues
        return []
    except Exception as e:
        print(f"Error reviewing scss file {classification.file_name}: {e}")
        return []

async def review_sql_file(classification: StarterResponse, diff: str) -> Optional[list]:
    """Review a SQL file."""
    try:
        context = SqlReviewerContext(
            workflow_input_as_text=f"Review {classification.file_name}",
            content=classification.content,
            diff=diff or "",
            file_name=classification.file_name
        )
        
        result = await Runner.run(
            sql_reviewer,
            input=[{
                "role": "user",
                "content": [{"type": "input_text", "text": "Review this SQL file"}]
            }],
            context=context,
            run_config=RunConfig()
        )
        
        if result.final_output:
            return result.final_output.issues
        return []
    except Exception as e:
        print(f"Error reviewing SQL file {classification.file_name}: {e}")
        return []


async def review_dataview_file(
    classification: StarterResponse,
    diff: str,
    gitlab_token: Optional[str],
    gitlab_domain: Optional[str],
    project_path: Optional[str],
    source_branch: Optional[str]
) -> Optional[list]:
    """Review a dataview file."""
    try:
        context = DataviewReviewerContext(
            workflow_input_as_text=f"Review {classification.file_name}",
            content=classification.content,
            diff=diff or "",
            jira_task_number=classification.jira_task_number if classification.jira_task_number != "UNKNOWN" else None,
            package_name=classification.package_name,
            module_name=classification.module_name,
            file_name=classification.file_name,
            gitlab_token=gitlab_token,
            gitlab_domain=gitlab_domain.replace("https://", "").replace("http://", "") if gitlab_domain else None,
            source_branch=source_branch,
            project_path=project_path
        )
        
        result = await Runner.run(
            dataview_reviewer,
            input=[{
                "role": "user",
                "content": [{"type": "input_text", "text": "Review this dataview file"}]
            }],
            context=context,
            run_config=RunConfig()
        )
        
        if result.final_output:
            return result.final_output.issues
        return []
    except Exception as e:
        print(f"Error reviewing dataview file {classification.file_name}: {e}")
        return []


async def review_test_file(classification: StarterResponse, diff: str) -> Optional[list]:
    """Review a test file."""
    try:
        context = TestReviewerContext(
            workflow_input_as_text=f"Review {classification.file_name}",
            content=classification.content,
            diff=diff or "",
            file_name=classification.file_name
        )
        
        result = await Runner.run(
            test_reviewer,
            input=[{
                "role": "user",
                "content": [{"type": "input_text", "text": "Review this test file"}]
            }],
            context=context,
            run_config=RunConfig()
        )
        
        if result.final_output:
            return result.final_output.issues
        return []
    except Exception as e:
        print(f"Error reviewing test file {classification.file_name}: {e}")
        return []


async def review_pbook_file(classification: StarterResponse, diff: str) -> Optional[list]:
    """Review a pbook file."""
    try:
        context = PbookReviewerContext(
            workflow_input_as_text=f"Review {classification.file_name}",
            content=classification.content,
            diff=diff or "",
            file_name=classification.file_name
        )
        
        result = await Runner.run(
            pbook_reviewer,
            input=[{
                "role": "user",
                "content": [{"type": "input_text", "text": "Review this pbook file"}]
            }],
            context=context,
            run_config=RunConfig()
        )
        
        if result.final_output:
            return result.final_output.issues
        return []
    except Exception as e:
        print(f"Error reviewing pbook file {classification.file_name}: {e}")
        return []


def format_comment(issues: list, file_name: str, file_path: str) -> str:
    """Format review issues as a GitLab comment."""
    if not issues:
        return f"✅ **{file_name}** - No issues found!\n\n"
    
    comment = f"## 📝 Review for `{file_path}`\n\n"
    comment += f"Found **{len(issues)}** issue(s):\n\n"
    
    # Group by severity
    high_issues = [i for i in issues if i.severity == "high"]
    medium_issues = [i for i in issues if i.severity == "medium"]
    low_issues = [i for i in issues if i.severity == "low"]
    
    if high_issues:
        comment += "### 🔴 High Priority Issues\n\n"
        for i, issue in enumerate(high_issues, 1):
            comment += f"**{i}. Line {issue.line}**\n"
            comment += f"- **Problem**: {issue.problem}\n"
            comment += f"- **Suggestion**: {issue.suggestion}\n\n"
    
    if medium_issues:
        comment += "### 🟡 Medium Priority Issues\n\n"
        for i, issue in enumerate(medium_issues, 1):
            comment += f"**{i}. Line {issue.line}**\n"
            comment += f"- **Problem**: {issue.problem}\n"
            comment += f"- **Suggestion**: {issue.suggestion}\n\n"
    
    if low_issues:
        comment += "### 🟢 Low Priority Issues\n\n"
        for i, issue in enumerate(low_issues, 1):
            comment += f"**{i}. Line {issue.line}**\n"
            comment += f"- **Problem**: {issue.problem}\n"
            comment += f"- **Suggestion**: {issue.suggestion}\n\n"
    
    return comment


def post_gitlab_comment(
    gitlab_url: str,
    project_path: str,
    mr_id: str,
    token: str,
    comment: str,
    file_path: Optional[str] = None,
    line_number: Optional[int] = None
) -> bool:
    """Post a comment to GitLab MR."""
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    encoded_project = quote(project_path, safe='')
    
    # Use discussions API for file-specific comments, or notes API for general comments
    if file_path and line_number:
        # Create a discussion on a specific line
        url = f"{gitlab_url}/api/v4/projects/{encoded_project}/merge_requests/{mr_id}/discussions"
        data = {
            "body": comment,
            "position": {
                "base_sha": None,  # GitLab will determine this
                "start_sha": None,
                "head_sha": None,
                "old_path": file_path,
                "new_path": file_path,
                "position_type": "text",
                "new_line": line_number
            }
        }
    else:
        # General comment
        url = f"{gitlab_url}/api/v4/projects/{encoded_project}/merge_requests/{mr_id}/notes"
        data = {"body": comment}
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error posting comment: {e}")
        if hasattr(e.response, 'text'):
            print(f"Response: {e.response.text}")
        return False


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
    mr_data = get_gitlab_mr_info(gitlab_url, project_path, mr_id, gitlab_token)
    if not mr_data:
        print("Error: Could not fetch MR information.")
        sys.exit(1)
    
    mr_title = mr_data.get("title", "")
    source_branch = mr_data.get("source_branch", "")
    target_branch = mr_data.get("target_branch", "")
    
    print(f"📄 Title: {mr_title}")
    print(f"🌿 Source Branch: {source_branch}")
    print(f"🎯 Target Branch: {target_branch}\n")
    
    # Fetch changes
    print("📥 Fetching changed files...")
    changes = get_gitlab_mr_changes(gitlab_url, project_path, mr_id, gitlab_token)
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
        file_content = get_file_content(gitlab_url, project_path, file_path, source_branch, gitlab_token)
        if not file_content:
            print(f"⚠️  Could not fetch content for {file_path}, skipping...")
            continue
        
        # Get diff
        diff = change.get("diff", "")


        classification = StarterResponse(
            file_name=file_path,
            file_type="unknown",
            package_name="unknown",
            module_name="unknown",
            content=file_content,
            diff=diff,
            target_branch=target_branch,
            source_branch=source_branch,
            jira_task_number="unknown")
        
        # Classify file
        print(f"🔍 Classifying file...")
        if file_path.endswith(".ts"):
            classification.file_type = "typescript"
        elif file_path.endswith(".html"):
            classification.file_type = "html"
        elif file_path.endswith(".scss"):
            print(f"🔍 Scss file detected: {file_path}")
            classification.file_type = "scss"
        else:
            classification = await classify_file(
                file_path,
                file_content,
                diff,
                mr_title,
                target_branch,
                source_branch
            )
            
        if not classification:
            print(f"⚠️  Could not classify {file_path}, skipping...")
            continue
        
        print(f"✅ Classified as: {classification.file_type}")
        print(f"   Module: {classification.module_name}, Package: {classification.package_name}")
        
        # Review file based on type
        issues = []
        print(f"🔍 Reviewing file...")

        if classification.file_type == "service":
            issues = await review_service_file(
                classification, diff, gitlab_token, gitlab_url, project_path, source_branch
            )
        elif classification.file_type == "sql":
            issues = await review_sql_file(classification, diff)
        elif classification.file_type == "dataview":
            issues = await review_dataview_file(
                classification, diff, gitlab_token, gitlab_url, project_path, source_branch
            )
        elif classification.file_type == "test":
            issues = await review_test_file(classification, diff)
        elif classification.file_type == "pbook":
            issues = await review_pbook_file(classification, diff)
        elif classification.file_type == "typescript":
            # Handle TypeScript files with starter agent
              issues = await review_typescript_file(
                classification, diff, gitlab_token, gitlab_url, project_path, source_branch
            )
        elif classification.file_type == "html":  
            # Handle HTML files with starter agent
            issues = await review_html_file(
                classification, diff, gitlab_token, gitlab_url, project_path, source_branch
            )
        elif classification.file_type == "scss":
            issues = await review_scss_file(
                classification, diff, gitlab_token, gitlab_url, project_path, source_branch
            )
        else:
            print(f"⚠️  Unknown file type: {classification.file_type}, skipping review...")
            continue
        
        print(f"✅ Found {len(issues)} issue(s)")
        
        # Format comment
        comment = format_comment(issues, classification.file_name, file_path)
        all_comments.append((file_path, comment, issues))
    
    # Post comments to GitLab
    print(f"\n{'=' * 60}")
    print(f"📤 Posting comments to GitLab MR...")
    print(f"{'=' * 60}\n")
    
    for file_path, comment, issues in all_comments:
        print(f"💬 Posting comment for {file_path}...")
        if post_gitlab_comment(gitlab_url, project_path, mr_id, gitlab_token, comment):
            print(f"✅ Comment posted successfully")
        else:
            print(f"❌ Failed to post comment")
    
    print(f"\n{'=' * 60}")
    print(f"✨ Review completed!")
    total_issues = sum(len(issues) for _, _, issues in all_comments)
    print(f"📊 Total issues found: {total_issues}")
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

