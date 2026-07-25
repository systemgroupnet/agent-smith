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


def _is_positional_400(body: str) -> bool:
    """True when a GitLab 400 body is the deterministic inline-position error.

    Creating an inline (positioned) discussion 400s with a ``line_code`` error
    (``can't be blank`` / ``must be a valid line code``) when the target line
    isn't a valid position in the MR diff. That won't change on retry, so we fail
    fast and let the caller fall back to a general note instead of burning the
    whole backoff schedule."""
    b = (body or "").lower()
    return "line_code" in b or "must be a valid line code" in b


def _post_json_with_retry(
    url: str, headers: dict, data: dict, label: str,
    warnings_sink: Optional[List[dict]] = None,
) -> Tuple[Optional[Dict], Optional[str]]:
    """POST JSON, retrying transient failures on the configured backoff schedule.
    Returns ``(parsed_json, None)`` on success or ``(None, error_message)`` after
    exhausting retries.

    Retries network errors (no status), 409/429, 5xx, and *transient* 400s. Does
    NOT retry auth/not-found (401/403/404) or a deterministic inline-position 400
    (blank/invalid ``line_code`` — see :func:`_is_positional_400`); those fail
    fast so the caller can fall back immediately.

    When ``warnings_sink`` is provided, each retried attempt appends one preformatted
    ``{stage, message}`` dict to it so the caller can surface transient comment-post
    failures as analytics warnings."""
    delays = _comment_retry_delays()
    for attempt in range(len(delays) + 1):
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()
            return response.json(), None
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            # Read the body once — GitLab explains a 400 here (e.g. the line isn't
            # part of the diff), which decides retryability and aids debugging.
            body = getattr(getattr(e, "response", None), "text", "") or ""
            # A positional 400 (invalid/blank line_code) is deterministic — treat
            # it like 401/403/404 (no retry). Other 400s may be transient.
            positional_400 = status == 400 and _is_positional_400(body)
            retryable = (
                status is None
                or status in (409, 429)
                or (status == 400 and not positional_400)
                or (status is not None and status >= 500)
            )
            if attempt < len(delays) and retryable:
                delay = delays[attempt]
                print(
                    f"  ⏳ [GitLab] {label} failed (HTTP {status}); retrying in {delay:g}s "
                    f"(attempt {attempt + 1}/{len(delays)})"
                )
                if warnings_sink is not None:
                    warnings_sink.append({
                        "stage": "comment_post_retry",
                        "message": (
                            f"{label} attempt {attempt + 1}/{len(delays) + 1} failed "
                            f"(HTTP {status}); retried after {delay:g}s: HTTP {status}: {e}"
                            + (f" — {body[:300]}" if body else "")
                        )[:600],
                    })
                time.sleep(delay)
                continue
            msg = f"HTTP {status}: {e}" + (f" — {body[:500]}" if body else "")
            print(f"  ❌ [GitLab API] {label} failed: {msg}")
            return None, msg
    return None, f"{label} failed"


def build_diff_position_map(diff_text: str) -> Dict[int, Optional[int]]:
    """Parse a unified diff into the new-file lines GitLab accepts as inline
    positions.

    Returns ``{new_line: old_line}`` where ``old_line`` is ``None`` for an added
    line (new side only) and an int for a context/unchanged line. GitLab requires
    **both** sides in the position for a context line — omitting ``old_line`` there
    is what triggers the ``line_code can't be blank`` 400 we were hitting. Lines
    that are not in any hunk are absent from the map: they are not valid inline
    positions at all (comment must fall back to a general note).
    """
    positions: Dict[int, Optional[int]] = {}
    if not diff_text:
        return positions

    old_ln = new_ln = 0
    in_hunk = False
    for raw in diff_text.split("\n"):
        if raw.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                old_ln, new_ln = int(m.group(1)), int(m.group(2))
                in_hunk = True
            else:
                in_hunk = False
            continue
        if not in_hunk or raw == "":
            continue
        tag = raw[0]
        if tag == "+":            # added line — valid on the new side alone
            positions[new_ln] = None
            new_ln += 1
        elif tag == "-":          # removed line — old side only; skip
            old_ln += 1
        elif tag == "\\":         # "\ No newline at end of file"
            continue
        else:                     # context line — needs both old & new
            positions[new_ln] = old_ln
            old_ln += 1
            new_ln += 1
    return positions


