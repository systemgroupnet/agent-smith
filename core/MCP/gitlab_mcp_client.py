"""
GitLab MCP Client - Integration with GitLab MCP Server

This module provides functions to interact with GitLab via the MCP (Model Context Protocol) server.
Uses HTTP/SSE transport for streaming communication and maintains persistent connections.

Usage:
    from core.MCP.gitlab_mcp_client import get_gitlab_mr_info_via_mcp, get_gitlab_mr_changes_via_mcp
    
    mr_info = await get_gitlab_mr_info_via_mcp(project_path, mr_id)
    changes = await get_gitlab_mr_changes_via_mcp(project_path, mr_id)
"""

from __future__ import annotations

import os
import json
import traceback
from typing import Optional
import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client


# Singleton session management
_gitlab_mcp_session: Optional[ClientSession] = None
_gitlab_mcp_client_context = None


def is_mcp_gitlab_enabled() -> bool:
    """Check if MCP GitLab integration is enabled."""
    return os.getenv("MCP_GITLAB_ENABLED", "true").lower() == "true"


def get_mcp_gitlab_url() -> str:
    """
    Get GitLab MCP server URL for HTTP/SSE transport.
    
    Returns:
        Base URL for the MCP server (e.g., http://mcp-gitlab:8888)
    """
    mcp_host = os.getenv("MCP_GITLAB_HOST", "mcp-gitlab")
    mcp_port = os.getenv("MCP_GITLAB_PORT", "8888")
    
    # If running in Docker, use container name; otherwise use localhost
    if mcp_host == "localhost":
        return f"http://localhost:{mcp_port}"
    else:
        return f"http://{mcp_host}:{mcp_port}"


async def get_gitlab_mcp_session() -> ClientSession:
    """
    Get or create a persistent MCP session for GitLab.
    
    This implements the singleton pattern to reuse the same session
    across multiple tool calls, which is the correct way to use MCP.
    
    Returns:
        Active ClientSession instance
    """
    global _gitlab_mcp_session, _gitlab_mcp_client_context
    
    # Return existing session if available
    if _gitlab_mcp_session is not None:
        return _gitlab_mcp_session
    
    # Create new session
    try:
        mcp_url = get_mcp_gitlab_url()
        print(f"  🔌 [GitLab MCP] Connecting to MCP server at {mcp_url}")
        print(f"     - Transport: HTTP with SSE (Server-Sent Events)")
        
        # Create SSE client with the MCP endpoint
        _gitlab_mcp_client_context = sse_client(
            url=f"{mcp_url}/sse",
            timeout=httpx.Timeout(timeout=60.0, connect=10.0)
        )
        
        # Enter the context and create session
        read_stream, write_stream = await _gitlab_mcp_client_context.__aenter__()
        
        _gitlab_mcp_session = ClientSession(read_stream, write_stream)
        await _gitlab_mcp_session.__aenter__()
        
        print(f"  🔄 [GitLab MCP] Initializing session...")
        await _gitlab_mcp_session.initialize()
        print(f"  ✅ [GitLab MCP] Session initialized and ready")
        
        return _gitlab_mcp_session
        
    except Exception as e:
        print(f"  ❌ [GitLab MCP] Failed to create session: {e}")
        print(f"     Traceback: {traceback.format_exc()}")
        _gitlab_mcp_session = None
        _gitlab_mcp_client_context = None
        raise


async def close_gitlab_mcp_session():
    """
    Close the GitLab MCP session.
    
    Should be called when shutting down the application or when
    you want to force a session refresh.
    """
    global _gitlab_mcp_session, _gitlab_mcp_client_context
    
    if _gitlab_mcp_session is not None:
        try:
            await _gitlab_mcp_session.__aexit__(None, None, None)
            print(f"  🔌 [GitLab MCP] Session closed")
        except Exception as e:
            print(f"  ⚠️  [GitLab MCP] Error closing session: {e}")
        finally:
            _gitlab_mcp_session = None
    
    if _gitlab_mcp_client_context is not None:
        try:
            await _gitlab_mcp_client_context.__aexit__(None, None, None)
        except Exception as e:
            print(f"  ⚠️  [GitLab MCP] Error closing client context: {e}")
        finally:
            _gitlab_mcp_client_context = None


