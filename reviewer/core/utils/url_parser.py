"""
URL Parsing Utilities

This module provides functions for parsing GitLab merge request URLs.
"""

import re
from typing import Optional
from urllib.parse import unquote, urlparse


def parse_gitlab_mr_url(url: str) -> Optional[dict]:
    """
    Parse GitLab MR URL to extract project path and MR ID.
    
    Examples:
        https://gitlab.example.com/group/project/-/merge_requests/123
        https://gitlab.example.com/group/project/-/merge_requests/123/diffs
    
    Args:
        url: GitLab merge request URL
    
    Returns:
        Dictionary with domain, project_path, and mr_id, or None if parsing fails
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
    from fnmatch import fnmatch
    
    if not exclusion_patterns:
        return False
    
    for pattern in exclusion_patterns:
        # Match against full path and filename
        if fnmatch(file_path, pattern) or fnmatch(file_path, f"*/{pattern}"):
            return True
    
    return False
