"""
MCP Toolkit Manager - Centralized MCP Tool Management

This module provides a centralized manager for all MCP toolkits (GitLab, Atlassian).
It exposes MCP tools directly to LangChain agents without wrapper methods.

Usage:
    from core.MCP.toolkit_manager import get_mcp_toolkit_manager
    
    manager = get_mcp_toolkit_manager()
    await manager.initialize()
    tools = manager.get_all_tools()
    
    # Use tools with LangChain agents
"""

from __future__ import annotations

import os
from typing import List, Optional, Dict
from langchain_core.tools import BaseTool
from langchain_mcp import MCPToolkit


class MCPToolkitManager:
    """
    Centralized manager for all MCP toolkits.
    
    This class handles initialization and lifecycle management of multiple
    MCP toolkits (GitLab, Atlassian) and provides a unified interface to
    access all available MCP tools.
    """
    
    def __init__(self):
        """Initialize the toolkit manager."""
        self._gitlab_toolkit: Optional[MCPToolkit] = None
        self._atlassian_toolkit: Optional[MCPToolkit] = None
        self._initialized = False
    
    @property
    def is_initialized(self) -> bool:
        """Check if manager is initialized."""
        return self._initialized
    
    def is_gitlab_enabled(self) -> bool:
        """Check if GitLab MCP integration is enabled."""
        return os.getenv("MCP_GITLAB_ENABLED", "true").lower() == "true"
    
    def is_atlassian_enabled(self) -> bool:
        """Check if Atlassian MCP integration is enabled."""
        return os.getenv("MCP_ATLASSIAN_ENABLED", "false").lower() == "true"
    
    def _get_gitlab_sse_url(self) -> str:
        """Get GitLab MCP server SSE endpoint URL."""
        mcp_host = os.getenv("MCP_GITLAB_HOST", "mcp-gitlab")
        mcp_port = os.getenv("MCP_GITLAB_PORT", "8888")
        
        if mcp_host == "localhost":
            return f"http://localhost:{mcp_port}/sse"
        return f"http://{mcp_host}:{mcp_port}/sse"
    
    def _get_atlassian_sse_url(self) -> str:
        """Get Atlassian MCP server SSE endpoint URL."""
        mcp_host = os.getenv("MCP_ATLASSIAN_HOST", "mcp-atlassian")
        mcp_port = os.getenv("MCP_ATLASSIAN_PORT", "8080")
        
        if mcp_host == "localhost":
            return f"http://localhost:{mcp_port}/sse"
        return f"http://{mcp_host}:{mcp_port}/sse"
    
    async def initialize(self) -> None:
        """
        Initialize all enabled MCP toolkits.
        
        This method creates and initializes MCPToolkit instances for each
        enabled MCP server (GitLab, Atlassian).
        
        Raises:
            Exception: If toolkit initialization fails
        """
        if self._initialized:
            print("  ⚠️  [MCP Manager] Already initialized, skipping...")
            return
        
        print("\n" + "="*60)
        print("🔌 Initializing MCP Toolkits")
        print("="*60)
        
        # Initialize GitLab toolkit
        if self.is_gitlab_enabled():
            await self._initialize_gitlab_toolkit()
        else:
            print("  ⏭️  [GitLab MCP] Disabled, skipping...")
        
        # Initialize Atlassian toolkit
        if self.is_atlassian_enabled():
            await self._initialize_atlassian_toolkit()
        else:
            print("  ⏭️  [Atlassian MCP] Disabled, skipping...")
        
        self._initialized = True
        print("\n✅ MCP Toolkit Manager initialized\n")
    
    async def _initialize_gitlab_toolkit(self) -> None:
        """Initialize GitLab MCP toolkit."""
        try:
            sse_url = self._get_gitlab_sse_url()
            print(f"  🔌 [GitLab MCP] Connecting to: {sse_url}")
            print(f"     - Transport: HTTP with SSE (Server-Sent Events)")
            
            self._gitlab_toolkit = MCPToolkit(
                server_url=sse_url,
                transport="sse"
            )
            
            print(f"  🔄 [GitLab MCP] Initializing toolkit...")
            await self._gitlab_toolkit.initialize()
            
            tools = self._gitlab_toolkit.get_tools()
            print(f"  ✅ [GitLab MCP] Initialized with {len(tools)} tool(s)")
            
            # Print available tools
            for tool in tools:
                print(f"     - {tool.name}: {tool.description[:80]}...")
                
        except Exception as e:
            print(f"  ❌ [GitLab MCP] Failed to initialize: {e}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()}")
            self._gitlab_toolkit = None
            # Don't raise - allow partial initialization
    
    async def _initialize_atlassian_toolkit(self) -> None:
        """Initialize Atlassian MCP toolkit."""
        try:
            sse_url = self._get_atlassian_sse_url()
            print(f"  🔌 [Atlassian MCP] Connecting to: {sse_url}")
            print(f"     - Transport: HTTP with SSE (Server-Sent Events)")
            
            self._atlassian_toolkit = MCPToolkit(
                server_url=sse_url,
                transport="sse"
            )
            
            print(f"  🔄 [Atlassian MCP] Initializing toolkit...")
            await self._atlassian_toolkit.initialize()
            
            tools = self._atlassian_toolkit.get_tools()
            print(f"  ✅ [Atlassian MCP] Initialized with {len(tools)} tool(s)")
            
            # Print available tools
            for tool in tools:
                print(f"     - {tool.name}: {tool.description[:80]}...")
                
        except Exception as e:
            print(f"  ❌ [Atlassian MCP] Failed to initialize: {e}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()}")
            self._atlassian_toolkit = None
            # Don't raise - allow partial initialization
    
    def get_gitlab_tools(self) -> List[BaseTool]:
        """
        Get GitLab MCP tools.
        
        Returns:
            List of GitLab MCP tools or empty list if not initialized
        """
        if self._gitlab_toolkit is None:
            return []
        
        try:
            return self._gitlab_toolkit.get_tools()
        except Exception as e:
            print(f"  ⚠️  [GitLab MCP] Error getting tools: {e}")
            return []
    
    def get_atlassian_tools(self) -> List[BaseTool]:
        """
        Get Atlassian MCP tools.
        
        Returns:
            List of Atlassian MCP tools or empty list if not initialized
        """
        if self._atlassian_toolkit is None:
            return []
        
        try:
            return self._atlassian_toolkit.get_tools()
        except Exception as e:
            print(f"  ⚠️  [Atlassian MCP] Error getting tools: {e}")
            return []
    
    def get_all_tools(self) -> List[BaseTool]:
        """
        Get all available MCP tools from all toolkits.
        
        Returns:
            Combined list of all MCP tools
        """
        all_tools = []
        all_tools.extend(self.get_gitlab_tools())
        all_tools.extend(self.get_atlassian_tools())
        return all_tools
    
    def get_tools_by_category(self) -> Dict[str, List[BaseTool]]:
        """
        Get tools organized by category (gitlab, atlassian).
        
        Returns:
            Dictionary mapping category names to tool lists
        """
        return {
            "gitlab": self.get_gitlab_tools(),
            "atlassian": self.get_atlassian_tools(),
        }
    
    async def close(self) -> None:
        """
        Close all MCP toolkits and cleanup resources.
        
        Should be called when shutting down the application.
        """
        print("\n🔌 Closing MCP Toolkits...")
        
        if self._gitlab_toolkit is not None:
            try:
                await self._gitlab_toolkit.close()
                print("  ✅ [GitLab MCP] Toolkit closed")
            except Exception as e:
                print(f"  ⚠️  [GitLab MCP] Error closing toolkit: {e}")
            finally:
                self._gitlab_toolkit = None
        
        if self._atlassian_toolkit is not None:
            try:
                await self._atlassian_toolkit.close()
                print("  ✅ [Atlassian MCP] Toolkit closed")
            except Exception as e:
                print(f"  ⚠️  [Atlassian MCP] Error closing toolkit: {e}")
            finally:
                self._atlassian_toolkit = None
        
        self._initialized = False
        print("✅ MCP Toolkit Manager closed\n")
    
    async def reset(self) -> None:
        """
        Reset the manager by closing and re-initializing all toolkits.
        
        Useful for recovering from errors or forcing a reconnection.
        """
        print("🔄 Resetting MCP Toolkit Manager...")
        await self.close()
        await self.initialize()


# =============================================================================
# Singleton Instance
# =============================================================================

_mcp_toolkit_manager: Optional[MCPToolkitManager] = None


def get_mcp_toolkit_manager() -> MCPToolkitManager:
    """
    Get or create the global MCP toolkit manager instance.
    
    Returns:
        Singleton MCPToolkitManager instance
    """
    global _mcp_toolkit_manager
    
    if _mcp_toolkit_manager is None:
        _mcp_toolkit_manager = MCPToolkitManager()
    
    return _mcp_toolkit_manager


async def initialize_mcp_toolkits() -> MCPToolkitManager:
    """
    Initialize MCP toolkits and return the manager.
    
    Convenience function that gets the manager and initializes it.
    
    Returns:
        Initialized MCPToolkitManager instance
    """
    manager = get_mcp_toolkit_manager()
    if not manager.is_initialized:
        await manager.initialize()
    return manager


async def close_mcp_toolkits() -> None:
    """
    Close all MCP toolkits.
    
    Convenience function for cleanup on application shutdown.
    """
    global _mcp_toolkit_manager
    
    if _mcp_toolkit_manager is not None:
        await _mcp_toolkit_manager.close()
        _mcp_toolkit_manager = None

