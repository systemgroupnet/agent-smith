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


def format_single_issue(issue: Any) -> str:
    """
    Format a single issue for GitLab comment.
    
    Args:
        issue: Review issue (dict or object)
    
    Returns:
        Formatted comment string with GitLab suggestion syntax
    """
    # Handle both dict and object formats
    problem = issue.get("problem", "") if isinstance(issue, dict) else issue.problem
    suggestion = issue.get("suggestion", "") if isinstance(issue, dict) else getattr(issue, "suggestion", "")
    
    # Format the comment
    comment = f"**Issue:**\n{problem}\n\n"
    
    # Only add suggestion section if it exists and is not empty
    if suggestion and suggestion.strip():
        comment += f"**Suggested Fix:**\n```suggestion:-0+0\n{suggestion.strip()}\n```"
    
    return comment


def extract_high_severity_issues(all_comments: list) -> list:
    """
    Extract high-severity issues from all comments.
    
    Args:
        all_comments: List of tuples (file_path, issues) where issues is a list of issue objects
        
    Returns:
        List of high-severity issues across all files
    """
    all_high_severity_issues = []
    for file_path, issues in all_comments:
        high_issues = [issue for issue in issues if get_severity(issue) == "high"]
        all_high_severity_issues.extend(high_issues)

    return all_high_severity_issues
