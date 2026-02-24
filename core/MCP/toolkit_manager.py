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
from typing import List, Optional, Dict, Literal, Awaitable, Callable
import asyncio
from langchain_core.tools import BaseTool
from langchain_mcp import MCPToolkit
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from contextlib import AsyncExitStack

# Transport types
TransportType = Literal["sse", "streamable-http"]


class MCPToolkitManager:
    """
    Centralized manager for all MCP toolkits.
    
    This class handles initialization and lifecycle management of multiple
    MCP toolkits (GitLab, Atlassian) and provides a unified interface to
    access all available MCP tools.
    """
    
    # Define allowed tools for each MCP server
    ALLOWED_GITLAB_TOOLS = {
        "get_file_contents",
        "get_repository_tree",
    }
    
    ALLOWED_ATLASSIAN_TOOLS = {
        "jira_get_issue",
        "confluence_get_page",
        "jira_search",
    }
    
    def __init__(self):
        """Initialize the toolkit manager."""
        self._gitlab_toolkit: Optional[MCPToolkit] = None
        self._atlassian_toolkit: Optional[MCPToolkit] = None
        self._exit_stack: Optional[AsyncExitStack] = None
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
    
    def _get_gitlab_transport(self) -> TransportType:
        """Get GitLab MCP transport type from environment."""
        transport = os.getenv("MCP_GITLAB_TRANSPORT", "streamable-http").lower()
        if transport in ("sse", "streamable-http"):
            return transport  # type: ignore
        return "streamable-http"  # default to streamable-http
    
    def _get_atlassian_transport(self) -> TransportType:
        """Get Atlassian MCP transport type from environment."""
        transport = os.getenv("MCP_ATLASSIAN_TRANSPORT", "streamable-http").lower()
        if transport in ("sse", "streamable-http"):
            return transport  # type: ignore
        return "streamable-http"  # default to streamable-http
    
    def _get_gitlab_url(self) -> str:
        """Get GitLab MCP server URL (no endpoint appended)."""
        return os.getenv("MCP_GITLAB_URL", "http://mcp-gitlab:3002").rstrip('/')
    
    def _get_atlassian_url(self) -> str:
        """Get Atlassian MCP server URL (no endpoint appended)."""
        return os.getenv("MCP_ATLASSIAN_URL", "http://mcp-atlassian:9000").rstrip('/')
    
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
        
        # Create exit stack for managing async context managers
        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()
        
        # Initialize GitLab toolkit (isolated to prevent cancel scope leakage)
        if self.is_gitlab_enabled():
            await self._initialize_toolkit_isolated(
                self._initialize_gitlab_toolkit,
                "GitLab MCP"
            )
        else:
            print("  ⏭️  [GitLab MCP] Disabled, skipping...")
        
        # Initialize Atlassian toolkit (isolated to prevent cancel scope leakage)
        if self.is_atlassian_enabled():
            await self._initialize_toolkit_isolated(
                self._initialize_atlassian_toolkit,
                "Atlassian MCP"
            )
        else:
            print("  ⏭️  [Atlassian MCP] Disabled, skipping...")
        
        self._initialized = True
        print("\n✅ MCP Toolkit Manager initialized\n")
    
    async def _initialize_gitlab_toolkit(self) -> None:
        """Initialize GitLab MCP toolkit."""
        try:
            url = self._get_gitlab_url()
            transport = self._get_gitlab_transport()
            transport_name = "SSE (Server-Sent Events)" if transport == "sse" else "Streamable HTTP"
            
            print(f"  🔌 [GitLab MCP] Connecting to: {url}")
            print(f"     - Transport: {transport_name}")
            
            # Create client based on transport type
            if transport == "sse":
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    sse_client(url)
                )
            else:
                # streamable-http transport
                read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                    streamablehttp_client(url)
                )
            
            # Create ClientSession with the streams
            session = ClientSession(read_stream, write_stream)
            
            # Initialize the session
            await self._exit_stack.enter_async_context(session)
            
            # Create MCPToolkit with the session
            self._gitlab_toolkit = MCPToolkit(session=session)
            
            print(f"  🔄 [GitLab MCP] Initializing toolkit...")
            await self._gitlab_toolkit.initialize()
            
            all_tools = self._gitlab_toolkit.get_tools()
            filtered_tools = [t for t in all_tools if t.name in self.ALLOWED_GITLAB_TOOLS]
            print(f"  ✅ [GitLab MCP] Initialized with {len(filtered_tools)}/{len(all_tools)} tool(s) (filtered)")
            
            # Print available tools (filtered)
            for tool in filtered_tools:
                print(f"     - {tool.name}: {tool.description[:80]}...")
                
        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            print(f"  ❌ [GitLab MCP] Initialization cancelled: {e}")
            self._gitlab_toolkit = None
            # Don't raise - allow partial initialization
        except Exception as e:
            print(f"  ❌ [GitLab MCP] Failed to initialize: {e}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()}")
            self._gitlab_toolkit = None
            # Don't raise - allow partial initialization
    
    async def _initialize_atlassian_toolkit(self) -> None:
        """Initialize Atlassian MCP toolkit."""
        try:
            url = self._get_atlassian_url()
            transport = self._get_atlassian_transport()
            transport_name = "SSE (Server-Sent Events)" if transport == "sse" else "Streamable HTTP"
            
            print(f"  🔌 [Atlassian MCP] Connecting to: {url}")
            print(f"     - Transport: {transport_name}")
            
            # Create client based on transport type
            if transport == "sse":
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    sse_client(url)
                )
            else:
                # streamable-http transport
                read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                    streamablehttp_client(url)
                )
            
            # Create ClientSession with the streams
            session = ClientSession(read_stream, write_stream)
            
            # Initialize the session
            await self._exit_stack.enter_async_context(session)
            
            # Create MCPToolkit with the session
            self._atlassian_toolkit = MCPToolkit(session=session)
            
            print(f"  🔄 [Atlassian MCP] Initializing toolkit...")
            await self._atlassian_toolkit.initialize()
            
            all_tools = self._atlassian_toolkit.get_tools()
            filtered_tools = [t for t in all_tools if t.name in self.ALLOWED_ATLASSIAN_TOOLS]
            print(f"  ✅ [Atlassian MCP] Initialized with {len(filtered_tools)}/{len(all_tools)} tool(s) (filtered)")
            
            # Print available tools (filtered)
            for tool in filtered_tools:
                print(f"     - {tool.name}: {tool.description[:80]}...")
                
        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            print(f"  ❌ [Atlassian MCP] Initialization cancelled: {e}")
            self._atlassian_toolkit = None
            # Don't raise - allow partial initialization
        except Exception as e:
            print(f"  ❌ [Atlassian MCP] Failed to initialize: {e}")
            import traceback
            print(f"     Traceback: {traceback.format_exc()}")
            self._atlassian_toolkit = None
            # Don't raise - allow partial initialization

    async def _initialize_toolkit_isolated(
        self,
        initializer: Callable[[], Awaitable[None]],
        toolkit_label: str,
        timeout: float = 30.0
    ) -> None:
        """Run toolkit initialization in a child task to avoid cancel leakage.
        
        Uses asyncio.shield() and timeout to fully isolate MCP cancel scopes
        from propagating to the main workflow.
        """
        async def _run_with_shield():
            """Inner wrapper to fully isolate the initializer."""
            try:
                await asyncio.wait_for(initializer(), timeout=timeout)
            except asyncio.TimeoutError:
                print(f"  ❌ [{toolkit_label}] Initialization timed out after {timeout}s")
            except asyncio.CancelledError as e:
                # Suppress cancellation - don't let it propagate
                print(f"  ❌ [{toolkit_label}] Initialization cancelled: {e}")
            except Exception as e:
                print(f"  ❌ [{toolkit_label}] Initialization failed: {e}")
        
        task = asyncio.create_task(_run_with_shield())
        try:
            # Shield prevents external cancellation from reaching the task
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # If we get cancelled while shielding, wait for task to finish anyway
            # but don't propagate the cancellation
            try:
                await task
            except Exception:
                pass
            # Clear any pending cancellation on current task
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()
        except Exception as e:
            print(f"  ❌ [{toolkit_label}] Unexpected error: {e}")
    
    def get_gitlab_tools(self) -> List[BaseTool]:
        """
        Get GitLab MCP tools (filtered to allowed tools only).
        
        Returns:
            List of GitLab MCP tools or empty list if not initialized
        """
        if self._gitlab_toolkit is None:
            return []
        
        try:
            all_tools = self._gitlab_toolkit.get_tools()
            # Filter to only allowed tools
            filtered_tools = [
                tool for tool in all_tools 
                if tool.name in self.ALLOWED_GITLAB_TOOLS
            ]
            return filtered_tools
        except Exception as e:
            print(f"  ⚠️  [GitLab MCP] Error getting tools: {e}")
            return []
    
    def get_atlassian_tools(self) -> List[BaseTool]:
        """
        Get Atlassian MCP tools (filtered to allowed tools only).
        
        Returns:
            List of Atlassian MCP tools or empty list if not initialized
        """
        if self._atlassian_toolkit is None:
            return []
        
        try:
            all_tools = self._atlassian_toolkit.get_tools()
            # Filter to only allowed tools
            filtered_tools = [
                tool for tool in all_tools 
                if tool.name in self.ALLOWED_ATLASSIAN_TOOLS
            ]
            return filtered_tools
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
        
        # Clear toolkit references first
        if self._gitlab_toolkit is not None:
            print("  ✅ [GitLab MCP] Toolkit closed")
            self._gitlab_toolkit = None
        
        if self._atlassian_toolkit is not None:
            print("  ✅ [Atlassian MCP] Toolkit closed")
            self._atlassian_toolkit = None
        
        # Close all async context managers (SSE/HTTP clients)
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except BaseExceptionGroup as eg:
                # Handle ExceptionGroup from TaskGroup (Python 3.11+)
                for exc in eg.exceptions:
                    exc_str = str(exc)
                    if "already running" in exc_str or "cancel scope" in exc_str or "TaskGroup" in exc_str:
                        print(f"  ⚠️  [MCP Manager] Async cleanup warning (safe to ignore): {type(exc).__name__}")
                    elif isinstance(exc, asyncio.CancelledError):
                        print("  ⚠️  [MCP Manager] Suppressed cancellation during cleanup")
                    else:
                        print(f"  ⚠️  [MCP Manager] Cleanup error: {exc}")
            except asyncio.CancelledError:
                # Suppress cancellation errors during cleanup
                print("  ⚠️  [MCP Manager] Suppressed cancellation during cleanup")
            except RuntimeError as e:
                # Handle "async generator already running" errors gracefully
                if "already running" in str(e) or "cancel scope" in str(e):
                    print(f"  ⚠️  [MCP Manager] Async cleanup warning (safe to ignore): {e}")
                else:
                    print(f"  ⚠️  [MCP Manager] Error closing exit stack: {e}")
            except Exception as e:
                # Catch-all for other exceptions
                e_str = str(e)
                if "TaskGroup" in e_str or "unhandled errors" in e_str:
                    print(f"  ⚠️  [MCP Manager] TaskGroup cleanup warning (safe to ignore)")
                else:
                    print(f"  ⚠️  [MCP Manager] Error closing exit stack: {e}")
            finally:
                self._exit_stack = None
        
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

