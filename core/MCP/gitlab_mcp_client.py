"""
GitLab MCP Client - Direct Tool Exposure

This module provides direct access to GitLab MCP tools through langchain-mcp.
No wrapper methods - tools are exposed directly to LangChain agents for
the LLM to choose and use.

Usage:
    # Use the centralized toolkit manager instead:
    from core.MCP.toolkit_manager import get_mcp_toolkit_manager
    
    manager = get_mcp_toolkit_manager()
    await manager.initialize()
    gitlab_tools = manager.get_gitlab_tools()
    
    # Use tools with LangChain agents
"""

from __future__ import annotations

import os
from typing import Optional, List
from langchain_core.tools import BaseTool
from langchain_mcp import MCPToolkit
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client


# Singleton toolkit management (deprecated - use toolkit_manager instead)
_gitlab_mcp_toolkit: Optional[MCPToolkit] = None


def is_mcp_gitlab_enabled() -> bool:
    """Check if MCP GitLab integration is enabled."""
    return os.getenv("MCP_GITLAB_ENABLED", "true").lower() == "true"


def get_mcp_gitlab_url() -> str:
    """
    Get GitLab MCP server URL for HTTP/SSE transport.
    
    Returns:
        SSE endpoint URL for the MCP server (e.g., http://mcp-gitlab:8888/sse)
    """
    mcp_host = os.getenv("MCP_GITLAB_HOST", "mcp-gitlab")
    mcp_port = os.getenv("MCP_GITLAB_PORT", "8888")
    
    # If running in Docker, use container name; otherwise use localhost
    if mcp_host == "localhost":
        return f"http://localhost:{mcp_port}/sse"
    else:
        return f"http://{mcp_host}:{mcp_port}/sse"


async def get_gitlab_mcp_tools() -> List[BaseTool]:
    """
    Get or create LangChain tools from GitLab MCP server.
    
    This function creates a toolkit that exposes all GitLab MCP tools
    as LangChain tools. The toolkit manages the MCP connection and
    session automatically using HTTP/SSE transport.
    
    DEPRECATED: Use toolkit_manager.get_gitlab_tools() instead for
    better centralized management.
    
    Returns:
        List of LangChain tools that can be used with agents/chains
        
    Raises:
        Exception: If toolkit creation fails
    """
    global _gitlab_mcp_toolkit
    
    # Return existing toolkit if available
    if _gitlab_mcp_toolkit is not None:
        return _gitlab_mcp_toolkit.get_tools()
    
    # Create new toolkit
    try:
        sse_url = get_mcp_gitlab_url()
        print(f"  🔌 [GitLab MCP] Creating toolkit with SSE endpoint: {sse_url}")
        print(f"     - Transport: HTTP with SSE (Server-Sent Events)")
        
        # Create SSE client and get streams
        async with sse_client(sse_url) as (read_stream, write_stream):
            # Create ClientSession with the streams
            session = ClientSession(read_stream, write_stream)
            
            # Create MCPToolkit with the session
            _gitlab_mcp_toolkit = MCPToolkit(session=session)
            
            print(f"  🔄 [GitLab MCP] Initializing toolkit...")
            await _gitlab_mcp_toolkit.initialize()
            
            tools = _gitlab_mcp_toolkit.get_tools()
            print(f"  ✅ [GitLab MCP] Toolkit initialized with {len(tools)} tool(s)")
            
            # Print available tools
            for tool in tools:
                print(f"     - {tool.name}: {tool.description}")
            
            return tools
        
    except Exception as e:
        print(f"  ❌ [GitLab MCP] Failed to create toolkit: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        _gitlab_mcp_toolkit = None
        raise


async def close_gitlab_mcp_toolkit():
    """
    Close the GitLab MCP toolkit and cleanup resources.
    
    DEPRECATED: Use toolkit_manager.close() instead.
    
    Should be called when shutting down the application or when
    you want to force a toolkit refresh.
    """
    global _gitlab_mcp_toolkit
    
    if _gitlab_mcp_toolkit is not None:
        try:
            await _gitlab_mcp_toolkit.close()
            print(f"  🔌 [GitLab MCP] Toolkit closed")
        except Exception as e:
            print(f"  ⚠️  [GitLab MCP] Error closing toolkit: {e}")
        finally:
            _gitlab_mcp_toolkit = None


# Backward compatibility alias
async def close_gitlab_mcp_session():
    """Backward compatibility alias for close_gitlab_mcp_toolkit()."""
    await close_gitlab_mcp_toolkit()
