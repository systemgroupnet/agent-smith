"""
GitLab Helper Functions

This module provides utility functions for interacting with GitLab API
using direct REST API calls, including the GitLab REST API client.
"""

from typing import Optional, Dict, List
from urllib.parse import quote
import requests


__all__ = [
    'GitLabAPIClient',
    'get_gitlab_api_client',
    'get_gitlab_mr_info',
    'get_gitlab_mr_changes',
    'get_file_content',
    'post_gitlab_comment'
]


class GitLabAPIClient:
    """GitLab REST API client for merge request operations."""
    
    def __init__(self, gitlab_url: str, token: str):
        """
        Initialize GitLab API client.
        
        Args:
            gitlab_url: GitLab instance URL (e.g., https://gitlab.example.com)
            token: Personal access token
        """
        self.gitlab_url = gitlab_url.rstrip("/")
        self.token = token
        self.headers = {
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json"
        }
    
    def _build_api_url(self, endpoint: str) -> str:
        """Build full API URL."""
        return f"{self.gitlab_url}/api/v4/{endpoint.lstrip('/')}"
    
    def get_merge_request(self, project_path: str, mr_iid: str) -> Optional[Dict]:
        """
        Get merge request information.
        
        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            
        Returns:
            MR data dictionary or None if failed
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/merge_requests/{mr_iid}")
            
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            
            return response.json()
            
        except Exception as e:
            print(f"  ❌ [GitLab API] Error fetching MR: {e}")
            return None
    
    def get_merge_request_changes(self, project_path: str, mr_iid: str) -> Optional[List[Dict]]:
        """
        Get merge request changes (diffs).
        
        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            
        Returns:
            List of changed files with diffs or None if failed
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/merge_requests/{mr_iid}/changes")
            
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            return data.get("changes", [])
            
        except Exception as e:
            print(f"  ❌ [GitLab API] Error fetching MR changes: {e}")
            return None
    
    def get_file_content(self, project_path: str, file_path: str, ref: str) -> Optional[str]:
        """
        Get file content from repository.
        
        Args:
            project_path: Project path (e.g., "group/project")
            file_path: Path to file in repository
            ref: Branch, tag, or commit SHA
            
        Returns:
            File content as string or None if failed
        """
        try:
            project_id = quote(project_path, safe="")
            file_path_encoded = quote(file_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/repository/files/{file_path_encoded}/raw")
            
            params = {"ref": ref}
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()
            
            return response.text
            
        except Exception as e:
            print(f"  ❌ [GitLab API] Error fetching file content: {e}")
            return None
    
    def create_merge_request_note(self, project_path: str, mr_iid: str, body: str) -> Optional[Dict]:
        """
        Create a general note/comment on the merge request.
        
        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            body: Comment text (markdown supported)
            
        Returns:
            Created note data or None if failed
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/merge_requests/{mr_iid}/notes")
            
            data = {"body": body}
            response = requests.post(url, headers=self.headers, json=data, timeout=30)
            response.raise_for_status()
            
            return response.json()
            
        except Exception as e:
            print(f"  ❌ [GitLab API] Error creating note: {e}")
            return None
    
    def create_merge_request_discussion(
        self,
        project_path: str,
        mr_iid: str,
        body: str,
        position: Optional[Dict] = None
    ) -> Optional[Dict]:
        """
        Create a discussion/thread on the merge request.
        
        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            body: Comment text (markdown supported)
            position: Position object for inline comments (optional)
            
        Returns:
            Created discussion data or None if failed
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/merge_requests/{mr_iid}/discussions")
            
            data = {"body": body}
            if position:
                data["position"] = position
            
            response = requests.post(url, headers=self.headers, json=data, timeout=30)
            response.raise_for_status()
            
            return response.json()
            
        except Exception as e:
            print(f"  ❌ [GitLab API] Error creating discussion: {e}")
            return None


def get_gitlab_api_client(gitlab_url: str, token: str) -> GitLabAPIClient:
    """
    Get GitLab API client instance.
    
    Args:
        gitlab_url: GitLab instance URL
        token: Personal access token
        
    Returns:
        GitLabAPIClient instance
    """
    return GitLabAPIClient(gitlab_url, token)


async def get_gitlab_mr_info(gitlab_url: str, project_path: str, mr_id: str, token: str) -> Optional[dict]:
    """
    Fetch merge request information from GitLab using REST API.
    
    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        mr_id: Merge request IID
        token: GitLab access token
        
    Returns:
        MR data dictionary or None if failed
    """
    print(f"  🔍 [GitLab] Fetching MR info via REST API...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    return api_client.get_merge_request(project_path, mr_id)


async def get_gitlab_mr_changes(gitlab_url: str, project_path: str, mr_id: str, token: str) -> Optional[list]:
    """
    Fetch changed files and diffs from GitLab MR using REST API.
    
    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        mr_id: Merge request IID
        token: GitLab access token
        
    Returns:
        List of changed files with diffs or None if failed
    """
    print(f"  🔍 [GitLab] Fetching MR changes via REST API...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    return api_client.get_merge_request_changes(project_path, mr_id)


async def get_file_content(gitlab_url: str, project_path: str, file_path: str, branch: str, token: str) -> Optional[str]:
    """
    Fetch file content from GitLab repository using REST API.
    
    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        file_path: Path to file in repository
        branch: Branch name or commit SHA
        token: GitLab access token
        
    Returns:
        File content as string or None if failed
    """
    print(f"  🔍 [GitLab] Fetching file content via REST API...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    return api_client.get_file_content(project_path, file_path, branch)


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
    Post a comment to GitLab MR using REST API.
    
    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        mr_id: Merge request IID
        token: GitLab access token
        comment: Comment text (markdown supported)
        file_path: Optional file path for inline comments
        line_number: Optional line number for inline comments
        base_sha: Base commit SHA (required for inline comments)
        start_sha: Start commit SHA (required for inline comments)
        head_sha: Head commit SHA (required for inline comments)
        
    Returns:
        True if comment posted successfully, False otherwise
    """
    print(f"  🔍 [GitLab] Posting comment via REST API...")
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