def resolve_inline_position(
    file_name: Optional[str],
    line,
    position_maps: Dict[str, Dict[int, Optional[int]]],
) -> Tuple[Optional[str], Optional[int], bool, bool]:
    """Map a review issue (file_name, line) to a valid GitLab inline position.

    The model's ``file_name`` may be a bare name or a full path, and its ``line``
    may not be a line GitLab accepts inline. Resolve against the authoritative MR
    diff (``position_maps``: real-new-path -> {new_line: old_line|None}, built with
    :func:`build_diff_position_map`):

    * Match the file to exactly one changed path (exact, then unique suffix, then
      unique basename; ambiguous matches are treated as unresolved).
    * If the line is a valid position in that file's diff, return it with the
      correct ``old_line`` (int for a context line, None for an added line).

    Returns ``(real_path, old_line, in_diff, file_in_mr)``:

    * ``in_diff`` False means the line is not a valid inline position (post a
      general note, not inline).
    * ``file_in_mr`` False means ``file_name`` matched **no** changed path in the
      MR at all. Each reviewer reviews a real changed file but self-reports
      ``file_name`` per issue and can fabricate it (e.g. a Go MR yielding a bogus
      Python ``src/core/engine.py`` finding), so a file that isn't in the MR is a
      reliable hallucination signal — the caller drops it instead of posting.
    """
    if not file_name:
        return file_name, None, False, False

    pos_map = position_maps.get(file_name)
    real_path = file_name
    if pos_map is None:
        # Not an exact changed-path; try unique suffix / basename matches.
        base = file_name.rsplit("/", 1)[-1]
        matches = [
            p for p in position_maps
            if p == file_name or p.endswith("/" + file_name) or p.rsplit("/", 1)[-1] == base
        ]
        if len(matches) == 1:  # unique — safe to use; ambiguous -> give up
            real_path = matches[0]
            pos_map = position_maps[real_path]

    # ``pos_map is None`` iff nothing matched: the file is not part of this MR.
    # (A real changed file with no parseable hunks maps to {} — present, empty —
    # so it is still recognised as in-MR and posted as a general note.)
    file_in_mr = pos_map is not None
    if not file_in_mr:
        return real_path, None, False, False

    if line is None:
        return real_path, None, False, True

    try:
        line_int = int(line)
    except (TypeError, ValueError):
        return real_path, None, False, True

    if line_int in pos_map:
        return real_path, pos_map[line_int], True, True
    return real_path, None, False, True


# Machine-readable token embedded in the review marker note on the MR. The
# agent uses it to remember the last commit it reviewed so subsequent pipeline
# runs only review the new commits instead of the whole MR diff.
MARKER_TOKEN = "code-review-agent:last-reviewed-sha"

# Award-emoji names (GitLab names, no colons) seeded on every posted review
# comment as a 1..5 rating scale — :one: .. :five:. A human clicking one of
# them awards the same emoji as themselves, which GitLab delivers to the
# collector's webhook endpoint as that comment's score. Order matters: GitLab
# renders reactions in first-award order, so seeding 1→5 keeps the scale sorted.
SCORE_REACTION_EMOJIS = ("one", "two", "three", "four", "five")


