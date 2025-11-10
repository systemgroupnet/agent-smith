"""
Atlassian MCP Client - Integration with Atlassian MCP Server

This module provides functions to interact with Jira and Confluence via the MCP (Model Context Protocol) server.

Usage:
    from core.MCP.atlassian_mcp_client import get_jira_issue_via_mcp, get_confluence_page_via_mcp
    
    issue = await get_jira_issue_via_mcp("PROJ-123")
    page = await get_confluence_page_via_mcp("12345")
"""

from __future__ import annotations

import os
import json
import traceback
from typing import Optional
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def is_mcp_atlassian_enabled() -> bool:
    """Check if MCP Atlassian integration is enabled."""
    return os.getenv("MCP_ATLASSIAN_ENABLED", "false").lower() == "true"


def get_mcp_atlassian_server_params():
    """
    Get Atlassian MCP server parameters.
    
    Supports two modes:
    1. Local Docker: Connects to local container via docker exec (MCP_ATLASSIAN_HOST=localhost)
    2. Remote: Connects to remote MCP server via SSH tunnel or direct connection
    
    The MCP server is expected to expose MCP over stdio.
    
    Returns:
        StdioServerParameters for connecting to the MCP server
    """
    mcp_host = os.getenv("MCP_ATLASSIAN_HOST", "localhost")
    mcp_port = os.getenv("MCP_ATLASSIAN_PORT", "3000")
    container_name = os.getenv("MCP_ATLASSIAN_CONTAINER", "mcp-atlassian")
    
    print(f"  🔌 [Atlassian MCP] Creating transport connection...")
    print(f"     - Host: {mcp_host}")
    print(f"     - Container: {container_name}")
    
    # Local Docker mode: Use docker exec
    if mcp_host in ["localhost", "127.0.0.1"]:
        print(f"     - Mode: Local Docker")
        server_params = StdioServerParameters(
            command="docker",
            args=["exec", "-i", container_name, "node", "/app/build/index.js"],
            env=None
        )
    else:
        # Remote mode: Use SSH to connect to remote Docker container
        # Assumes SSH access to the remote host
        # Format: ssh user@host docker exec -i container_name command
        remote_user = os.getenv("MCP_ATLASSIAN_SSH_USER", "root")
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


async def call_atlassian_mcp_tool(tool_name: str, arguments: dict) -> Optional[dict]:
    """
    Call a tool on the MCP Atlassian server.
    
    Args:
        tool_name: Name of the MCP tool to call
        arguments: Tool arguments as a dictionary
        
    Returns:
        Tool result as a dictionary or None if failed
    """
    print(f"  🔧 [Atlassian MCP] Calling tool: {tool_name}")
    print(f"     - Arguments: {json.dumps(arguments, indent=2)}")
    
    try:
        server_params = get_mcp_atlassian_server_params()
        
        print(f"  🔄 [Atlassian MCP] Establishing connection...")
        async with stdio_client(server_params) as (read_stream, write_stream):
            print(f"  ✅ [Atlassian MCP] Transport connection established")
            print(f"  🔄 [Atlassian MCP] Creating session for {tool_name}...")
            
            async with ClientSession(read_stream, write_stream) as session:
                print(f"  🔄 [Atlassian MCP] Initializing session...")
                await session.initialize()
                print(f"  ✅ [Atlassian MCP] Session initialized, calling tool...")
                
                result = await session.call_tool(tool_name, arguments)
                print(f"  📥 [Atlassian MCP] Tool {tool_name} returned response")
                
                # Parse the result
                if result:
                    print(f"     - Response has {len(result.content) if result.content else 0} content item(s)")
                    if result.content and len(result.content) > 0:
                        content = result.content[0]
                        if hasattr(content, 'text'):
                            try:
                                parsed = json.loads(content.text)
                                print(f"  ✅ [Atlassian MCP] Tool {tool_name} succeeded")
                                return parsed
                            except json.JSONDecodeError as e:
                                print(f"  ⚠️  [Atlassian MCP] Failed to parse JSON response: {e}")
                                print(f"     - Raw response: {content.text[:200]}...")
                                return None
                        else:
                            print(f"  ⚠️  [Atlassian MCP] Response content has no 'text' attribute")
                            print(f"     - Content type: {type(content)}")
                            return None
                    else:
                        print(f"  ⚠️  [Atlassian MCP] Tool {tool_name} returned empty content")
                        return None
                else:
                    print(f"  ⚠️  [Atlassian MCP] Tool {tool_name} returned None")
                    return None
            
    except Exception as e:
        print(f"  ❌ [Atlassian MCP] Error calling tool {tool_name}: {e}")
        print(f"     Traceback: {traceback.format_exc()}")
        return None


async def get_jira_issue_via_mcp(issue_key: str) -> Optional[dict]:
    """
    Get Jira issue information via Atlassian MCP.
    
    Args:
        issue_key: Jira issue key (e.g., "PROJ-123")
        
    Returns:
        Jira issue data as dict or None if failed
    """
    if not issue_key:
        print(f"  ⚠️  [Atlassian MCP] No issue key provided, skipping Jira fetch")
        return None
    
    print(f"  🔌 [Atlassian MCP] Fetching Jira issue: {issue_key}")
    
    result = await call_atlassian_mcp_tool("jira_get_issue", {"issue_key": issue_key})
    
    if result:
        fields = result.get("fields", {})
        summary = fields.get("summary", "N/A")
        print(f"  ✅ [Atlassian MCP] Jira issue fetched: {issue_key} - {summary}")
    else:
        print(f"  ❌ [Atlassian MCP] Failed to fetch Jira issue: {issue_key}")
    
    return result


async def get_confluence_page_via_mcp(page_id: str) -> Optional[dict]:
    """
    Get Confluence page information via Atlassian MCP.
    
    Args:
        page_id: Confluence page ID
        
    Returns:
        Confluence page data as dict or None if failed
    """
    if not page_id:
        print(f"  ⚠️  [Atlassian MCP] No page ID provided, skipping Confluence fetch")
        return None
    
    print(f"  🔌 [Atlassian MCP] Fetching Confluence page: {page_id}")
    
    result = await call_atlassian_mcp_tool("confluence_get_page", {"page_id": page_id})
    
    if result:
        title = result.get("title", "N/A")
        body_length = len(result.get("body", {}).get("storage", {}).get("value", ""))
        print(f"  ✅ [Atlassian MCP] Confluence page fetched: {title} ({body_length} chars)")
    else:
        print(f"  ❌ [Atlassian MCP] Failed to fetch Confluence page: {page_id}")
    
    return result

