"""
Atlassian MCP Client - Direct Tool Exposure

This module provides direct access to Atlassian MCP tools through langchain-mcp.
No wrapper methods - tools are exposed directly to LangChain agents for
the LLM to choose and use.

Usage:
    # Use the centralized toolkit manager instead:
    from core.MCP.toolkit_manager import get_mcp_toolkit_manager
    
    manager = get_mcp_toolkit_manager()
    await manager.initialize()
    atlassian_tools = manager.get_atlassian_tools()
    
    # Use tools with LangChain agents
"""

from __future__ import annotations

import os
from typing import Optional, List
from langchain_core.tools import BaseTool
from langchain_mcp import MCPToolkit


# Singleton toolkit management (deprecated - use toolkit_manager instead)
_atlassian_mcp_toolkit: Optional[MCPToolkit] = None


def is_mcp_atlassian_enabled() -> bool:
    """Check if MCP Atlassian integration is enabled."""
    return os.getenv("MCP_ATLASSIAN_ENABLED", "false").lower() == "true"


def get_mcp_atlassian_url() -> str:
    """
    Get Atlassian MCP server URL for HTTP/SSE transport.
    
    Returns:
        SSE endpoint URL for the MCP server (e.g., http://mcp-atlassian:8080/sse)
    """
    mcp_host = os.getenv("MCP_ATLASSIAN_HOST", "mcp-atlassian")
    mcp_port = os.getenv("MCP_ATLASSIAN_PORT", "8080")
    
    # If running in Docker, use container name; otherwise use localhost
    if mcp_host == "localhost":
        return f"http://localhost:{mcp_port}/sse"
    else:
        return f"http://{mcp_host}:{mcp_port}/sse"


async def get_atlassian_mcp_tools() -> List[BaseTool]:
    """
    Get or create LangChain tools from Atlassian MCP server.
    
    This function creates a toolkit that exposes all Atlassian MCP tools
    (Jira, Confluence) as LangChain tools. The toolkit manages the MCP
    connection and session automatically using HTTP/SSE transport.
    
    DEPRECATED: Use toolkit_manager.get_atlassian_tools() instead for
    better centralized management.
    
    Returns:
        List of LangChain tools that can be used with agents/chains
        
    Raises:
        Exception: If toolkit creation fails
    """
    global _atlassian_mcp_toolkit
    
    # Return existing toolkit if available
    if _atlassian_mcp_toolkit is not None:
        return _atlassian_mcp_toolkit.get_tools()
    
    # Create new toolkit
    try:
        sse_url = get_mcp_atlassian_url()
        print(f"  🔌 [Atlassian MCP] Creating toolkit with SSE endpoint: {sse_url}")
        print(f"     - Transport: HTTP with SSE (Server-Sent Events)")
        
        # Create MCPToolkit with SSE transport
        _atlassian_mcp_toolkit = MCPToolkit(
            server_url=sse_url,
            transport="sse"
        )
        
        print(f"  🔄 [Atlassian MCP] Initializing toolkit...")
        await _atlassian_mcp_toolkit.initialize()
        
        tools = _atlassian_mcp_toolkit.get_tools()
        print(f"  ✅ [Atlassian MCP] Toolkit initialized with {len(tools)} tool(s)")
        
        # Print available tools
        for tool in tools:
            print(f"     - {tool.name}: {tool.description}")
        
        return tools
        
    except Exception as e:
        print(f"  ❌ [Atlassian MCP] Failed to create toolkit: {e}")
        import traceback
        print(f"     Traceback: {traceback.format_exc()}")
        _atlassian_mcp_toolkit = None
        raise


async def close_atlassian_mcp_toolkit():
    """
    Close the Atlassian MCP toolkit and cleanup resources.
    
    DEPRECATED: Use toolkit_manager.close() instead.
    
    Should be called when shutting down the application or when
    you want to force a toolkit refresh.
    """
    global _atlassian_mcp_toolkit
    
    if _atlassian_mcp_toolkit is not None:
        try:
            await _atlassian_mcp_toolkit.close()
            print(f"  🔌 [Atlassian MCP] Toolkit closed")
        except Exception as e:
            print(f"  ⚠️  [Atlassian MCP] Error closing toolkit: {e}")
        finally:
            _atlassian_mcp_toolkit = None


# Backward compatibility alias
async def close_atlassian_mcp_session():
    """Backward compatibility alias for close_atlassian_mcp_toolkit()."""
    await close_atlassian_mcp_toolkit()