async def call_gitlab_mcp_tool(tool_name: str, arguments: dict) -> Optional[dict]:
    """
    Call a tool on the GitLab MCP server using persistent session.
    
    Args:
        tool_name: Name of the MCP tool to call
        arguments: Tool arguments as a dictionary
        
    Returns:
        Tool result as a dictionary or None if failed
    """
    print(f"  🔧 [GitLab MCP] Calling tool: {tool_name}")
    print(f"     - Arguments: {json.dumps(arguments, indent=2)}")
    
    try:
        # Get or create persistent session
        session = await get_gitlab_mcp_session()
        
        print(f"  📤 [GitLab MCP] Sending request to tool: {tool_name}")
        result = await session.call_tool(tool_name, arguments)
        print(f"  📥 [GitLab MCP] Tool {tool_name} returned response")
        
        # Parse the result
        if result:
            print(f"     - Response has {len(result.content) if result.content else 0} content item(s)")
            if result.content and len(result.content) > 0:
                content = result.content[0]
                if hasattr(content, 'text'):
                    try:
                        parsed = json.loads(content.text)
                        print(f"  ✅ [GitLab MCP] Tool {tool_name} succeeded")
                        return parsed
                    except json.JSONDecodeError as e:
                        print(f"  ⚠️  [GitLab MCP] Failed to parse JSON response: {e}")
                        print(f"     - Raw response: {content.text[:200]}...")
                        # Sometimes the response might be plain text, return as dict
                        return {"text": content.text}
                else:
                    print(f"  ⚠️  [GitLab MCP] Response content has no 'text' attribute")
                    print(f"     - Content type: {type(content)}")
                    return None
            else:
                print(f"  ⚠️  [GitLab MCP] Tool {tool_name} returned empty content")
                return None
        else:
            print(f"  ⚠️  [GitLab MCP] Tool {tool_name} returned None")
            return None
            
    except Exception as e:
        print(f"  ❌ [GitLab MCP] Error calling tool {tool_name}: {e}")
        print(f"     Traceback: {traceback.format_exc()}")
        
        # On error, reset session to force reconnect on next call
        print(f"  🔄 [GitLab MCP] Resetting session due to error")
        await close_gitlab_mcp_session()
        return None


async def get_gitlab_mr_info_via_mcp(project_path: str, mr_id: str) -> Optional[dict]:
    """
    Get merge request information via GitLab MCP.
    
    Args:
        project_path: GitLab project path (e.g., "group/project")
        mr_id: Merge request IID or ID
        
    Returns:
        Merge request data as dict or None if failed
    """
    print(f"  🔌 [GitLab MCP] Fetching MR info: {project_path} !{mr_id}")
    
    result = await call_gitlab_mcp_tool("get_merge_request", {
        "project_id": project_path,
        "merge_request_iid": mr_id
    })
    
    if result:
        title = result.get("title", "N/A")
        print(f"  ✅ [GitLab MCP] MR info fetched: {title}")
    else:
        print(f"  ❌ [GitLab MCP] Failed to fetch MR info")
    
    return result


async def get_gitlab_mr_changes_via_mcp(project_path: str, mr_id: str) -> Optional[list]:
    """
    Get merge request changes/diffs via GitLab MCP.
    
    Args:
        project_path: GitLab project path (e.g., "group/project")
        mr_id: Merge request IID or ID
        
    Returns:
        List of changes or None if failed
    """
    print(f"  🔌 [GitLab MCP] Fetching MR changes: {project_path} !{mr_id}")
    
    result = await call_gitlab_mcp_tool("get_merge_request_diffs", {
        "project_id": project_path,
        "merge_request_iid": mr_id
    })
    
    if result:
        # The result might be a dict with changes array or directly an array
        if isinstance(result, dict):
            changes = result.get("changes", result.get("diffs", []))
        else:
            changes = result if isinstance(result, list) else []
        
        print(f"  ✅ [GitLab MCP] MR changes fetched: {len(changes)} file(s)")
        return changes
    else:
        print(f"  ❌ [GitLab MCP] Failed to fetch MR changes")
        return None


async def get_gitlab_file_content_via_mcp(project_path: str, file_path: str, branch: str) -> Optional[str]:
    """
    Get file content from GitLab repository via MCP.
    
    Args:
        project_path: GitLab project path (e.g., "group/project")
        file_path: Path to the file in the repository
        branch: Branch name or commit SHA
        
    Returns:
        File content as string or None if failed
    """
    print(f"  🔌 [GitLab MCP] Fetching file: {project_path}/{file_path}@{branch}")
    
    result = await call_gitlab_mcp_tool("get_file_contents", {
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
        
        print(f"  ✅ [GitLab MCP] File content fetched: {len(content)} chars")
        return content
    else:
        print(f"  ❌ [GitLab MCP] Failed to fetch file content")
        return None


async def post_gitlab_comment_via_mcp(
    project_path: str,
    mr_id: str,
    comment: str,
    file_path: Optional[str] = None,
    line_number: Optional[int] = None
) -> bool:
    """
    Post a comment to GitLab MR via MCP.
    
    Args:
        project_path: GitLab project path (e.g., "group/project")
        mr_id: Merge request IID or ID
        comment: Comment text
        file_path: Optional file path for line-specific comment
        line_number: Optional line number for line-specific comment
        
    Returns:
        True if successful, False otherwise
    """
    print(f"  🔌 [GitLab MCP] Posting comment to MR: {project_path} !{mr_id}")
    
    if file_path and line_number:
        # Create a thread/discussion on a specific line
        result = await call_gitlab_mcp_tool("create_merge_request_thread", {
            "project_id": project_path,
            "merge_request_iid": mr_id,
            "body": comment,
            "position": {
                "new_path": file_path,
                "new_line": line_number
            }
        })
    else:
        # General comment (note: use create_merge_request_note, not create_note)
        result = await call_gitlab_mcp_tool("create_merge_request_note", {
            "project_id": project_path,
            "merge_request_iid": mr_id,
            "body": comment
        })
    
    if result:
        print(f"  ✅ [GitLab MCP] Comment posted successfully")
        return True
    else:
        print(f"  ❌ [GitLab MCP] Failed to post comment")
        return False

