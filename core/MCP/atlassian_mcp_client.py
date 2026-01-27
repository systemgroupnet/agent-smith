"""
Atlassian MCP Client - Integration with Atlassian MCP Server

This module provides functions to interact with Jira and Confluence via the MCP (Model Context Protocol) server.
Uses HTTP/SSE transport for streaming communication and maintains persistent connections.

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
import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client


# Singleton session management
_atlassian_mcp_session: Optional[ClientSession] = None
_atlassian_mcp_client_context = None


def is_mcp_atlassian_enabled() -> bool:
    """Check if MCP Atlassian integration is enabled."""
    return os.getenv("MCP_ATLASSIAN_ENABLED", "false").lower() == "true"


def get_mcp_atlassian_url() -> str:
    """
    Get Atlassian MCP server URL for HTTP/SSE transport.
    
    Returns:
        Base URL for the MCP server (e.g., http://mcp-atlassian:8080)
    """
    mcp_host = os.getenv("MCP_ATLASSIAN_HOST", "mcp-atlassian")
    mcp_port = os.getenv("MCP_ATLASSIAN_PORT", "8080")
    
    # If running in Docker, use container name; otherwise use localhost
    if mcp_host == "localhost":
        return f"http://localhost:{mcp_port}"
    else:
        return f"http://{mcp_host}:{mcp_port}"


async def get_atlassian_mcp_session() -> ClientSession:
    """
    Get or create a persistent MCP session for Atlassian.
    
    This implements the singleton pattern to reuse the same session
    across multiple tool calls, which is the correct way to use MCP.
    
    Returns:
        Active ClientSession instance
    """
    global _atlassian_mcp_session, _atlassian_mcp_client_context
    
    # Return existing session if available
    if _atlassian_mcp_session is not None:
        return _atlassian_mcp_session
    
    # Create new session
    try:
        mcp_url = get_mcp_atlassian_url()
        print(f"  🔌 [Atlassian MCP] Connecting to MCP server at {mcp_url}")
        print(f"     - Transport: HTTP with SSE (Server-Sent Events)")
        
        # Create SSE client with the MCP endpoint
        _atlassian_mcp_client_context = sse_client(
            url=f"{mcp_url}/sse",
            timeout=httpx.Timeout(timeout=60.0, connect=10.0)
        )
        
        # Enter the context and create session
        read_stream, write_stream = await _atlassian_mcp_client_context.__aenter__()
        
        _atlassian_mcp_session = ClientSession(read_stream, write_stream)
        await _atlassian_mcp_session.__aenter__()
        
        print(f"  🔄 [Atlassian MCP] Initializing session...")
        await _atlassian_mcp_session.initialize()
        print(f"  ✅ [Atlassian MCP] Session initialized and ready")
        
        return _atlassian_mcp_session
        
    except Exception as e:
        print(f"  ❌ [Atlassian MCP] Failed to create session: {e}")
        print(f"     Traceback: {traceback.format_exc()}")
        _atlassian_mcp_session = None
        _atlassian_mcp_client_context = None
        raise


async def close_atlassian_mcp_session():
    """
    Close the Atlassian MCP session.
    
    Should be called when shutting down the application or when
    you want to force a session refresh.
    """
    global _atlassian_mcp_session, _atlassian_mcp_client_context
    
    if _atlassian_mcp_session is not None:
        try:
            await _atlassian_mcp_session.__aexit__(None, None, None)
            print(f"  🔌 [Atlassian MCP] Session closed")
        except Exception as e:
            print(f"  ⚠️  [Atlassian MCP] Error closing session: {e}")
        finally:
            _atlassian_mcp_session = None
    
    if _atlassian_mcp_client_context is not None:
        try:
            await _atlassian_mcp_client_context.__aexit__(None, None, None)
        except Exception as e:
            print(f"  ⚠️  [Atlassian MCP] Error closing client context: {e}")
        finally:
            _atlassian_mcp_client_context = None


async def call_atlassian_mcp_tool(tool_name: str, arguments: dict) -> Optional[dict]:
    """
    Call a tool on the MCP Atlassian server using persistent session.
    
    Args:
        tool_name: Name of the MCP tool to call
        arguments: Tool arguments as a dictionary
        
    Returns:
        Tool result as a dictionary or None if failed
    """
    print(f"  🔧 [Atlassian MCP] Calling tool: {tool_name}")
    print(f"     - Arguments: {json.dumps(arguments, indent=2)}")
    
    try:
        # Get or create persistent session
        session = await get_atlassian_mcp_session()
        
        print(f"  📤 [Atlassian MCP] Sending request to tool: {tool_name}")
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
        
        # On error, reset session to force reconnect on next call
        print(f"  🔄 [Atlassian MCP] Resetting session due to error")
        await close_atlassian_mcp_session()
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