__all__ = [
    'GitLabAPIClient',
    'get_gitlab_api_client',
    'get_gitlab_mr_info',
    'get_gitlab_mr_changes',
    'get_file_content',
    'post_gitlab_comment',
    'add_note_award_emojis',
    'build_diff_position_map',
    'resolve_inline_position',
    'get_gitlab_compare',
    'get_review_marker',
    'upsert_review_marker',
    'add_mr_label',
    'MARKER_TOKEN',
    'SCORE_REACTION_EMOJIS',
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

    def add_merge_request_labels(
        self, project_path: str, mr_iid: str, labels: str
    ) -> Optional[Dict]:
        """
        Add one or more labels (comma-separated) to the merge request.

        Uses GitLab's ``add_labels`` param, which is **incremental** — it does not
        touch labels already on the MR — and **auto-creates** any referenced label
        the project doesn't have yet, so no repo-side setup is required.

        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            labels: Comma-separated label name(s) to add

        Returns:
            Updated MR data or None if failed (sets ``last_error``).
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(f"/projects/{project_id}/merge_requests/{mr_iid}")
            response = requests.put(
                url, headers=self.headers, json={"add_labels": labels}, timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"  ❌ [GitLab API] Error adding MR labels: {e}")
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

    def create_mr_note_award_emoji(
        self, project_path: str, mr_iid: str, note_id: int, name: str
    ) -> Optional[Dict]:
        """
        Award one emoji reaction on an MR note/comment.

        Args:
            project_path: Project path (e.g., "group/project")
            mr_iid: Merge request IID
            note_id: ID of the note to react to
            name: Emoji name without colons (e.g. "one", "thumbsup")

        Returns:
            Created award-emoji data or None if failed. Single attempt, no
            retry — a reaction only seeds the rating scale, so losing one
            degrades the evaluation UI but must never slow down the review.
        """
        try:
            project_id = quote(project_path, safe="")
            url = self._build_api_url(
                f"/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}/award_emoji"
            )
            response = requests.post(url, headers=self.headers, json={"name": name}, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"  ❌ [GitLab API] Error awarding :{name}: on note {note_id}: {e}")
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


def _posted_from_discussion(result: Dict, comment_type: str = "inline") -> Dict:
    """Extract the posted-comment identity from a created-discussion response.

    A discussion's JSON is ``{"id": <discussion_id>, "notes": [{...}]}`` — the
    note id (what emoji-award webhooks report as ``awardable_id``) lives on the
    first (only) note of the new discussion. ``comment_type`` distinguishes a
    positioned inline discussion from an unpositioned general one; both are
    threads, so both carry a discussion id.
    """
    notes = result.get("notes") or []
    first = notes[0] if notes and isinstance(notes[0], dict) else {}
    return {
        "note_id": first.get("id"),
        "discussion_id": result.get("id"),
        "comment_type": comment_type,
    }


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
    old_line: Optional[int] = None,
    old_path: Optional[str] = None,
    warnings_sink: Optional[List[dict]] = None,
) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Post a comment to GitLab MR using REST API.

    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        mr_id: Merge request IID
        token: GitLab access token
        comment: Comment text (markdown supported)
        file_path: Optional file path for inline comments (the NEW-side path)
        line_number: Optional NEW-side line number for inline comments
        base_sha: Base commit SHA (required for inline comments)
        start_sha: Start commit SHA (required for inline comments)
        head_sha: Head commit SHA (required for inline comments)
        old_line: OLD-side line number — REQUIRED by GitLab to position an inline
            comment on a context (unchanged) line; omit/None for an added line
        old_path: OLD-side path (defaults to file_path); only used with old_line
        warnings_sink: Optional list; each retried POST attempt appends one entry
            (so the caller can record transient failures as analytics warnings)

    Returns:
        ``(posted, error)``: on success ``posted`` identifies the created comment
        — ``{"note_id", "discussion_id", "comment_type": "inline"|"note"}`` (the
        note id is what reaction seeding and the collector's emoji webhook key
        on) — and ``error`` is None. On failure ``posted`` is None and ``error``
        describes why (inline or via the general-note fallback).
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
                "new_line": line_number,
            }
            # A context (unchanged) line needs BOTH sides or GitLab rejects the
            # note with a blank line_code; an added line uses the new side alone.
            if old_line is not None:
                position["old_path"] = old_path or file_path
                position["old_line"] = old_line
            result = api_client.create_merge_request_discussion(
                project_path, mr_id, comment, position, warnings_sink=warnings_sink
            )
            if result is not None:
                return _posted_from_discussion(result), None
            # The inline post kept failing — usually the line isn't part of the
            # MR diff (a context line), which is deterministic, so retries won't
            # help. Fall back to a general MR note (prefixed with the location)
            # so the finding is preserved rather than dropped.
            inline_error = api_client.last_error
            print(f"  ↩️  [GitLab] Inline post failed for {file_path}:{line_number}; posting as a general note instead")
            located = f"**`{file_path}:{line_number}`**\n\n{comment}"
            result = api_client.create_merge_request_discussion(
                project_path, mr_id, located, warnings_sink=warnings_sink
            )
            if result is not None:
                # Record the fall-back once (not the raw inline retries) so the
                # collector still shows how often inline positioning fails without
                # the per-attempt noise.
                if warnings_sink is not None:
                    warnings_sink.append({
                        "stage": "comment_post_fallback",
                        "message": (
                            f"inline comment failed for {file_path}:{line_number} "
                            f"({inline_error}); posted as a general MR note instead"
                        )[:600],
                    })
                return _posted_from_discussion(result, "note"), None
            return None, f"inline post failed ({inline_error}); fallback note also failed ({api_client.last_error})"

        # General note. Posted as an *unpositioned discussion* rather than a plain
        # note: both render as a comment on the MR, but a discussion is a thread
        # with a stable discussion_id, which is what lets the collector recognise a
        # human's reply to this comment (GitLab's note API returns no discussion_id,
        # so a plain note's replies could never be tied back to us).
        result = api_client.create_merge_request_discussion(
            project_path, mr_id, comment, warnings_sink=warnings_sink
        )
        if result is not None:
            return _posted_from_discussion(result, "note"), None
        return None, (api_client.last_error or "note post failed")
    except Exception as e:
        print(f"  ❌ Failed to post comment: {e}")
        return None, f"{type(e).__name__}: {e}"


async def add_note_award_emojis(
    gitlab_url: str,
    project_path: str,
    mr_id: str,
    token: str,
    note_id: int,
    names=SCORE_REACTION_EMOJIS,
) -> List[str]:
    """Seed award-emoji reactions on an MR note, in order (the rating scale).

    Best-effort: each emoji is a single POST with no retry — a missing reaction
    only degrades the evaluation UI, never the review. Returns the emoji names
    that failed so the caller can record them as analytics warnings.
    """
    api_client = get_gitlab_api_client(gitlab_url, token)
    failed: List[str] = []
    for name in names:
        if api_client.create_mr_note_award_emoji(project_path, mr_id, note_id, name) is None:
            failed.append(name)
    return failed


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


async def add_mr_label(
    gitlab_url: str,
    project_path: str,
    mr_id: str,
    token: str,
    label: str,
) -> bool:
    """
    Add a label to the merge request (non-fatal).

    GitLab's ``add_labels`` is incremental (won't remove labels already on the MR)
    and auto-creates the label if the project doesn't have it, so this needs no
    repo-side setup and behaves the same for every reviewed repo. Never raises.

    Args:
        gitlab_url: GitLab instance URL
        project_path: Project path (e.g., "group/project")
        mr_id: Merge request IID
        token: GitLab access token
        label: Label name to add

    Returns:
        True if the label was added successfully, False otherwise.
    """
    if not label:
        return False
    api_client = get_gitlab_api_client(gitlab_url, token)
    try:
        result = api_client.add_merge_request_labels(project_path, mr_id, label)
        return result is not None
    except Exception as e:
        print(f"  ❌ Failed to add MR label {label!r}: {e}")
        return False
