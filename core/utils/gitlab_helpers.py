"""
GitLab Helper Functions

This module provides utility functions for interacting with GitLab API
using MCP tools or REST API fallback.
"""

import json
from typing import Optional
from core.MCP.toolkit_manager import get_mcp_toolkit_manager
from core.gitlab_api_client import get_gitlab_api_client


__all__ = [
    'call_gitlab_tool',
    'get_gitlab_mr_info',
    'get_gitlab_mr_changes',
    'get_file_content',
    'post_gitlab_comment',
    '_normalize_mcp_json_payload'
]


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
        import asyncio
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
