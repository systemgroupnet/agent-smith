"""
GitLab MCP Client - Integration with GitLab MCP Server

This module provides functions to interact with GitLab via the MCP (Model Context Protocol) server.
It follows the same pattern as the Atlassian MCP integration.

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
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def is_mcp_gitlab_enabled() -> bool:
    """Check if MCP GitLab integration is enabled."""
    return os.getenv("MCP_GITLAB_ENABLED", "true").lower() == "true"


def get_mcp_gitlab_server_params():
    """
    Get GitLab MCP server parameters.
    
    Supports two modes:
    1. Local Docker: Connects to local container via docker exec (MCP_GITLAB_HOST=localhost)
    2. Remote: Connects to remote MCP server via SSH tunnel or direct connection
    
    The MCP server is expected to expose MCP over stdio.
    
    Returns:
        StdioServerParameters for connecting to the MCP server
    """
    mcp_host = os.getenv("MCP_GITLAB_HOST", "localhost")
    container_name = os.getenv("MCP_GITLAB_CONTAINER", "mcp-gitlab")
    
    print(f"  🔌 [GitLab MCP] Creating transport connection...")
    print(f"     - Host: {mcp_host}")
    print(f"     - Container: {container_name}")
    
    # Local Docker mode: Use docker exec
    if mcp_host in ["localhost", "127.0.0.1"]:
        print(f"     - Mode: Local Docker")
        # GitLab MCP server entry point
        # Adjust the command based on how gitlab-mcp is installed/run
        # Common patterns:
        # - node /app/build/index.js (if built from source)
        # - npx gitlab-mcp (if installed via npm)
        # - gitlab-mcp (if installed globally)
        server_params = StdioServerParameters(
            command="docker",
            args=["exec", "-i", container_name, "node", "/app/build/index.js"],
            env=None
        )
    else:
        # Remote mode: Use SSH to connect to remote Docker container
        remote_user = os.getenv("MCP_GITLAB_SSH_USER", "root")
        print(f"     - Mode: Remote via SSH ({remote_user}@{mcp_host})")
        server_params = StdioServerParameters(
            command="ssh",
            args=[
                f"{remote_user}@{mcp_host}",
                "docker", "exec", "-i", container_name,
                "node", "/app/build/index.js"
            ],
            env=None
        )
    
    print(f"     - Command: {server_params.command} {' '.join(server_params.args)}")
    return server_params


async def call_gitlab_mcp_tool(tool_name: str, arguments: dict) -> Optional[dict]:
    """
    Call a tool on the GitLab MCP server.
    
    Args:
        tool_name: Name of the MCP tool to call
        arguments: Tool arguments as a dictionary
        
    Returns:
        Tool result as a dictionary or None if failed
    """
    print(f"  🔧 [GitLab MCP] Calling tool: {tool_name}")
    print(f"     - Arguments: {json.dumps(arguments, indent=2)}")
    
    try:
        server_params = get_mcp_gitlab_server_params()
        
        print(f"  🔄 [GitLab MCP] Establishing connection...")
        async with stdio_client(server_params) as (read_stream, write_stream):
            print(f"  ✅ [GitLab MCP] Transport connection established")
            print(f"  🔄 [GitLab MCP] Creating session for {tool_name}...")
            
            async with ClientSession(read_stream, write_stream) as session:
                print(f"  🔄 [GitLab MCP] Initializing session...")
                await session.initialize()
                print(f"  ✅ [GitLab MCP] Session initialized, calling tool...")
                
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

