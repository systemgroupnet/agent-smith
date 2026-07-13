"""
GitLab Helper Functions

This module provides utility functions for interacting with GitLab API
using direct REST API calls, including the GitLab REST API client.
"""

import os
import re
import time
from typing import Optional, Dict, List, Tuple
from urllib.parse import quote
import requests


def _comment_retry_delays() -> List[float]:
    """Backoff schedule (seconds) for retrying comment POSTs. Default 1,2,5,10,30
    — override with GITLAB_COMMENT_RETRY_DELAYS (comma-separated). The number of
    entries is the number of retries after the first attempt."""
    raw = os.getenv("GITLAB_COMMENT_RETRY_DELAYS", "1,2,5,10,30")
    delays: List[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            delays.append(float(part))
        except ValueError:
            continue
    return delays or [1.0, 2.0, 5.0, 10.0, 30.0]


def _post_json_with_retry(
    url: str, headers: dict, data: dict, label: str,
    warnings_sink: Optional[List[dict]] = None,
) -> Tuple[Optional[Dict], Optional[str]]:
    """POST JSON, retrying transient failures (and 400s — GitLab occasionally
    returns a transient 400) on the configured backoff schedule. Never retries
    auth/not-found (401/403/404). Returns ``(parsed_json, None)`` on success or
    ``(None, error_message)`` after exhausting retries.

    When ``warnings_sink`` is provided, each retried attempt appends one dict
    ``{label, status, attempt, max_attempts, delay, message}`` to it so the caller
    can surface transient comment-post failures as analytics warnings."""
    delays = _comment_retry_delays()
    for attempt in range(len(delays) + 1):
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()
            return response.json(), None
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            # Retry network errors (no status), 400/409/429, and 5xx; a 401/403/404
            # is deterministic so we stop immediately.
            retryable = status is None or status in (400, 409, 429) or status >= 500
            if attempt < len(delays) and retryable:
                delay = delays[attempt]
                print(
                    f"  ⏳ [GitLab] {label} failed (HTTP {status}); retrying in {delay:g}s "
                    f"(attempt {attempt + 1}/{len(delays)})"
                )
                if warnings_sink is not None:
                    body_snip = getattr(getattr(e, "response", None), "text", "") or ""
                    warnings_sink.append({
                        "label": label,
                        "status": status,
                        "attempt": attempt + 1,
                        "max_attempts": len(delays) + 1,
                        "delay": delay,
                        "message": (
                            f"HTTP {status}: {e}"
                            + (f" — {body_snip[:300]}" if body_snip else "")
                        )[:500],
                    })
                time.sleep(delay)
                continue
            # Include the response body when present — GitLab's 400 explains why
            # (e.g. the line isn't part of the diff), which is what we want to debug.
            body = getattr(getattr(e, "response", None), "text", "") or ""
            msg = f"HTTP {status}: {e}" + (f" — {body[:500]}" if body else "")
            print(f"  ❌ [GitLab API] {label} failed: {msg}")
            return None, msg
    return None, f"{label} failed"


# Machine-readable token embedded in the review marker note on the MR. The
# agent uses it to remember the last commit it reviewed so subsequent pipeline
# runs only review the new commits instead of the whole MR diff.
MARKER_TOKEN = "code-review-agent:last-reviewed-sha"


__all__ = [
    'GitLabAPIClient',
    'get_gitlab_api_client',
    'get_gitlab_mr_info',
    'get_gitlab_mr_changes',
    'get_file_content',
    'post_gitlab_comment',
    'get_gitlab_compare',
    'get_review_marker',
    'upsert_review_marker',
    'MARKER_TOKEN',
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
        # Last POST error message (set by the create_* helpers), so callers can
        # surface why a comment failed without changing their return types.
        self.last_error: Optional[str] = None
    
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
    
    def create_merge_request_note(
        self, project_path: str, mr_iid: str, body: str,
        warnings_sink: Optional[List[dict]] = None,
    ) -> Optional[Dict]:
        """
        Create a general note/comment on the merge request.

        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            body: Comment text (markdown supported)
            warnings_sink: Optional list that collects one entry per retried attempt

        Returns:
            Created note data or None if failed
        """
        project_id = quote(project_path, safe="")
        url = self._build_api_url(f"/projects/{project_id}/merge_requests/{mr_iid}/notes")
        result, self.last_error = _post_json_with_retry(
            url, self.headers, {"body": body}, "create note", warnings_sink=warnings_sink
        )
        return result
    
    def update_merge_request_note(
        self, project_path: str, mr_iid: str, note_id: int, body: str
    ) -> Optional[Dict]:
        """
        Update an existing general note/comment on the merge request.

        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            note_id: ID of the note to update
            body: New comment text (markdown supported)

        Returns:
            Updated note data or None if failed
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(
                f"/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}"
            )

            data = {"body": body}
            response = requests.put(url, headers=self.headers, json=data, timeout=30)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            print(f"  ❌ [GitLab API] Error updating note: {e}")
            return None

    def list_merge_request_notes(self, project_path: str, mr_iid: str) -> Optional[List[Dict]]:
        """
        List notes/comments on the merge request (most recent first).

        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID

        Returns:
            List of note dictionaries or None if failed
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/merge_requests/{mr_iid}/notes")

            params = {"per_page": 100, "sort": "desc", "order_by": "updated_at"}
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            print(f"  ❌ [GitLab API] Error listing notes: {e}")
            return None

    def list_repository_tree(
        self,
        project_path: str,
        path: str = "",
        ref: Optional[str] = None,
        recursive: bool = False,
        per_page: int = 100,
    ) -> Optional[List[Dict]]:
        """
        List a directory in the repository tree.

        Args:
            project_path: Project path (e.g., "group/project")
            path: Directory path to list (empty = repo root)
            ref: Branch/tag/SHA to read from
            recursive: Whether to recurse into subdirectories
            per_page: Page size

        Returns:
            List of entries ({name, type: "tree"|"blob", path, ...}); [] when the
            path does not exist (404); None on an unexpected error.
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/repository/tree")
            entries: List[Dict] = []
            page = 1
            while True:
                params: Dict = {
                    "path": path,
                    "per_page": per_page,
                    "recursive": "true" if recursive else "false",
                    "page": page,
                }
                if ref:
                    params["ref"] = ref
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
                if response.status_code == 404:
                    return []  # path not present on this ref
                response.raise_for_status()
                batch = response.json()
                if not batch:
                    break
                entries.extend(batch)
                if len(batch) < per_page or page >= 50:  # last page / safety cap
                    break
                page += 1
            return entries
        except Exception as e:
            print(f"  ⚠️  [GitLab API] Error listing tree '{path}': {e}")
            return None

    def compare_commits(self, project_path: str, from_sha: str, to_sha: str) -> Optional[Dict]:
        """
        Compare two commits/refs and return the diff between them.

        Args:
            project_path: Project path (e.g., "group/project")
            from_sha: Base commit SHA (the previously reviewed commit)
            to_sha: Target commit SHA (the current MR head)

        Returns:
            Compare result dictionary (with a "diffs" list) or None if failed
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/repository/compare")

            params = {"from": from_sha, "to": to_sha, "unidiff": "true"}
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            print(f"  ❌ [GitLab API] Error comparing commits: {e}")
            return None

    def create_merge_request_discussion(
        self,
        project_path: str,
        mr_iid: str,
        body: str,
        position: Optional[Dict] = None,
        warnings_sink: Optional[List[dict]] = None,
    ) -> Optional[Dict]:
        """
        Create a discussion/thread on the merge request.

        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            body: Comment text (markdown supported)
            position: Position object for inline comments (optional)
            warnings_sink: Optional list that collects one entry per retried attempt

        Returns:
            Created discussion data or None if failed
        """
        project_id = quote(project_path, safe="")
        url = self._build_api_url(f"/projects/{project_id}/merge_requests/{mr_iid}/discussions")
        data: Dict = {"body": body}
        if position:
            data["position"] = position
        result, self.last_error = _post_json_with_retry(
            url, self.headers, data, "create discussion", warnings_sink=warnings_sink
        )
        return result


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
    head_sha: Optional[str] = None,
    warnings_sink: Optional[List[dict]] = None,
) -> Optional[str]:
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
        warnings_sink: Optional list; each retried POST attempt appends one entry
            (so the caller can record transient failures as analytics warnings)

    Returns:
        None if the comment was posted (inline or via the general-note fallback),
        otherwise an error message string describing why it failed.
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
                project_path, mr_id, comment, position, warnings_sink=warnings_sink
            )
            if result is not None:
                return None
            # The inline post kept failing — usually the line isn't part of the
            # MR diff (a context line), which is deterministic, so retries won't
            # help. Fall back to a general MR note (prefixed with the location)
            # so the finding is preserved rather than dropped.
            inline_error = api_client.last_error
            print(f"  ↩️  [GitLab] Inline post failed for {file_path}:{line_number}; posting as a general note instead")
            located = f"**`{file_path}:{line_number}`**\n\n{comment}"
            result = api_client.create_merge_request_note(
                project_path, mr_id, located, warnings_sink=warnings_sink
            )
            if result is not None:
                return None
            return f"inline post failed ({inline_error}); fallback note also failed ({api_client.last_error})"

        # General note
        result = api_client.create_merge_request_note(
            project_path, mr_id, comment, warnings_sink=warnings_sink
        )
        return None if result is not None else (api_client.last_error or "note post failed")
    except Exception as e:
        print(f"  ❌ Failed to post comment: {e}")
        return f"{type(e).__name__}: {e}"


