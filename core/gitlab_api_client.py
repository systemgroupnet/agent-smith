"""
GitLab REST API Client - Fallback for MCP

This module provides direct GitLab REST API access as a fallback
when MCP is unavailable or disabled.
"""

import os
import base64
from typing import Optional, Dict, List
from urllib.parse import quote
import requests


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

