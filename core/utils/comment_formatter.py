"""
Comment Formatting Utilities

This module provides functions for formatting review comments and issues.
"""

from typing import Any


def get_severity(issue: Any) -> str:
    """
    Extract severity from an issue (handles both dict and object formats).
    
    Args:
        issue: Review issue (dict or object)
    
    Returns:
        Severity string ("high", "medium", "low")
    """
    return issue.get("severity", "low") if isinstance(issue, dict) else issue.severity


def format_comment(issues: list, file_name: str, file_path: str) -> str:
    """Format review issues as a GitLab comment."""
    if not issues:
        return f"✅ **{file_name}** - No issues found!\n\n"
    
    comment = f"## 📝 Review for `{file_path}`\n\n"
    comment += f"Found **{len(issues)}** issue(s):\n\n"
    
    # Group by severity (handle both dict and object formats)
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