async def get_gitlab_compare(
    gitlab_url: str, project_path: str, from_sha: str, to_sha: str, token: str
) -> Optional[list]:
    """
    Fetch the incremental diff between two commits via the GitLab compare API.

    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        from_sha: Base commit SHA (the previously reviewed commit)
        to_sha: Target commit SHA (the current MR head)
        token: GitLab access token

    Returns:
        List of changed files with diffs (same shape as MR changes) or None if failed.
    """
    print(f"  🔍 [GitLab] Comparing {from_sha[:8]}..{to_sha[:8]} via REST API...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    result = api_client.compare_commits(project_path, from_sha, to_sha)
    if result is None:
        return None
    return result.get("diffs", [])


async def get_review_marker(
    gitlab_url: str, project_path: str, mr_id: str, token: str
) -> Tuple[Optional[str], Optional[int]]:
    """
    Find the agent's review marker note on the MR and read the last-reviewed SHA.

    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        mr_id: Merge request IID
        token: GitLab access token

    Returns:
        Tuple of (last_reviewed_sha, note_id). Both are None when no marker exists.
    """
    print(f"  🔍 [GitLab] Looking up review marker note...")
    api_client = get_gitlab_api_client(gitlab_url, token)
    notes = api_client.list_merge_request_notes(project_path, mr_id)
    if not notes:
        return None, None

    pattern = re.compile(re.escape(MARKER_TOKEN) + r"=([0-9a-fA-F]+)")
    for note in notes:
        body = note.get("body", "") or ""
        match = pattern.search(body)
        if match:
            return match.group(1), note.get("id")
    return None, None


async def upsert_review_marker(
    gitlab_url: str,
    project_path: str,
    mr_id: str,
    token: str,
    sha: str,
    note_id: Optional[int] = None,
) -> bool:
    """
    Create or update the review marker note recording the last-reviewed SHA.

    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        mr_id: Merge request IID
        token: GitLab access token
        sha: The commit SHA that was just reviewed (the current MR head)
        note_id: Existing marker note id to update in place; None to create a new note.

    Returns:
        True if the marker was written successfully, False otherwise.
    """
    api_client = get_gitlab_api_client(gitlab_url, token)
    body = (
        f"<!-- {MARKER_TOKEN}={sha} -->\n"
        f"🤖 Code Review Agent reviewed up to commit `{sha[:8]}`."
    )
    try:
        if note_id is not None:
            result = api_client.update_merge_request_note(project_path, mr_id, note_id, body)
        else:
            result = api_client.create_merge_request_note(project_path, mr_id, body)
        return result is not None
    except Exception as e:
        print(f"  ❌ Failed to write review marker: {e}")
        return False
